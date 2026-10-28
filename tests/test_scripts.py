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

class TunnelTests(unittest.TestCase):
    def apply(self, ip, cfg=CFG):
        r = tun.Reconciler(cfg, runner=ip, log=lambda *_: None); r.apply(); return r.changed
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

class DummyTests(unittest.TestCase):
    def test_mgmt_interface(self):
        ip = FakeIP([])
        cfg = {"tunnels": [], "dummies": [{"name": "mgmt0", "addresses": ["2001:db8:1::1/64"]}]}
        r = tun.Reconciler(cfg, runner=ip, log=lambda *_: None)
        self.assertTrue(r.apply())
        self.assertIn("2001:db8:1::1/64", ip.addrs["mgmt0"])
        r = tun.Reconciler(cfg, runner=ip, log=lambda *_: None)
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
        self.assertEqual(len(got["sessions"]), len(want["sessions"]))
    def test_bad_token(self):
        p = self.run_script("fetch", env=dict(self.env, NETBOX_TOKEN="nope")); self.assertNotEqual(p.returncode, 0)
    def test_set_cf_idempotent(self):
        self.assertIn("changed", self.run_script("set-cf", "nl-transit", "mesh_ip", "100.96.0.9/32").stdout)
        self.assertIn("unchanged", self.run_script("set-cf", "nl-transit", "mesh_ip", "100.96.0.9/32").stdout)
        d = {x["name"]: x for x in json.loads(self.run_script("fetch").stdout)["devices"]}
        self.assertEqual(d["nl-transit"]["custom_fields"]["mesh_ip"], "100.96.0.9/32")
    def test_probe(self):
        p = self.run_script("probe"); self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
