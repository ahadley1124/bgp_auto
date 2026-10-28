import json, os, sys, unittest
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "filter_plugins"))
import bgp_auto as b

RAW = json.load(open(os.path.join(HERE, "fixtures", "netbox.json")))

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.m = b.build_model(RAW, {})

    def test_no_errors(self):
        self.assertEqual(self.m["errors"], [])

    def test_primary_and_order(self):
        self.assertEqual(self.m["primary"], "vultr-transit")
        self.assertEqual(self.m["order"][:2], ["vultr-transit", "nl-transit"])
        self.assertEqual(self.m["order"][-1], "mon-vultr")

    def test_ibgp_is_loopback_hub_and_spoke(self):
        v = self.m["nodes"]["vultr-transit"]
        peers = {p["peer"]: p for p in v["ibgp"]}
        self.assertEqual(set(peers), {"nl-transit", "vultr-ix1"})
        self.assertTrue(peers["vultr-ix1"]["client"])
        self.assertFalse(peers["nl-transit"]["client"])
        self.assertEqual(peers["nl-transit"]["lo4"], "10.255.255.2")
        ix = self.m["nodes"]["vultr-ix1"]
        self.assertEqual([p["peer"] for p in ix["ibgp"]], ["vultr-transit"])
        self.assertEqual([t["ifname"] for t in ix["tunnels"]], ["gre-vultr"])

    def test_stage_expectations(self):
        # primary is first, so it cannot require iBGP to peers that are not deployed yet
        self.assertEqual(b.node_expected(self.m["nodes"]["vultr-transit"]), ["tr_vultr_v4", "tr_vultr_v6"])
        nl = b.node_expected(self.m["nodes"]["nl-transit"])
        self.assertIn("ibgp4_vultr_transit", nl)
        self.assertIn("ibgp6_vultr_transit", nl)

    def test_session_defaults(self):
        s = {x["label"]: x for x in self.m["nodes"]["nl-transit"]["sessions"]}
        self.assertEqual(s["nl-v4"]["prepend"], 3)
        self.assertEqual(s["nl-v4"]["local_pref"], 100)
        v = {x["label"]: x for x in self.m["nodes"]["vultr-transit"]["sessions"]}
        self.assertEqual(v["vultr-v4"]["local_pref"], 100)  # equal local-pref: AS path decides outbound
        self.assertEqual(v["vultr-v4"]["prepend"], 0)       # no prepend: preferred inbound
        self.assertEqual(v["vultr-v4"]["multihop"], 2)
        self.assertEqual(v["vultr-v4"]["max_prefix"], 1400000)

    def test_planned_session_not_rendered(self):
        labels = [s["label"] for s in self.m["nodes"]["vultr-ix1"]["sessions"]]
        self.assertNotIn("ix1-planned", labels)

    def test_irr_only_for_bilateral(self):
        self.assertEqual(self.m["irr_sets"], ["AS-EXAMPLE"])

    def test_originate_only_on_primary(self):
        self.assertTrue(self.m["nodes"]["vultr-transit"]["originate"])
        self.assertFalse(self.m["nodes"]["nl-transit"]["originate"])

    def test_rpki_servers(self):
        self.assertEqual(self.m["nodes"]["vultr-transit"]["rtr_servers"], [{"host": "127.0.0.1", "port": 3323}])
        self.assertEqual(self.m["nodes"]["vultr-ix1"]["rtr_servers"], [{"host": "10.255.255.1", "port": 3323}])

    def test_prefix_sets(self):
        self.assertEqual(b.bird_prefix_set(self.m["own"]["4"]), "192.0.2.0/24, 198.18.0.0/22{22,24}")
        self.assertEqual(b.bird_prefix_set(self.m["own"]["4"], cover=True), "192.0.2.0/24{24,32}, 198.18.0.0/22{22,32}")
        self.assertEqual(b.bird_prefix_set(self.m["own"]["6"]), "2001:db8:5000::/36{36,48}")

class ValidationTests(unittest.TestCase):
    def test_missing_primary(self):
        raw = json.loads(json.dumps(RAW))
        raw["devices"][0]["custom_fields"]["bgp_primary"] = False
        self.assertTrue(any("bgp_primary" in e for e in b.build_model(raw, {})["errors"]))

    def test_ix_without_hub(self):
        raw = json.loads(json.dumps(RAW))
        raw["devices"][2]["custom_fields"]["hub"] = None
        self.assertTrue(any("hub" in e for e in b.build_model(raw, {})["errors"]))

    def test_bad_password_rejected(self):
        raw = json.loads(json.dumps(RAW))
        raw["sessions"][0]["custom_fields"]["bgp_password"] = 'bad"pw'
        self.assertTrue(any("password" in e for e in b.build_model(raw, {})["errors"]))

    def test_long_ifname(self):
        raw = json.loads(json.dumps(RAW))
        raw["devices"][0]["custom_fields"]["short_name"] = "waytoolongshortname"
        self.assertTrue(any("15 chars" in e for e in b.build_model(raw, {})["errors"]))

    def test_tagged_prefix_fallback(self):
        raw = json.loads(json.dumps(RAW))
        raw["prefix_lists"] = []; raw["prefix_list_rules"] = []
        raw["tagged_prefixes"] = [{"prefix": "192.0.2.0/24", "status": {"value": "active"}}]
        m = b.build_model(raw, {})
        self.assertEqual(m["own"]["4"][0]["prefix"], "192.0.2.0/24")

class IrrTests(unittest.TestCase):
    def test_defines(self):
        m = b.build_model(RAW, {})
        sess = m["nodes"]["vultr-ix1"]["sessions"]
        text, empty = b.irr_defines({"AS-EXAMPLE": {"4": [{"prefix": "203.0.113.0/24", "exact": True},
            {"prefix": "198.51.100.0/22", "exact": False, "greater-equal": 22, "less-equal": 24}]}}, sess)
        self.assertIn("define IRR_AS_EXAMPLE_V4 = [ 203.0.113.0/24, 198.51.100.0/22{22,24} ];", text)
        self.assertEqual(empty, [])
        text, empty = b.irr_defines({}, sess)
        self.assertEqual(empty, ["IRR_AS_EXAMPLE_V4"])

if __name__ == "__main__":
    unittest.main()


class TunnelPlanTests(unittest.TestCase):
    def setUp(self):
        raw = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "netbox.json")))
        for i, d in enumerate(raw["devices"]):
            d["custom_fields"]["mesh_ip"] = "100.96.0.%d/32" % (10 + i)
        self.model = b.build_model(raw, {})

    def test_plan_is_symmetric_and_stable(self):
        a = b.tunnel_plan(self.model, "vultr-transit")
        c = b.tunnel_plan(self.model, "vultr-ix1")
        self.assertEqual(a["errors"], [])
        ta = {t["peer"]: t for t in a["tunnels"]}["vultr-ix1"]
        tc = {t["peer"]: t for t in c["tunnels"]}["vultr-transit"]
        self.assertEqual(ta["local"], tc["remote"])
        self.assertNotEqual(ta["addresses"], tc["addresses"])
        self.assertEqual(ta["addresses"][0].split("/")[0][:-1], tc["addresses"][0].split("/")[0][:-1])
        self.assertIn("10.255.255.1/32", a["loopbacks"])

    def test_missing_mesh_ip_reported(self):
        self.model["nodes"]["nl-transit"]["mesh_ip"] = None
        self.assertTrue(b.tunnel_plan(self.model, "vultr-transit")["errors"])


class PathAsnTests(unittest.TestCase):
    def test_vultr_sessions_default_to_as20473_in_paths(self):
        raw = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "netbox.json")))
        m = b.build_model(raw, {})
        s = {x["label"]: x for x in m["nodes"]["vultr-transit"]["sessions"]}
        self.assertEqual(s["vultr-v4"]["path_asn"], 20473)
        n = {x["label"]: x for x in m["nodes"]["nl-transit"]["sessions"]}
        self.assertEqual(n["nl-v4"]["path_asn"], 205941)


class V6BlockTests(unittest.TestCase):
    def raw(self):
        raw = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "netbox.json")))
        return raw

    def setCF(self, raw, name, **cf):
        for d in raw["devices"]:
            if d["name"] == name:
                d["custom_fields"].update(cf)

    def test_blocks_and_management_net(self):
        raw = self.raw()
        self.setCF(raw, "vultr-ix1", ipv6_block="2001:db8:5001::/48")
        self.setCF(raw, "mon-vultr", mgmt_parent="vultr-ix1", mgmt_ipv6="2001:db8:5001::10")
        m = b.build_model(raw, {})
        self.assertEqual(m["errors"], [])
        ix = m["nodes"]["vultr-ix1"]
        self.assertEqual(ix["mgmt_net"], "2001:db8:5001::/64")
        self.assertEqual(ix["mgmt_addr"], "2001:db8:5001::1/64")
        self.assertEqual(m["nodes"]["mon-vultr"]["mgmt_addr"], "2001:db8:5001::10/64")
        self.assertEqual(ix["mgmt_members"], [{"name": "mon-vultr", "addr": "2001:db8:5001::10"}])

    def test_rejects_bad_blocks(self):
        raw = self.raw()
        self.setCF(raw, "vultr-ix1", ipv6_block="2001:db8:5001::/64")       # not a /48
        self.setCF(raw, "nl-ix1", ipv6_block="2001:db8:ffff::/48")              # outside OWN-V6
        self.setCF(raw, "vultr-transit", ipv6_block="2001:db8:5002::/48")
        self.setCF(raw, "nl-transit", ipv6_block="2001:db8:5002::/48")      # duplicate
        errs = " | ".join(b.build_model(raw, {})["errors"])
        self.assertIn("must be an IPv6 /48", errs)
        self.assertIn("not inside the own IPv6 prefixes", errs)
        self.assertIn("overlapping", errs)

    def addReassigned(self, raw, prefix):
        raw["prefix_lists"].append({"id": 9, "name": "REASSIGNED", "family": {"value": 6}})
        raw["prefix_list_rules"].append({"id": 99, "index": 10, "action": "permit",
            "prefix_list": {"id": 9, "name": "REASSIGNED"}, "prefix": None,
            "prefix_custom": prefix, "ge": None, "le": None})

    def test_reassigned_prefix_is_excluded(self):
        raw = self.raw()
        self.addReassigned(raw, "2001:db8:5ffe::/48")
        self.setCF(raw, "vultr-ix1", ipv6_block="2001:db8:5001::/48")
        m = b.build_model(raw, {})
        self.assertEqual(m["errors"], [])
        self.assertEqual(m["nodes"]["vultr-ix1"]["reassigned_v6"][0]["prefix"], "2001:db8:5ffe::/48")
        self.setCF(raw, "nl-ix1", ipv6_block="2001:db8:5ffe::/48")
        self.assertIn("overlaps reassigned", " ".join(b.build_model(raw, {})["errors"]))
        raw2 = self.raw()
        self.addReassigned(raw2, "2001:db9::/48")
        self.assertIn("not inside our own", " ".join(b.build_model(raw2, {})["errors"]))

    def test_child_must_be_inside_parent_net(self):
        raw = self.raw()
        self.setCF(raw, "vultr-ix1", ipv6_block="2001:db8:5001::/48")
        self.setCF(raw, "mon-vultr", mgmt_parent="vultr-ix1", mgmt_ipv6="2001:db8:5001:1::10")
        self.assertIn("not inside", " ".join(b.build_model(raw, {})["errors"]))

    def test_tunnel_plan_has_management_interface(self):
        raw = self.raw()
        for i, d in enumerate(raw["devices"]):
            d["custom_fields"]["mesh_ip"] = "100.96.0.%d/32" % (10 + i)
        self.setCF(raw, "vultr-ix1", ipv6_block="2001:db8:5001::/48")
        plan = b.tunnel_plan(b.build_model(raw, {}), "vultr-ix1")
        self.assertEqual(plan["dummies"], [{"name": "mgmt0", "addresses": ["2001:db8:5001::1/64"]}])


class DirectAndMeshTunnelTests(unittest.TestCase):
    def setUp(self):
        raw = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "netbox.json")))
        for i, d in enumerate(raw["devices"]):
            d["custom_fields"]["mesh_ip"] = "100.96.0.%d/32" % (10 + i)
        self.model = b.build_model(raw, {})

    def test_transit_pair_has_direct_and_mesh(self):
        plan = b.tunnel_plan(self.model, "vultr-transit")
        to_nl = [t for t in plan["tunnels"] if t["peer"] == "nl-transit"]
        self.assertEqual(sorted(t["kind"] for t in to_nl), ["direct", "mesh"])
        direct = [t for t in to_nl if t["kind"] == "direct"][0]
        mesh = [t for t in to_nl if t["kind"] == "mesh"][0]
        self.assertEqual((direct["encap"], direct["name"]), ("gre", "gre-nl"))
        self.assertEqual((mesh["encap"], mesh["name"]), ("fou", "grm-nl"))
        self.assertTrue(direct["addresses"][0].startswith("10.255."))
        self.assertTrue(mesh["addresses"][0].startswith("10.254."))
        self.assertEqual(direct["local"], "203.0.113.10")

    def test_ix_router_has_only_a_direct_tunnel_and_needs_no_mesh(self):
        node = self.model["nodes"]["vultr-ix1"]
        self.assertFalse(node["use_mesh"])
        self.assertEqual([t["kind"] for t in node["tunnels"]], ["direct"])
        self.assertTrue(self.model["nodes"]["vultr-transit"]["use_mesh"])
        self.assertTrue(self.model["nodes"]["mon-vultr"]["use_mesh"])

    def test_ix_plan_works_without_a_mesh_ip(self):
        self.model["nodes"]["vultr-ix1"]["mesh_ip"] = None
        self.assertEqual(b.tunnel_plan(self.model, "vultr-ix1")["errors"], [])

    def test_direct_needs_public_address(self):
        raw = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "netbox.json")))
        for d in raw["devices"]:
            if d["name"] == "vultr-ix1":
                d["primary_ip4"] = None
        self.assertIn("direct GRE needs", " ".join(b.build_model(raw, {})["errors"]))

    def test_exporters_follow_mesh_or_public(self):
        n = self.model["nodes"]
        self.assertEqual(n["vultr-transit"]["exporter_addr"], n["vultr-transit"]["mesh_ip"])
        self.assertFalse(n["vultr-transit"]["exporter_restricted"])
        self.assertEqual(n["vultr-ix1"]["exporter_addr"], n["vultr-ix1"]["mgmt_ip"])
        self.assertTrue(n["vultr-ix1"]["exporter_restricted"])
        self.assertEqual(self.model["monitoring_ips"], [n["mon-vultr"]["mgmt_ip"]])
