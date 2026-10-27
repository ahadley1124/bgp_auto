#!/usr/bin/env python3
"""Run real routes through the generated BIRD filters and check accept/reject.

Starts a throw-away bird process (no kernel protocol, no sessions) whose only
job is to evaluate the rendered 00-defs / 20-filters / per-session filters.
Needs the `bird` and `birdc` binaries (apt install bird2).
"""
import json, os, re, subprocess, sys, tempfile, time, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "filter_plugins"))
import bgp_auto as b

raw = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
# Use a non-bogon own prefix so the "own space" rule can be tested separately from bogons.
raw["prefix_list_rules"][0]["prefix_custom"] = "45.200.0.0/22"
raw["prefix_list_rules"][0]["ge"] = None; raw["prefix_list_rules"][0]["le"] = None
raw["prefix_list_rules"][1]["prefix_custom"] = "45.201.0.0/22"
tmp = tempfile.mkdtemp(prefix="birdtest")
subprocess.check_call([sys.executable, os.path.join(HERE, "render_bird.py"), tmp, "--irr-json", os.path.join(HERE, "fixtures", "irr.json")]
                      , env=dict(os.environ)) if False else None

# render with the modified fixture
json.dump(raw, open(os.path.join(tmp, "fx.json"), "w"))
subprocess.check_call([sys.executable, os.path.join(HERE, "render_bird.py"), os.path.join(tmp, "r"),
                       "--fixture", os.path.join(tmp, "fx.json"), "--irr-json", os.path.join(HERE, "fixtures", "irr.json")],
                      stdout=subprocess.DEVNULL)

def to_function(text, name):
    """filter NAME {..}  ->  function NAME() -> bool {..} so a test filter can call it."""
    m = re.search(r"^filter %s \{\n(.*?)^\}\n" % re.escape(name), text, re.S | re.M)
    body = m.group(1)
    body = body.replace("then reject;", "then return false;").replace("accept;", "return true;")
    body = re.sub(r"^(\s*)reject;", r"\1return false;", body, flags=re.M)
    return "function %s() -> bool {\n%s}\n" % (name, body)

def run(node, imp_name, cases, extra_defs=""):
    d = os.path.join(tmp, "r", node, "generated")
    defs = open(os.path.join(d, "00-defs.conf")).read()
    filt = open(os.path.join(d, "20-filters.conf")).read()
    ipfx = open(os.path.join(d, "25-prefix-sets.conf")).read()
    ebgp = open(os.path.join(d, "70-ebgp.conf")).read()
    fn = to_function(ebgp, imp_name)
    setters = []
    routes = []
    for case in cases:
        prefix, path = case[0], case[1]
        pre = case[3] if len(case) > 3 else ""
        sets = "".join("bgp_path.prepend(%d); " % a for a in reversed(path)) + pre
        setters.append("if net = %s then { %s }" % (prefix, sets))
        routes.append("route %s blackhole;" % prefix)
    conf = f"""router id 192.0.2.1;
roa4 table roa_v4;
roa6 table roa_v6;
{defs}
{filt}
{ipfx}
{fn}
protocol static roas {{ roa4 {{ table roa_v4; }}; route 8.8.8.0/24 max 24 as 15169; route 1.1.1.0/24 max 24 as 13335; }}
filter T {{
  {' '.join(setters)}
  if {imp_name}() then accept;
  reject;
}}
protocol static t {{ ipv4 {{ import filter T; }}; {' '.join(routes)} }}
"""
    path = os.path.join(tmp, node + ".conf")
    open(path, "w").write(conf)
    sock = os.path.join(tmp, node + ".ctl")
    p = subprocess.run(["bird", "-p", "-c", path], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit("config error: " + p.stderr)
    subprocess.check_call(["bird", "-c", path, "-s", sock, "-P", os.path.join(tmp, node + ".pid")])
    time.sleep(1.0)
    out = subprocess.run(["birdc", "-s", sock, "show", "route", "all"], capture_output=True, text=True).stdout
    subprocess.run(["birdc", "-s", sock, "down"], capture_output=True)
    return out

fails = 0
def check(node, imp, cases):
    global fails
    out = run(node, imp, cases)
    for case in cases:
        prefix, path, expect = case[0], case[1], case[2]
        blk = re.search(r"^%s\s.*?(?=^\S|\Z)" % re.escape(prefix), out, re.S | re.M)
        got = bool(blk)
        ok = got == expect[0]
        extra = ""
        if expect[0] and blk and len(expect) > 1:
            for needle in expect[1:]:
                if needle not in blk.group(0):
                    ok = False; extra = " (missing %r)" % needle
        print("%-4s %-10s %-16s path=%s  expect=%s got=%s%s" % ("ok" if ok else "FAIL", node, prefix, path, expect[0], got, extra))
        fails += 0 if ok else 1

# Vultr transit: private 64515 is allowed on this session
check("vultr-transit", "IMP_tr_vultr_v4", [
    ("8.8.8.0/24",   [64515, 15169],        (True, "(17290, 1100, 1)", "(17290, 1000, 1)", "BGP.local_pref: 100")),
    ("1.1.1.0/24",   [64515, 15169],        (False,)),                      # ROA says AS13335 -> RPKI invalid
    ("4.4.0.0/16",   [64515, 3356, 3356],   (True, "(17290, 1100, 2)")),         # no ROA -> unknown -> accepted
    ("10.1.0.0/16",  [64515, 15169],        (False,)),                      # bogon prefix
    ("9.9.9.0/25",   [64515, 15169],        (False,)),                      # too specific
    ("45.200.1.0/24",[64515, 15169],        (False,)),                      # our own space from outside
    ("8.8.4.0/24",   [64515, 64500, 15169], (False,)),                      # documentation ASN in path
    ("8.8.5.0/24",   [64515, 65001, 15169], (True,)),                       # private ASN allowed here (Vultr)
    ("0.0.0.0/0",    [64515],               (False,)),                      # default route not accepted
])
# IX route server: the RS ASN is not in the path; a tier-1 in the path is a leak
check("vultr-ix1", "IMP_rs_ix1_rs1_v4", [
    ("4.4.0.0/16",   [15169],               (True, "(17290, 1000, 2)", "BGP.local_pref: 200")),
    ("4.5.0.0/16",   [3356, 15169],         (False,)),                      # tier-1 leak via IX
    ("4.6.0.0/16",   [65001, 15169],        (False,)),                      # private ASN not allowed
])
# Bilateral peer: only what its IRR as-set contains, and bilateral local-pref
check("vultr-ix1", "IMP_bi_ix1_peer_v4", [
    ("4.4.0.0/16",   [15169], (True, "(17290, 1000, 3)", "BGP.local_pref: 220")),
    ("4.9.0.0/20",   [15169],               (True,)),                       # inside 4.8.0.0/14 le 24
    ("4.12.0.0/16",  [15169],               (False,)),                      # outside the as-set
    ("203.0.113.0/24", [15169],              (False,)),                      # bogon
])
# Export: only our own prefixes that were originated by us, with per-session prepend
LC = "bgp_large_community.add(LC_LOCAL_ORIGIN);"
check("nl-transit", "EXP_tr_nl_v4", [
    ("45.200.0.0/22", [], (True, "BGP.as_path: 17290 17290 17290"), LC),   # NL provider: prepend x3 (+1 added on send)
    ("198.18.0.0/22", [], (False,)),                                       # our prefix but not tagged as locally originated
    ("8.8.8.0/24",    [], (False,), LC),                                   # not our prefix
])
check("vultr-transit", "EXP_tr_vultr_v4", [
    ("45.200.0.0/22", [], (True,), LC),
])
print("FAILURES:", fails)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
