#!/usr/bin/env python3
"""Generate tests/fixtures/netbox.json: a NetBox-shaped data set for the dual transit design.

Addresses are documentation/example values. Run this after changing the shape.
"""
import json, os

def dev(i, name, role, site, lo4, lo6, short=None, primary=False, hub=None, mgmt=None, extra=None):
    cf = {"loopback_ip": lo4 + "/32" if lo4 else None, "loopback_ip6": lo6 + "/128" if lo6 else None,
          "bgp_primary": primary, "hub": hub, "short_name": short, "mesh_ip": None,
          "ansible_user": "ansible"}
    cf.update(extra or {})
    return {"id": i, "name": name, "role": {"slug": role}, "site": {"slug": site},
            "status": {"value": "active"}, "primary_ip4": {"address": (mgmt or "198.18.0.%d" % i) + "/32"},
            "custom_fields": cf}

devices = [
    dev(1, "vultr-transit", "transit-router", "vultr-chi", "10.255.255.1", "fd17:290::1", "vultr", primary=True, mgmt="203.0.113.10", extra={"bgp_role": "rr"}),
    dev(2, "nl-transit", "transit-router", "nl-1", "10.255.255.2", "fd17:290::2", "nl", extra={"bgp_role": "rr"}),
    dev(3, "vultr-ix1", "ix-router", "vultr-chi", "10.255.255.11", "fd17:290::11", "vix1", hub="vultr-transit"),
    dev(4, "nl-ix1", "ix-router", "nl-1", "10.255.255.12", "fd17:290::12", "nix1", hub="nl-transit"),
    dev(5, "mon-vultr", "monitoring", "vultr-chi", None, None, "mon"),
]

def sess(i, name, device, local, remote, remote_as, cf, status="active"):
    return {"id": i, "name": name, "device": {"id": 0, "name": device},
            "local_address": {"address": local + ("/32" if ":" not in local else "/128")} if local else None,
            "remote_address": {"address": remote + ("/32" if ":" not in remote else "/128")},
            "local_as": {"asn": 17290}, "remote_as": {"asn": remote_as}, "peer_group": None,
            "status": {"value": status}, "custom_fields": cf}

sessions = [
    sess(1, "vultr-v4", "vultr-transit", "203.0.113.10", "169.254.169.254", 64515,
         {"session_class": "transit", "bgp_password": "vultrSecret1", "multihop_ttl": 2,
          "allow_private_as": True, "neighbor_interface": "enp1s0"}),
    sess(2, "vultr-v6", "vultr-transit", "2001:db8:10::10", "2001:19f0:ffff::1", 64515,
         {"session_class": "transit", "bgp_password": "vultrSecret1", "multihop_ttl": 2, "allow_private_as": True}),
    sess(3, "nl-v4", "nl-transit", "198.51.100.2", "198.51.100.1", 205941,
         {"session_class": "transit", "export_prepend": 3, "bgp_password": "nlSecret1"}),
    sess(4, "nl-v6", "nl-transit", "2001:db8:100::2", "2001:db8:100::1", 205941,
         {"session_class": "transit", "export_prepend": 3, "bgp_password": "nlSecret1"}),
    sess(5, "ix1-rs1-v4", "vultr-ix1", "192.0.2.10", "192.0.2.1", 64496,
         {"session_class": "ix-rs", "max_prefix": 150000}),
    sess(6, "ix1-rs1-v6", "vultr-ix1", "2001:db8:ffff::10", "2001:db8:ffff::1", 64496,
         {"session_class": "ix-rs", "max_prefix": 60000}),
    sess(7, "ix1-peer-v4", "vultr-ix1", "192.0.2.10", "192.0.2.77", 64497,
         {"session_class": "ix-bilateral", "irr_as_set": "AS-EXAMPLE", "max_prefix": 100}),
    sess(8, "ix1-planned", "vultr-ix1", "192.0.2.10", "192.0.2.88", 64498,
         {"session_class": "ix-bilateral"}, status="planned"),
    sess(9, "nlix-rs1-v4", "nl-ix1", "203.0.113.200", "203.0.113.1", 64499,
         {"session_class": "ix-rs"}),
]

pl = [{"id": 1, "name": "OWN-V4", "family": {"value": 4}}, {"id": 2, "name": "OWN-V6", "family": {"value": 6}}]
rules = [
    {"id": 1, "index": 10, "action": "permit", "prefix_list": {"id": 1, "name": "OWN-V4"}, "prefix": None, "prefix_custom": "192.0.2.0/24", "ge": None, "le": None},
    {"id": 2, "index": 20, "action": "permit", "prefix_list": {"id": 1, "name": "OWN-V4"}, "prefix": None, "prefix_custom": "198.18.0.0/22", "ge": 22, "le": 24},
    {"id": 3, "index": 10, "action": "permit", "prefix_list": {"id": 2, "name": "OWN-V6"}, "prefix": None, "prefix_custom": "2001:db8:5000::/36", "ge": None, "le": 48},
]
out = {"devices": devices, "sessions": sessions, "prefix_lists": pl, "prefix_list_rules": rules, "tagged_prefixes": []}
path = os.path.join(os.path.dirname(__file__), "fixtures", "netbox.json")
json.dump(out, open(path, "w"), indent=1)
print("wrote", path)
