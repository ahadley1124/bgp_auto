import json, os, stat, subprocess, sys, tempfile, unittest, importlib.machinery, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)

def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec); loader.exec_module(mod); return mod

tun = load(os.path.join(ROOT, "roles/tunnels/files/bgp-auto-tunnels"), "tun")
health = load(os.path.join(ROOT, "roles/bgp_router/files/bgp-auto-health"), "health")

class FakeIP:
    """Just enough of `ip` to exercise the reconciler."""
    def __init__(self, local_addrs):
        self.links = {}; self.addrs = {"lo": set()}; self.local = set(local_addrs); self.fou = set(); self.calls = []
    def __call__(self, cmd, check=False):
        self.calls.append(cmd)
        if cmd[:3] == ["ip", "-j", "addr"] and cmd[3] == "show" and len(cmd) == 4:
            return 0, json.dumps([{"addr_info": [{"local": a, "prefixlen": 32} for a in self.local]}])
        if cmd[:3] == ["ip", "-j", "addr"]:
            dev = cmd[-1]
            if dev != "lo" and dev not in self.links: return 1, ""
            ai = [{"local": a.split("/")[0], "prefixlen": int(a.split("/")[1])} for a in self.addrs.get(dev, set())]
            return 0, json.dumps([{"addr_info": ai}])
        if cmd[:4] == ["ip", "-j", "-d", "link"]:
            l = self.links.get(cmd[-1]);  return (0, json.dumps([l])) if l else (1, "")
        if cmd[:3] == ["ip", "fou", "show"]:
            return 0, "".join("port %s ipproto 47\n" % p for p in self.fou)
        if cmd[:3] == ["ip", "fou", "add"]:
            self.fou.add(cmd[4]); return 0, ""
        if cmd[:3] == ["ip", "link", "add"] and cmd[-1] == "dummy":
            self.links[cmd[3]] = {"mtu": 1500, "flags": [], "linkinfo": {"info_kind": "dummy"}}
            self.addrs[cmd[3]] = set(); return 0, ""
        if cmd[:3] == ["ip", "link", "add"]:
            kv = dict(zip(cmd[6::2], cmd[7::2]))
            info = {"local": kv["local"], "remote": kv["remote"], "ttl": int(kv["ttl"]), "encap_type": "fou" if "encap" in cmd else "none"}
            if "encap-dport" in cmd: info["encap_dport"] = int(cmd[cmd.index("encap-dport") + 1])
            self.links[cmd[3]] = {"mtu": 1476, "flags": ["POINTOPOINT"], "linkinfo": {"info_kind": "gre", "info_data": info}}
            self.addrs[cmd[3]] = set(); return 0, ""
        if cmd[:3] == ["ip", "link", "set"]:
            l = self.links[cmd[3]]
            if cmd[4] == "mtu": l["mtu"] = int(cmd[5])
            if cmd[4] == "up": l["flags"] = ["UP", "LOWER_UP"]
            return 0, ""
        if cmd[:3] == ["ip", "link", "del"]:
            self.links.pop(cmd[3], None); return 0, ""
        if cmd[:3] == ["ip", "addr", "add"]:
            self.addrs.setdefault(cmd[5], set()).add(cmd[3]); return 0, ""
        return 0, ""

CFG = {"fou_port": 5555, "loopbacks": ["10.255.255.1/32"], "tunnels": [
    {"name": "gre-nl", "local": "100.96.0.5", "remote": "100.96.0.9", "encap": "fou", "mtu": 1240, "ttl": 255, "addresses": ["10.255.0.0/31"]}]}

class ReconcilerCase(unittest.TestCase):
    """Keeps the managed-tunnel state file out of /var/lib so the tests run unprivileged."""
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.state = os.path.join(d.name, "tunnels.managed")
    def reconciler(self, cfg, ip):
        return tun.Reconciler(cfg, runner=ip, log=lambda *_: None, state=self.state)

class TunnelTests(ReconcilerCase):
    def apply(self, ip, cfg=CFG):
        r = self.reconciler(cfg, ip); r.apply(); return r.changed
    def test_idempotent(self):
        ip = FakeIP(["100.96.0.5"])
        self.assertTrue(self.apply(ip))
        self.assertFalse(self.apply(ip), "second run must not touch anything (would flap BGP)")
    def test_param_change_recreates(self):
        ip = FakeIP(["100.96.0.5"]); self.apply(ip)
        cfg = json.loads(json.dumps(CFG)); cfg["tunnels"][0]["remote"] = "100.96.0.10"
        self.assertTrue(self.apply(ip, cfg))
        self.assertEqual(ip.links["gre-nl"]["linkinfo"]["info_data"]["remote"], "100.96.0.10")
    def test_mtu_only_change_does_not_recreate(self):
        ip = FakeIP(["100.96.0.5"]); self.apply(ip)
        cfg = json.loads(json.dumps(CFG)); cfg["tunnels"][0]["mtu"] = 1200
        ip.calls.clear(); self.apply(ip, cfg)
        self.assertFalse(any(c[:3] == ["ip", "link", "del"] for c in ip.calls))
        self.assertEqual(ip.links["gre-nl"]["mtu"], 1200)
    def test_fou_and_loopback(self):
        ip = FakeIP(["100.96.0.5"]); self.apply(ip)
        self.assertIn("5555", ip.fou); self.assertIn("10.255.255.1/32", ip.addrs["lo"])
        self.assertIn("10.255.0.0/31", ip.addrs["gre-nl"])

class DummyTests(ReconcilerCase):
    def test_mgmt_interface(self):
        ip = FakeIP([])
        cfg = {"tunnels": [], "dummies": [{"name": "mgmt0", "addresses": ["2001:db8:1::1/64"]}]}
        r = self.reconciler(cfg, ip)
        self.assertTrue(r.apply())
        self.assertIn("2001:db8:1::1/64", ip.addrs["mgmt0"])
        r = self.reconciler(cfg, ip)
        self.assertFalse(r.apply(), "second run changes nothing")


class HealthTests(unittest.TestCase):
    OUT = """BIRD 3.2.1 ready.
Name       Proto      Table      State  Since         Info
ibgp4_nl   BGP        ---        up     2026-10-27 12:00:01  Established
ospf4      OSPF       ---        up     2026-10-27 12:00:00  Running
tr_nl_v4   BGP        ---        start  2026-10-27 12:00:00  Connect
rtr1       RPKI       ---        up     2026-10-27 12:00:00  Established
"""
    def rows(self):
        old = health.subprocess.run
        health.subprocess.run = lambda *a, **k: type("P", (), {"returncode": 0, "stdout": self.OUT, "stderr": ""})()
        try: return health.protocols()[0]
        finally: health.subprocess.run = old
    def test_ok(self):
        self.assertEqual(health.status(self.rows(), ["ibgp4_nl", "ospf4", "rtr1"]), [])
    def test_down(self):
        bad = health.status(self.rows(), ["tr_nl_v4", "missing"])
        self.assertEqual(len(bad), 2)

class IrrTests(unittest.TestCase):
    def test_fetch_cache_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "bgpq4")
            open(fake, "w").write("#!/bin/sh\nif [ -f %s/fail ]; then echo boom >&2; exit 1; fi\n"
                "echo '{\"X\":[{\"prefix\":\"4.4.0.0/16\",\"exact\":true},{\"prefix\":\"4.8.0.0/14\",\"exact\":false,\"greater-equal\":14,\"less-equal\":24}]}'\n" % d)
            os.chmod(fake, 0o755)
            out = os.path.join(d, "o.json"); cache = os.path.join(d, "c")
            script = os.path.join(ROOT, "scripts", "irr_fetch.py")
            args = [sys.executable, script, "--out", out, "--cache", cache, "--bgpq4", fake, "--ttl", "0"]
            subprocess.check_call(args + ["AS-X"])
            data = json.load(open(out))["AS-X"]
            self.assertEqual(data["4"][1]["less-equal"], 24)
            open(os.path.join(d, "fail"), "w").close()
            p = subprocess.run(args + ["AS-X"], capture_output=True, text=True)   # IRR down: cached copy used
            self.assertEqual(p.returncode, 0); self.assertIn("cached", p.stderr)
            p = subprocess.run(args + ["AS-NEVER-SEEN"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)

if __name__ == "__main__":
    unittest.main()


class NetboxFetchTests(unittest.TestCase):
    def setUp(self):
        self.srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_netbox.py")], stdout=subprocess.PIPE, text=True)
        self.env = dict(os.environ, NETBOX_URL="http://127.0.0.1:%s" % self.srv.stdout.readline().strip(), NETBOX_TOKEN="testtoken")
    def tearDown(self):
        self.srv.terminate(); self.srv.wait(); self.srv.stdout.close()
    def run_script(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "netbox_fetch.py")] + list(args),
                              capture_output=True, text=True, env=env or self.env)
    def test_fetch_paginates(self):
        p = self.run_script("fetch"); self.assertEqual(p.returncode, 0, p.stderr)
        got = json.loads(p.stdout)
        want = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
        self.assertEqual(len(got["devices"]), len(want["devices"]))
        self.assertEqual(len(got["peers"]), len(want["peers"]))
    def test_bad_token(self):
        p = self.run_script("fetch", env=dict(self.env, NETBOX_TOKEN="nope")); self.assertNotEqual(p.returncode, 0)
    def test_set_cf_idempotent(self):
        self.assertIn("changed", self.run_script("set-cf", "nl-transit", "mesh_ip", "100.96.0.9/32").stdout)
        self.assertIn("unchanged", self.run_script("set-cf", "nl-transit", "mesh_ip", "100.96.0.9/32").stdout)
        d = {x["name"]: x for x in json.loads(self.run_script("fetch").stdout)["devices"]}
        self.assertEqual(d["nl-transit"]["custom_fields"]["mesh_ip"], "100.96.0.9/32")
    def test_probe(self):
        p = self.run_script("probe"); self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class BlockAnnouncementTests(unittest.TestCase):
    """An IX router originates its own /48 (and not the aggregates); transits accept it as our space."""
    def test_render(self):
        if not __import__("shutil").which("bird"):
            self.skipTest("needs bird")
        with tempfile.TemporaryDirectory() as d:
            raw = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
            for dev in raw["devices"]:
                if dev["name"] == "vultr-ix1":
                    dev["custom_fields"]["ipv6_block"] = "2001:db8:5001::/48"
                if dev["name"] == "nl-ix1":
                    dev["custom_fields"]["ipv6_block"] = "2001:db8:5002::/48"
            fx = os.path.join(d, "fx.json"); json.dump(raw, open(fx, "w"))
            out = os.path.join(d, "out")
            subprocess.check_call([sys.executable, os.path.join(HERE, "render_bird.py"), out, "--fixture", fx,
                                   "--irr-json", os.path.join(HERE, "fixtures", "irr.json")], stdout=subprocess.DEVNULL)
            ix = open(os.path.join(out, "vultr-ix1", "generated", "60-originate.conf")).read()
            self.assertIn("route 2001:db8:5001::/48 blackhole;", ix)
            self.assertNotIn("2001:db8:5000::/36 blackhole", ix, "IX routers must not originate the aggregates")
            self.assertNotIn("2001:db8:5002::/48", ix)
            prim = open(os.path.join(out, "vultr-transit", "generated", "60-originate.conf")).read()
            self.assertIn("2001:db8:5000::/36 blackhole", prim)
            self.assertNotIn("5001::/48 blackhole", prim, "the hub must not blackhole an IX router's /48")
            defs = open(os.path.join(out, "vultr-transit", "generated", "00-defs.conf")).read()
            self.assertIn("2001:db8:5001::/48", defs)
            self.assertIn("2001:db8:5002::/48", defs)
            main = open(os.path.join(out, "vultr-ix1", "bird.conf")).read()
            self.assertIn("60-originate.conf", main)
            for host in os.listdir(out):
                p = subprocess.run(["bird", "-p", "-c", os.path.join(out, host, "bird.conf")], capture_output=True, text=True, cwd="/")
                self.assertEqual(p.returncode, 0, host + ": " + p.stderr)


class AnycastRenderTests(unittest.TestCase):
    def fixture(self, d):
        raw = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
        raw["anycast_prefixes"] = [{"prefix": "2001:db8:5008::/46", "status": {"value": "active"}}]
        for dev in raw["devices"]:
            if dev["name"] in ("vultr-transit", "nl-transit"):
                dev["custom_fields"]["anycast_originate"] = True
        for s in raw["peers"]:
            if s["name"] == "vultr-v6":
                s["custom_fields"].update(anycast_prepend=2, anycast_communities="20473:6001:3356, 64601:3356")
        fx = os.path.join(d, "fx.json"); json.dump(raw, open(fx, "w")); return fx

    def test_render_and_parse(self):
        if not __import__("shutil").which("bird"):
            self.skipTest("needs bird")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            subprocess.check_call([sys.executable, os.path.join(HERE, "render_bird.py"), out, "--fixture", self.fixture(d),
                                   "--irr-json", os.path.join(HERE, "fixtures", "irr.json")], stdout=subprocess.DEVNULL)
            for host in ("vultr-transit", "nl-transit"):
                orig = open(os.path.join(out, host, "generated", "60-originate.conf")).read()
                self.assertIn("route 2001:db8:5008::/46 blackhole;", orig)
            ix = open(os.path.join(out, "vultr-ix1", "generated", "60-originate.conf")).read()
            self.assertNotIn("5008", ix, "only anycast_originate routers originate it")
            ebgp = open(os.path.join(out, "vultr-transit", "generated", "70-ebgp.conf")).read()
            self.assertIn("if net ~ ANYCAST_V6 then {", ebgp)
            self.assertIn("bgp_large_community.add((20473, 6001, 3356));", ebgp)
            self.assertIn("bgp_community.add((64601, 3356));", ebgp)
            self.assertEqual(ebgp.count("bgp_path.prepend(17290);"), 2 + 0, "2 anycast prepends, v4 session has none")
            nl = open(os.path.join(out, "nl-transit", "generated", "70-ebgp.conf")).read()
            self.assertNotIn("ANYCAST_V6 then", nl)
            for host in os.listdir(out):
                p = subprocess.run(["bird", "-p", "-c", os.path.join(out, host, "bird.conf")], capture_output=True, text=True, cwd="/")
                self.assertEqual(p.returncode, 0, host + ": " + p.stderr)

    def test_validation(self):
        sys.path.insert(0, os.path.join(ROOT, "filter_plugins"))
        import bgp_auto as b
        raw = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
        raw["anycast_prefixes"] = [{"prefix": "2001:db8:ffff::/46"}, {"prefix": "2001:db8:5008::/52"}]
        for s in raw["peers"]:
            if s["name"] == "vultr-v6":
                s["custom_fields"].update(anycast_communities="not-a-community")
        errs = " | ".join(b.build_model(raw, {})["errors"])
        self.assertIn("not inside our own prefixes", errs)
        self.assertIn("too specific", errs)
        self.assertIn("bad community", errs)


class AnycastWatchTests(unittest.TestCase):
    PFX = "2602:fa86:ff8::/46"

    def lg(self, rows):
        return {"data": {"rrcs": [{"rrc": "RRC00", "peers": [dict(prefix=self.PFX, as_path=p) for p in rows]}]}}

    def cfg(self, **kw):
        c = {"own_asn": 17290, "prefixes": [self.PFX], "tier1_asns": [174, 1299, 3356, 2914],
             "entries": {"20473": {"name": "Vultr", "sessions": [{"device": "vultr-transit", "label": "vultr-v6", "family": 6, "prepend": 0}]},
                         "205941": {"name": "NL", "sessions": [{"device": "nl-transit", "label": "nl-v6", "family": 6, "prepend": 0}]}}}
        c.update(kw); return c

    def load(self):
        return load(os.path.join(ROOT, "scripts", "anycast_watch.py"), "aw")

    def test_entry_and_shares(self):
        aw = self.load()
        res = aw.analyze({self.PFX: self.lg(["1299 20473 17290", "3356 20473 17290 17290", "174 205941 17290", "9999 20473 17290"])}, self.cfg())
        info = res["prefixes"][self.PFX]
        self.assertEqual(info["tier1_seen"], 3, "non tier-1 collector peers are ignored")
        self.assertAlmostEqual(info["share"]["20473"], 2 / 3, places=3)
        self.assertEqual(info["per_tier1"]["3356"]["length"], 4)
        self.assertEqual(info["per_tier1"]["3356"]["entry"], 20473)

    def test_suggests_prepend_on_overloaded_entry_and_never_applies(self):
        aw = self.load()
        data = self.lg(["1299 20473 17290", "3356 20473 17290", "174 20473 17290", "2914 205941 17290"])
        res = aw.analyze({self.PFX: data}, self.cfg(target_share={"20473": 0.5}))
        s = res["suggestions"]
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["session"], s[0]["from"], s[0]["to"]), ("vultr-v6", 0, 1))
        self.assertIn("anycast_prepend=1", s[0]["netbox"])
        self.assertIn("bgp_auto_anycast_suggestion", aw.prom(res, self.cfg()))

    def test_only_sessions_of_the_prefix_family(self):
        aw = self.load()
        cfg = self.cfg(target_share={"20473": 0.5})
        cfg["entries"]["20473"]["sessions"].append({"device": "vultr-transit", "label": "vultr-v4", "family": 4, "prepend": 0})
        data = self.lg(["1299 20473 17290", "3356 20473 17290", "174 20473 17290", "2914 205941 17290"])
        self.assertEqual([s["session"] for s in aw.analyze({self.PFX: data}, cfg)["suggestions"]], ["vultr-v6"])

    def test_balanced_gives_no_suggestion(self):
        aw = self.load()
        data = self.lg(["1299 20473 17290", "3356 205941 17290"])
        self.assertEqual(aw.analyze({self.PFX: data}, self.cfg(target_share={"20473": 0.5}))["suggestions"], [])

    def test_prepend_capped(self):
        aw = self.load()
        cfg = self.cfg(target_share={"20473": 0.2})
        cfg["entries"]["20473"]["sessions"][0]["prepend"] = 3
        res = aw.analyze({self.PFX: self.lg(["1299 20473 17290", "3356 20473 17290"])}, cfg)
        self.assertEqual(res["suggestions"], [])

    def test_prefix_not_seen_alerts(self):
        aw = self.load()
        res = aw.analyze({self.PFX: self.lg(["9999 20473 17290"])}, self.cfg())
        self.assertTrue(res["alerts"])

    def test_cli_offline(self):
        with tempfile.TemporaryDirectory() as d:
            json.dump(self.cfg(target_share={"20473": 0.5}), open(os.path.join(d, "c.json"), "w"))
            json.dump({self.PFX: self.lg(["1299 20473 17290", "3356 20473 17290", "174 20473 17290"])}, open(os.path.join(d, "in.json"), "w"))
            p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "anycast_watch.py"), "--config", os.path.join(d, "c.json"),
                                "--out-dir", os.path.join(d, "o"), "--input", os.path.join(d, "in.json")], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("SUGGESTION", p.stdout)
            self.assertTrue(os.path.exists(os.path.join(d, "o", "anycast.prom")))


class CollectorRenderTests(unittest.TestCase):
    """A route collector (RouteViews / RIS) is the one class that exports the whole view."""
    def fixture(self, d):
        raw = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
        tmpl = raw["peers"][0]
        raw["peers"].append({
            "id": 900, "name": "routeviews-v4", "display": "routeviews-v4",
            "scope": tmpl["scope"],
            "peer": {"address": "128.223.51.16/32"},
            "source": None,
            "local_as": {"asn": 17290}, "remote_as": {"asn": 6447},
            "peer_group": None, "status": {"value": "active"},
            "password": None, "ttl": None, "bfd": None,
            "custom_fields": {"session_class": "collector"},
        })
        fx = os.path.join(d, "fx.json"); json.dump(raw, open(fx, "w")); return fx

    def test_collector_exports_full_view(self):
        if not __import__("shutil").which("bird"):
            self.skipTest("needs bird")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            subprocess.check_call([sys.executable, os.path.join(HERE, "render_bird.py"), out,
                                   "--fixture", self.fixture(d),
                                   "--irr-json", os.path.join(HERE, "fixtures", "irr.json")],
                                  stdout=subprocess.DEVNULL)
            ebgp = open(os.path.join(out, "vultr-transit", "generated", "70-ebgp.conf")).read()
            # Compare against config only -- the comments mention these keywords on purpose.
            code = "\n".join(l for l in ebgp.splitlines() if not l.lstrip().startswith("#"))
            block = code[code.index("filter IMP_co_routeviews_v4"):]
            proto = block[block.index("protocol bgp co_routeviews_v4"):].split("\n}", 1)[0]
            self.assertIn("collector_export_ok()", block, "collector must use the full-view export")
            self.assertNotIn("receive limit", proto, "a receive limit would trip on the first update")
            self.assertNotIn("import keep filtered", proto,
                             "keeping filtered routes would hold a full table for nothing")
            # the transit session alongside it must keep both
            tr = code[code.index("protocol bgp tr_vultr_v4"):]
            self.assertIn("receive limit", tr)
            self.assertIn("import keep filtered", tr)
            # and the whole config must still parse
            p = subprocess.run(["bird", "-p", "-c", os.path.join(out, "vultr-transit", "bird.conf")],
                               capture_output=True, text=True, cwd="/")
            self.assertEqual(p.returncode, 0, p.stderr)
