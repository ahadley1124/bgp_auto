"""End-to-end test of the playbooks against a fake NetBox, real `bird -p`, and fake birdc/systemctl.

Covers: validate.yml, rollout order (primary first), idempotence (no reconfigure when nothing
changed), and rollback + stop-the-rollout when the health check fails.
Skipped when ansible-playbook or bird are not installed.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest, urllib.request, hashlib

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
HAVE = shutil.which("ansible-playbook") and shutil.which("bird")
HOSTS = ["vultr-transit", "nl-transit", "nl-ix1", "vultr-ix1", "mon-vultr"]


def digest(path):
    h = hashlib.sha256()
    for dp, _, fs in sorted(os.walk(path)):
        for f in sorted(fs):
            h.update(open(os.path.join(dp, f), "rb").read())
    return h.hexdigest()


@unittest.skipUnless(HAVE, "needs ansible-playbook and bird")
class AnsibleFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bgptest")
        raw = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))
        for i, d in enumerate(raw["devices"]):
            d["custom_fields"]["mesh_ip"] = "100.96.0.%d/32" % (10 + i)
        fx = os.path.join(cls.tmp, "netbox.json"); json.dump(raw, open(fx, "w"))
        cls.srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_netbox.py"), "--fixture", fx],
                                   stdout=subprocess.PIPE, text=True)
        cls.url = "http://127.0.0.1:%s" % cls.srv.stdout.readline().strip()
        bindir = os.path.join(cls.tmp, "bin"); os.makedirs(bindir)
        for h in HOSTS:
            os.symlink(os.path.join(HERE, "shims", "birdc"), os.path.join(bindir, "birdc-" + h))
        shutil.copy(os.path.join(HERE, "shims", "systemctl"), os.path.join(bindir, "systemctl"))
        cls.env = dict(os.environ, NETBOX_URL=cls.url, NETBOX_TOKEN="testtoken", BGPTEST_ROOT=cls.tmp,
                       PATH=bindir + os.pathsep + os.environ["PATH"], ANSIBLE_FORCE_COLOR="0",
                       ANSIBLE_CONFIG=os.path.join(ROOT, "ansible.cfg"))
        cls.tdir = os.path.join(cls.tmp, "work")

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate(); cls.srv.wait(); cls.srv.stdout.close(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def ansible(self, playbook, *extra):
        t = self.tmp
        ev = {"netbox_model_irr_file": os.path.join(HERE, "fixtures", "irr.json"),
              "netbox_model_work_dir": self.tdir, "bgp_connection": "local", "ansible_become": False,
              "bgp_mesh": False, "bird_install": False, "bgp_manage_rpki": False,
              "bird_conf_dir": t + "/{{ inventory_hostname }}/etc-bird",
              "bgp_state_dir": t + "/{{ inventory_hostname }}/state",
              "bird_staging_dir": t + "/{{ inventory_hostname }}/staging",
              "bird_backup_dir": t + "/{{ inventory_hostname }}/backup",
              "bgp_auto_dir": t + "/{{ inventory_hostname }}/bgp-auto",
              "bgp_health_bin": t + "/{{ inventory_hostname }}/health",
              "birdc_bin": t + "/bin/birdc-{{ inventory_hostname }}",
              "bird_health_wait": 3, "validate_dir": t + "/validate"}
        cmd = ["ansible-playbook", "-i", "localhost,", os.path.join(ROOT, "playbooks", playbook),
               "-e", json.dumps(ev)] + list(extra)
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env, cwd=ROOT)

    def calls(self):
        p = os.path.join(self.tmp, "birdc.log")
        return [l.split(None, 2) for l in open(p)] if os.path.exists(p) else []

    def test_1_validate(self):
        r = self.ansible("validate.yml"); self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
        for h in ["vultr-transit", "nl-transit", "nl-ix1", "vultr-ix1"]:
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "validate", h, "bird.conf")), h)

    def test_2_deploy_order_and_idempotence(self):
        r = self.ansible("deploy.yml", "--tags", "bird"); self.assertEqual(r.returncode, 0, r.stdout[-4000:] + r.stderr[-2000:])
        hosts = [c[1] for c in self.calls() if c[2].startswith("configure timeout")]
        self.assertEqual(hosts, ["vultr-transit", "nl-transit", "nl-ix1", "vultr-ix1"], "primary first, IX last")
        for h in HOSTS[:4]:
            self.assertTrue(os.path.exists(os.path.join(self.tmp, h, "etc-bird", "bird.conf")), h)
            self.assertEqual(digest(os.path.join(self.tmp, h, "etc-bird", "generated")),
                             digest(os.path.join(self.tmp, h, "staging", "generated")))
        confirms = [c for c in self.calls() if c[2].startswith("configure confirm")]
        self.assertEqual(len(confirms), 4)
        before = len(self.calls())
        r = self.ansible("deploy.yml", "--tags", "bird"); self.assertEqual(r.returncode, 0, r.stdout[-3000:])
        new = [c for c in self.calls()[before:] if c[2].startswith("configure")]
        self.assertEqual(new, [], "an unchanged NetBox must not reconfigure BIRD")

    def test_3_unhealthy_stage_rolls_back_and_stops(self):
        live = os.path.join(self.tmp, "nl-transit", "etc-bird")
        old = digest(live)
        req = urllib.request.Request(self.url + "/api/plugins/bgp/session/4/", method="PATCH",
              data=json.dumps({"custom_fields": {"max_prefix": 1234567}}).encode(),
              headers={"Authorization": "Token testtoken", "Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
        open(os.path.join(self.tmp, "FAIL_nl-transit"), "w").close()
        before = len(self.calls())
        r = self.ansible("deploy.yml", "--tags", "bird")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("previous config kept", r.stdout + r.stderr)
        later = self.calls()[before:]
        self.assertTrue(any(c[1] == "nl-transit" and c[2].startswith("configure undo") for c in later))
        self.assertFalse(any(c[1] in ("nl-ix1", "vultr-ix1") for c in later), "rollout must stop")
        self.assertEqual(digest(live), old, "live config restored")


if __name__ == "__main__":
    unittest.main()
