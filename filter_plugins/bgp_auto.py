"""Turn raw NetBox API output into the per-router model the roles render.

Everything here is pure Python so it can be unit tested without NetBox or
Ansible (see tests/test_bgp_auto.py).

Topology (hub and spoke, OSPF underlay, loopback iBGP):

  * Every router has an IPv4 loopback (and optionally an IPv6 loopback).
  * Routers are joined by GRE tunnels (carried over Cloudflare Mesh). OSPFv2 and
    OSPFv3 run over those tunnels and announce the loopbacks.
  * iBGP sessions are loopback to loopback, so they ride on the OSPF routes.
  * The transit routers peer with each other and are both route reflectors.
    Each IX router tunnels to its hub transit router and is that hub's RR client.
  * The primary transit router (custom field bgp_primary) is deployed first.
"""
from __future__ import annotations

import ipaddress
import re

ROLE_TRANSIT = "transit-router"
ROLE_IX = "ix-router"
ROLE_MON = "monitoring"
ROUTER_ROLES = (ROLE_TRANSIT, ROLE_IX)
MANAGED_ROLES = (ROLE_TRANSIT, ROLE_IX, ROLE_MON)

SESSION_CLASSES = ("transit", "ix-rs", "ix-bilateral", "customer")

DEFAULT_POLICY = {
    "own_asn": 17290,
    "transit_asns": [64515, 205941],
    "own_prefix_lists": {"4": "OWN-V4", "6": "OWN-V6"},
    "own_prefix_tag": "bgp-originate",
    "device_statuses": ["active"],
    "local_pref": {"customer": 300, "ix-bilateral": 220, "ix-rs": 200, "transit": 100},
    "max_prefix": {
        "transit": {"4": 1400000, "6": 400000},
        "ix-rs": {"4": 250000, "6": 100000},
        "ix-bilateral": {"4": 5000, "6": 5000},
        "customer": {"4": 1000, "6": 1000},
    },
    "max_prefix_action": {
        "transit": "restart",
        "ix-rs": "disable",
        "ix-bilateral": "disable",
        "customer": "disable",
    },
    "enforce_first_as": {"transit": False, "ix-rs": False, "ix-bilateral": True, "customer": True},
    "irr_filter": {"transit": False, "ix-rs": False, "ix-bilateral": True, "customer": True},
    "export_prepend": {"transit": 0, "ix-rs": 0, "ix-bilateral": 0, "customer": 0},
    "ibgp_bfd": False,
    "bfd_interval_ms": 1000,
    "bfd_multiplier": 5,
    "ospf_hello": 3,
    "ospf_dead": 12,
    "ospf_cost": 10,
    "rtr_port": 3323,
    "gre_ifname_prefix": "gre-",
    "ospf_enabled": True,
}

_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def _val(x):
    """NetBox returns choices as {"value": .., "label": ..}; accept both."""
    if isinstance(x, dict):
        return x.get("value")
    return x


def _strip(addr):
    if addr is None:
        return None
    if isinstance(addr, dict):
        addr = addr.get("address")
    if not addr:
        return None
    return str(addr).split("/")[0].strip() or None


def _bool(x, default=False):
    if x is None or x == "":
        return default
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(x)


def _int(x, default=None):
    if x is None or x == "":
        return default
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _ident(name):
    s = _NAME_RE.sub("_", str(name))
    if not re.match(r"[A-Za-z_]", s):
        s = "_" + s
    return s


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _family(addr):
    return ipaddress.ip_address(addr).version


def _cf(obj):
    return (obj or {}).get("custom_fields") or {}


# ---------------------------------------------------------------- prefixes


def _rule_prefix(rule):
    pfx = rule.get("prefix")
    if isinstance(pfx, dict):
        pfx = pfx.get("prefix")
    return pfx or rule.get("prefix_custom")


def _prefix_entry(prefix, ge=None, le=None):
    net = ipaddress.ip_network(prefix, strict=False)
    ge = _int(ge)
    le = _int(le)
    return {
        "prefix": str(net),
        "family": net.version,
        "len": net.prefixlen,
        "ge": ge,
        "le": le,
    }


def _collect_prefix_lists(raw, errors):
    """name -> list of prefix entries (permit rules only, in index order)."""
    lists = {}
    by_id = {}
    for pl in raw.get("prefix_lists") or []:
        by_id[pl.get("id")] = pl.get("name")
        lists.setdefault(pl.get("name"), [])
    rules = sorted(raw.get("prefix_list_rules") or [], key=lambda r: _int(r.get("index"), 0))
    for rule in rules:
        plref = rule.get("prefix_list")
        plid = plref.get("id") if isinstance(plref, dict) else plref
        pname = plref.get("name") if isinstance(plref, dict) and plref.get("name") else by_id.get(plid)
        if pname is None:
            continue
        if _val(rule.get("action")) not in (None, "permit"):
            continue
        pfx = _rule_prefix(rule)
        if not pfx:
            continue
        try:
            lists.setdefault(pname, []).append(_prefix_entry(pfx, rule.get("ge"), rule.get("le")))
        except ValueError:
            errors.append("prefix list %s: bad prefix %r" % (pname, pfx))
    return lists


def _own_prefixes(raw, cfg, lists, errors):
    own = {"4": [], "6": []}
    for fam in ("4", "6"):
        name = cfg["own_prefix_lists"].get(fam)
        if name in lists and lists[name]:
            own[fam] = [e for e in lists[name] if str(e["family"]) == fam]
    if not own["4"] and not own["6"]:
        for p in raw.get("tagged_prefixes") or []:
            if _val(p.get("status")) not in (None, "active"):
                continue
            try:
                e = _prefix_entry(p.get("prefix"))
            except ValueError:
                errors.append("tagged prefix: bad prefix %r" % p.get("prefix"))
                continue
            own[str(e["family"])].append(e)
    return own


def bird_prefix_set(entries, cover=False):
    """Render prefix entries as the body of a BIRD prefix set (comma separated).

    cover=True matches the prefix and anything more specific (used to reject our
    own space when it is received from outside).
    """
    out = []
    for e in entries:
        maxlen = 32 if e["family"] == 4 else 128
        if cover:
            out.append("%s{%d,%d}" % (e["prefix"], e["len"], maxlen))
        elif e.get("ge") or e.get("le"):
            ge = e.get("ge") or e["len"]
            le = e.get("le") or maxlen
            out.append("%s{%d,%d}" % (e["prefix"], ge, le))
        else:
            out.append(e["prefix"])
    return ", ".join(out)


# ---------------------------------------------------------------- devices


def _short_name(dev):
    cf = _cf(dev)
    short = cf.get("short_name") or re.sub(r"[^a-z0-9]", "", dev["name"].lower())[:10]
    return short


def _device_view(d, cfg, errors):
    cf = _cf(d)
    r = d.get("role") or d.get("device_role")  # NetBox 4 uses role, older uses device_role
    role = r.get("slug") if isinstance(r, dict) else r
    site = (d.get("site") or {}).get("slug")
    rtr = cf.get("rpki_rtr") or ""
    if isinstance(rtr, list):
        rtr = ",".join(str(x) for x in rtr)
    return {
        "name": d["name"],
        "id": d.get("id"),
        "role": role,
        "site": site,
        "status": _val(d.get("status")),
        "mgmt_ip": _strip(d.get("primary_ip4")) or _strip(d.get("primary_ip")),
        "loopback4": _strip(cf.get("loopback_ip")),
        "loopback6": _strip(cf.get("loopback_ip6")),
        "bgp_role": cf.get("bgp_role") or "client",
        "primary": _bool(cf.get("bgp_primary")),
        "hub": cf.get("hub") or None,
        "mesh_ip": _strip(cf.get("mesh_ip")),
        "short": _short_name(d),
        "rtr_override": [x.strip() for x in str(rtr).split(",") if x.strip()],
        "rpki_validator": cf.get("rpki_validator"),
        "originate": cf.get("originate_prefixes"),
        "ansible_user": cf.get("ansible_user"),
        "v6_block_raw": (cf.get("ipv6_block") or "").strip() or None,
        "mgmt_parent": (cf.get("mgmt_parent") or "").strip() or None,
        "mgmt_ipv6_raw": _strip(cf.get("mgmt_ipv6")),
    }


# ---------------------------------------------------------------- sessions


def _session_view(s, cfg, errors, lists):
    cf = _cf(s)
    dev = (s.get("device") or {}).get("name")
    remote = _strip(s.get("remote_address"))
    local = _strip(s.get("local_address"))
    label = s.get("name") or ("session-%s" % s.get("id"))
    if not remote:
        errors.append("session %s: no remote address" % label)
        return None
    try:
        fam = _family(remote)
    except ValueError:
        errors.append("session %s: bad remote address %r" % (label, remote))
        return None
    if local:
        try:
            if _family(local) != fam:
                errors.append("session %s: local and remote address families differ" % label)
                return None
        except ValueError:
            errors.append("session %s: bad local address %r" % (label, local))
            return None
    local_as = _int((s.get("local_as") or {}).get("asn"), cfg["own_asn"])
    remote_as = _int((s.get("remote_as") or {}).get("asn"))
    if remote_as is None:
        errors.append("session %s: no remote AS" % label)
        return None
    cls = _val(cf.get("session_class"))
    if not cls:
        cls = "transit" if remote_as in cfg["transit_asns"] else "ix-bilateral"
    if cls not in SESSION_CLASSES:
        errors.append("session %s: session_class %r not in %s" % (label, cls, SESSION_CLASSES))
        return None
    fk = str(fam)
    password = s.get("password") or cf.get("bgp_password") or None
    if password and (not re.match(r"^[\x21-\x7e]+$", password) or '"' in password or "\\" in password):
        errors.append("session %s: password has characters that cannot be rendered safely" % label)
        return None
    irr_flag = cf.get("irr_filter")
    irr = _bool(irr_flag, cfg["irr_filter"][cls])
    irr_set = (cf.get("irr_as_set") or "").strip() or ("AS%d" % remote_as)
    status = _val(s.get("status")) or "active"
    pg = s.get("peer_group")
    imp_list = cf.get("import_prefix_list") or None
    if imp_list and imp_list not in lists:
        errors.append("session %s: import_prefix_list %r not found in NetBox" % (label, imp_list))
        imp_list = None
    max_prefix = _int(cf.get("max_prefix"), cfg["max_prefix"][cls][fk])
    return {
        "id": s.get("id"),
        "label": label,
        "device": dev,
        "family": fam,
        "local_address": local,
        "remote_address": remote,
        "local_as": local_as,
        "remote_as": remote_as,
        "peer_group": pg.get("name") if isinstance(pg, dict) else None,
        "class": cls,
        "enabled": status == "active",
        "status": status,
        "password": password,
        "multihop": _int(cf.get("multihop_ttl")),
        "max_prefix": max_prefix,
        "max_prefix_action": cfg["max_prefix_action"][cls],
        "local_pref": _int(cf.get("local_pref"), cfg["local_pref"][cls]),
        "prepend": _int(cf.get("export_prepend"), cfg["export_prepend"][cls]),
        "enforce_first_as": _bool(cf.get("enforce_first_as"), cfg["enforce_first_as"][cls]),
        "allow_private_as": _bool(cf.get("allow_private_as")),
        "accept_default": _bool(cf.get("accept_default")),
        "graceful_shutdown": _bool(cf.get("graceful_shutdown")),
        "bfd": _bool(cf.get("bfd")),
        "neighbor_interface": cf.get("neighbor_interface") or None,
        "irr_filter": irr,
        "irr_as_set": irr_set if irr else None,
        "irr_name": ("IRR_%s_V%d" % (_ident(irr_set), fam)) if irr else None,
        "import_prefix_list": imp_list,
    }


def _unique_names(sessions):
    seen = {}
    prefix = {"transit": "tr", "ix-rs": "rs", "ix-bilateral": "bi", "customer": "cu"}
    for s in sessions:
        base = "%s_%s" % (prefix[s["class"]], _ident(s["label"]))
        n = seen.get(base, 0)
        seen[base] = n + 1
        s["pname"] = base if n == 0 else "%s_%d" % (base, n + 1)
        s["proto"] = s["pname"]


# ---------------------------------------------------------------- topology


def _rank(dev):
    if dev["primary"]:
        return 1
    return {ROLE_TRANSIT: 2, ROLE_IX: 3, ROLE_MON: 4}.get(dev["role"], 9)


def _peers(devs, errors):
    """name -> list of {peer, client}. client means the PEER is my RR client."""
    peers = {d: [] for d in devs}
    transits = sorted(n for n, d in devs.items() if d["role"] == ROLE_TRANSIT)
    for a in transits:
        for b in transits:
            if a != b:
                peers[a].append({"peer": b, "client": False})
    for n, d in sorted(devs.items()):
        if d["role"] != ROLE_IX:
            continue
        hub = d["hub"]
        if not hub or hub not in devs or devs[hub]["role"] != ROLE_TRANSIT:
            errors.append("ix router %s: custom field 'hub' must name a transit-router (got %r)" % (n, hub))
            continue
        peers[n].append({"peer": hub, "client": False})
        peers[hub].append({"peer": n, "client": True})
    return peers


def _plan_v6_blocks(devs, own6, errors):
    """Give each device its /48 and the management /64 (the first /64 of that /48).

    A router's management address is <first /64>::1. Other VMs that sit in the same IX take an
    address from their parent's management /64: set mgmt_parent (device name) and mgmt_ipv6.
    Fills v6_block, mgmt_net, mgmt_addr, mgmt_members on every device dict.
    """
    own_nets = []
    for e in own6:
        try:
            own_nets.append(ipaddress.ip_network(e["prefix"], strict=False))
        except ValueError:
            pass
    blocks = {}
    for n, d in devs.items():
        d.update({"v6_block": None, "mgmt_net": None, "mgmt_addr": None, "mgmt_members": []})
        raw = d["v6_block_raw"]
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=True)
        except ValueError:
            errors.append("%s: ipv6_block %r is not a valid IPv6 prefix with host bits clear" % (n, raw))
            continue
        if net.version != 6 or net.prefixlen != 48:
            errors.append("%s: ipv6_block %s must be an IPv6 /48" % (n, raw))
            continue
        if own_nets and not any(net.subnet_of(o) for o in own_nets if o.version == 6):
            errors.append("%s: ipv6_block %s is not inside the own IPv6 prefixes (OWN-V6)" % (n, net))
            continue
        for other, onet in blocks.items():
            if net.overlaps(onet):
                errors.append("%s and %s have overlapping ipv6_block (%s, %s)" % (n, other, net, onet))
        blocks[n] = net
        mgmt = ipaddress.ip_network("%s/64" % net.network_address)
        d["v6_block"] = str(net)
        d["mgmt_net"] = str(mgmt)
        d["mgmt_addr"] = "%s/64" % (mgmt.network_address + 1)
    used = {}
    for n, d in devs.items():
        parent = d["mgmt_parent"]
        if not parent:
            continue
        pd = devs.get(parent)
        if pd is None or not pd["mgmt_net"]:
            errors.append("%s: mgmt_parent %r is not a device with a valid ipv6_block" % (n, parent))
            continue
        raw = d["mgmt_ipv6_raw"]
        if not raw:
            errors.append("%s: mgmt_ipv6 is required when mgmt_parent is set" % n)
            continue
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            errors.append("%s: mgmt_ipv6 %r is not an IPv6 address" % (n, raw))
            continue
        mnet = ipaddress.ip_network(pd["mgmt_net"])
        if addr.version != 6 or addr not in mnet:
            errors.append("%s: mgmt_ipv6 %s is not inside %s's management network %s" % (n, raw, parent, mnet))
            continue
        if addr == mnet.network_address or str(addr) == str(ipaddress.ip_interface(pd["mgmt_addr"]).ip):
            errors.append("%s: mgmt_ipv6 %s is the network or %s's own address" % (n, raw, parent))
            continue
        if addr in used:
            errors.append("%s and %s both use mgmt_ipv6 %s" % (n, used[addr], raw))
            continue
        used[addr] = n
        d["mgmt_addr"] = "%s/64" % addr
        d["mgmt_net"] = pd["mgmt_net"]
        pd["mgmt_members"].append({"name": n, "addr": str(addr)})


def build_model(raw, cfg=None):
    cfg = _merge(DEFAULT_POLICY, cfg or {})
    errors = []
    devs = {}
    for d in raw.get("devices") or []:
        v = _device_view(d, cfg, errors)
        if v["role"] not in MANAGED_ROLES:
            continue
        if v["status"] not in cfg["device_statuses"]:
            continue
        devs[v["name"]] = v

    primaries = [n for n, d in devs.items() if d["primary"]]
    if len(primaries) != 1:
        errors.append("exactly one device needs bgp_primary=true (found %s)" % (primaries or "none"))
    elif devs[primaries[0]]["role"] != ROLE_TRANSIT:
        errors.append("primary %s must have role %s" % (primaries[0], ROLE_TRANSIT))
    if not any(d["role"] == ROLE_TRANSIT for d in devs.values()):
        errors.append("no transit-router devices found")

    shorts = {}
    for n, d in devs.items():
        if d["role"] in ROUTER_ROLES:
            if not d["loopback4"]:
                errors.append("%s: loopback_ip custom field is required" % n)
            ifn = cfg["gre_ifname_prefix"] + d["short"]
            if len(ifn) > 15:
                errors.append("%s: tunnel interface name %s is longer than 15 chars; set short_name" % (n, ifn))
            if d["short"] in shorts:
                errors.append("%s and %s share short name %s; set short_name" % (n, shorts[d["short"]], d["short"]))
            shorts[d["short"]] = n

    lists = _collect_prefix_lists(raw, errors)
    own = _own_prefixes(raw, cfg, lists, errors)
    peers = _peers(devs, errors)
    _plan_v6_blocks(devs, own["6"], errors)

    sessions = {}
    for s in raw.get("sessions") or []:
        v = _session_view(s, cfg, errors, lists)
        if not v:
            continue
        if v["device"] not in devs:
            continue
        if devs[v["device"]]["role"] not in ROUTER_ROLES:
            errors.append("session %s is on %s which is not a router" % (v["label"], v["device"]))
            continue
        sessions.setdefault(v["device"], []).append(v)
    for dev, lst in sessions.items():
        lst.sort(key=lambda s: (s["class"], s["label"], s["id"] or 0))
        _unique_names(lst)

    primary = primaries[0] if len(primaries) == 1 else None
    irr_sets = sorted({s["irr_as_set"] for lst in sessions.values() for s in lst if s["enabled"] and s["irr_as_set"]})

    nodes = {}
    for n, d in devs.items():
        rank = _rank(d)
        is_router = d["role"] in ROUTER_ROLES
        enabled = [s for s in sessions.get(n, []) if s["enabled"]]
        if enabled and not (own["4"] or own["6"]):
            errors.append("%s has eBGP sessions but no own prefixes were found in NetBox" % n)
        ibgp = []
        tunnels = []
        if is_router:
            for p in peers.get(n, []):
                pd = devs[p["peer"]]
                ibgp.append({
                    "peer": p["peer"],
                    "client": p["client"],
                    "lo4": pd["loopback4"],
                    "lo6": pd["loopback6"],
                    "p4": "ibgp4_" + _ident(p["peer"]),
                    "p6": "ibgp6_" + _ident(p["peer"]),
                    "required": _rank(pd) < rank,
                })
                tunnels.append({
                    "peer": p["peer"],
                    "ifname": cfg["gre_ifname_prefix"] + pd["short"],
                    "leader": n < p["peer"],
                })
        want_originate = d["originate"] if d["originate"] is not None else d["primary"]
        node = dict(d)
        node.update({
            "rank": rank,
            "is_router": is_router,
            "ibgp": ibgp,
            "tunnels": tunnels,
            "sessions": enabled,
            "originate": _bool(want_originate) and is_router,
            "own_v4": own["4"],
            "own_v6": own["6"],
            "static_lists": {nm: lists[nm] for nm in sorted({s["import_prefix_list"] for s in enabled if s["import_prefix_list"]})},
            "policy": {k: cfg[k] for k in (
                "own_asn", "ibgp_bfd", "bfd_interval_ms", "bfd_multiplier", "ospf_hello", "ospf_dead",
                "ospf_cost", "rtr_port", "ospf_enabled")},
        })
        # RPKI: transit routers run their own validator, others use the hub's.
        want_val = d["rpki_validator"]
        if want_val is None:
            want_val = d["role"] == ROLE_TRANSIT
        node["rpki_validator"] = _bool(want_val)
        if d["rtr_override"]:
            servers = []
            for item in d["rtr_override"]:
                host, _, port = item.rpartition(":")
                if not host:
                    host, port = item, str(cfg["rtr_port"])
                servers.append({"host": host.strip("[]"), "port": int(port)})
        elif node["rpki_validator"]:
            servers = [{"host": "127.0.0.1", "port": cfg["rtr_port"]}]
        elif d["hub"] and d["hub"] in devs and devs[d["hub"]]["loopback4"]:
            servers = [{"host": devs[d["hub"]]["loopback4"], "port": cfg["rtr_port"]}]
        else:
            servers = []
        node["rtr_servers"] = servers if is_router else []
        node["has_ebgp"] = bool(enabled)
        nodes[n] = node

    order = sorted(nodes, key=lambda n: (nodes[n]["rank"], n))
    return {
        "primary": primary,
        "order": order,
        "nodes": nodes,
        "own": own,
        "irr_sets": irr_sets,
        "errors": errors,
    }


def node_expected(node, classes=("transit",)):
    """Protocols that must be Established/Up for this node's deploy stage to pass.

    Only sessions of the given classes gate a deploy: an IX session whose peer has not
    approved it yet must not roll back an unrelated change. Transit is always required.
    """
    exp = [s["pname"] for s in node["sessions"] if s["class"] in classes]
    for p in node["ibgp"]:
        if p["required"]:
            exp.append(p["p4"])
            if p["lo6"] and node.get("loopback6"):
                exp.append(p["p6"])
    return exp


def irr_defines(irr_data, sessions):
    """Render IRR prefix sets for the sessions that need them.

    irr_data: {as_set: {"4": [{prefix, exact, ge, le}], "6": [...]}}
    Returns (text, empty_names) where empty_names lists sets with no prefixes.
    """
    lines = []
    empty = []
    done = set()
    for s in sessions:
        if not s.get("irr_filter") or s["irr_name"] in done:
            continue
        done.add(s["irr_name"])
        entries = (irr_data.get(s["irr_as_set"]) or {}).get(str(s["family"])) or []
        parts = []
        for e in entries:
            pfx = e["prefix"]
            net = ipaddress.ip_network(pfx, strict=False)
            if e.get("exact", True) and not e.get("greater-equal") and not e.get("less-equal"):
                parts.append(str(net))
            else:
                ge = e.get("greater-equal") or net.prefixlen
                le = e.get("less-equal") or (32 if net.version == 4 else 128)
                parts.append("%s{%d,%d}" % (net, ge, le))
        if parts:
            lines.append("define %s = [ %s ];" % (s["irr_name"], ", ".join(parts)))
        else:
            empty.append(s["irr_name"])
            lines.append("# %s: no prefixes found in IRR for %s, session rejects all routes" % (s["irr_name"], s["irr_as_set"]))
    return ["\n".join(lines) + ("\n" if lines else ""), empty]


def bird_name(name):
    return _ident(name)


def strip_mask(addr):
    return _strip(addr)


TUNNEL_DEFAULTS = {
    "pool": "10.255.0.0/17",      # one /31 per router pair, derived from the NetBox device ids
    "mtu": 1240,                  # Cloudflare Mesh path (~1280) minus GRE+UDP overhead
    "encap": "fou",               # GRE in UDP; Mesh only forwards TCP/UDP/ICMP. "gre" = raw GRE
    "fou_port": 5555,
    "ttl": 255,
}


def tunnel_plan(model, name, cfg=None):
    """Desired state for /etc/bgp-auto/tunnels.json on one router.

    Tunnel /31s are derived from the two device ids, so adding a router never
    renumbers an existing tunnel (device ids must be below 128).
    Returns {"errors": [...], "tunnels": [...], "loopbacks": [...], "fou_port": N}.
    """
    t = _merge(TUNNEL_DEFAULTS, cfg or {})
    node = model["nodes"][name]
    pool = ipaddress.ip_network(t["pool"])
    errors, tunnels = [], []
    if not node.get("mesh_ip"):
        errors.append("%s: mesh_ip not set yet (the mesh play fills it in)" % name)
    for tun in node["tunnels"]:
        peer = model["nodes"][tun["peer"]]
        if not peer.get("mesh_ip"):
            errors.append("%s: peer %s has no mesh_ip" % (name, peer["name"]))
        lo, hi = sorted((node["id"], peer["id"]))
        if hi >= 128:
            errors.append("%s<->%s: device ids must be below 128 for tunnel addressing" % (name, peer["name"]))
            continue
        base = int(pool.network_address) + 2 * (lo * 128 + hi)
        if base + 1 >= int(pool.broadcast_address):
            errors.append("%s<->%s: tunnel pool %s exhausted" % (name, peer["name"], pool))
            continue
        mine = base if node["id"] == lo else base + 1
        tunnels.append({
            "name": tun["ifname"], "peer": peer["name"],
            "local": node.get("mesh_ip"), "remote": peer.get("mesh_ip"),
            "encap": t["encap"], "encap_port": t["fou_port"], "ttl": t["ttl"], "mtu": t["mtu"],
            "addresses": ["%s/31" % ipaddress.ip_address(mine)],
        })
    loops = []
    if node.get("loopback4"):
        loops.append(node["loopback4"] + "/32")
    if node.get("loopback6"):
        loops.append(node["loopback6"] + "/128")
    dummies = []
    if node.get("mgmt_addr") and node["is_router"]:
        dummies.append({"name": t.get("mgmt_ifname", "mgmt0"), "addresses": [node["mgmt_addr"]]})
    return {"errors": errors, "tunnels": tunnels, "loopbacks": loops, "dummies": dummies, "fou_port": t["fou_port"]}


class FilterModule(object):
    def filters(self):
        return {
            "bgp_model": build_model,
            "bgp_node_expected": node_expected,
            "bgp_irr_defines": irr_defines,
            "bgp_prefix_set": bird_prefix_set,
            "bgp_bird_name": bird_name,
            "bgp_strip_mask": strip_mask,
            "bgp_tunnel_plan": tunnel_plan,
        }
