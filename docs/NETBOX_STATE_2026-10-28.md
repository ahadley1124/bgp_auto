# NetBox state vs. `feature/dual-transit-bgp` — 2026-10-28

Source: `https://netbox.h2tech.dev` (NetBox 4.6.10 on host `bgp2`). Verified over the live REST
API (token reissued 2026-10-28) and cross-checked against the Django ORM over SSH.

## Two separate blockers, both confirmed against the live API

### 1. Cloudflare blocks urllib's default User-Agent (fixed in this working tree)

`scripts/netbox_fetch.py` used stdlib urllib, which sends `User-Agent: Python-urllib/3.12`.
The Cloudflare in front of NetBox rejects that signature outright with **HTTP 403, error 1010**
— on *every* path, including plain `/api/dcim/devices/`. It looks exactly like an auth failure
but is not: the same request with any other UA returns 200.

```
Python-urllib/3.12      HTTP 403     <- blocked
ansible-httpget         HTTP 200
python-requests/2.31.0  HTTP 200
bgp_auto/1.0            HTTP 200
curl/8.5.0              HTTP 200
```

Fixed by sending an explicit UA (`NETBOX_USER_AGENT`, default `bgp_auto/1.0`).
`scripts/netbox_bootstrap.py` reuses the same client, so it is covered too.
The Ansible `nb_inventory` plugin sends `ansible-httpget`/requests, so it was never affected.

### 2. The plugin the branch targets is gone

`netbox-bgp` was removed on 2026-09-21 and its data migrated to `netbox-routing`.
Installed plugins now: `netbox_secrets`, `netbox_prometheus_sd`, `netbox_topology_views`,
`netbox_routing` (0.5.0), `netbox_peering_manager` (0.3.1).

**`/api/plugins/bgp/` does still resolve — but it is `netbox_peering_manager` on that base path**,
with a completely different URL layout (`peering-session`, `peer-asn`, `peering-connection`, …).
So `/api/plugins/bgp/session/` 404s because the path shape belongs to another plugin, not because
the namespace vanished. Worth knowing: a bare "does `/api/plugins/bgp/` exist?" check says yes
and is misleading.

## Live probe result (after the UA fix)

```
FAIL  devices            /api/dcim/devices/?role=transit-router&role=ix-router&role=monitoring  -> 400
FAIL  sessions           /api/plugins/bgp/session/                                              -> 404
FAIL  prefix_lists       /api/plugins/bgp/prefixlist/                                           -> 404
FAIL  prefix_list_rules  /api/plugins/bgp/prefixlistrule/                                       -> 404
FAIL  tagged_prefixes    /api/ipam/prefixes/?tag=bgp-originate                                  -> 400
FAIL  anycast_prefixes   /api/ipam/prefixes/?tag=bgp-anycast                                    -> 400
```

All six are now genuine data/schema gaps, not transport problems. The 400s are explicit:

```
{"role":["Select a valid choice. transit-router is not one of the available choices."]}
{"tag":["Select a valid choice. bgp-originate is not one of the available choices."]}
```

## Endpoint mapping for a `netbox-routing` port

| `netbox_fetch.py` key | old (`netbox-bgp`) | new (`netbox-routing`) |
|---|---|---|
| `sessions` | `/api/plugins/bgp/session/` | `/api/plugins/routing/bgp/peer/` |
| `prefix_lists` | `/api/plugins/bgp/prefixlist/` | `/api/plugins/routing/objects/prefix-list/` |
| `prefix_list_rules` | `/api/plugins/bgp/prefixlistrule/` | `/api/plugins/routing/objects/prefix-list-entry/` |
| — | — | `/api/plugins/routing/bgp/router/` (new: ASN ↔ device binding) |
| — | — | `/api/plugins/routing/bgp/{session,peer,policy}-template/` |

`NETBOX_ENDPOINTS` can remap the paths, but **the response schemas differ**, so
`filter_plugins/bgp_auto.py` needs real work as well — this is not a path swap.

`netbox_routing.BGPPeer` fields (the `BGPSession` analogue):

```
scope       -> netbox_routing.BGPScope   (required)
peer        -> ipam.IPAddress            (required; was remote_address)
source      -> ipam.IPAddress            (was local_address)
remote_as   -> ipam.ASN
local_as    -> ipam.ASN
peer_group    -> BGPPeerTemplate
peer_session  -> BGPSessionTemplate
bfd         -> netbox_routing.BFDProfile
ttl, password, enabled, status(active|planned|offline|failed), tenant, description, comments
```

Several of the 17 custom fields in `netbox/schema.json` become **native fields** here and should
be dropped rather than recreated: `bgp_password`→`password`, `multihop_ttl`→`ttl`, `bfd`→`bfd`.
The rest (`session_class`, `max_prefix`, `irr_as_set`, `irr_filter`, `local_pref`,
`export_prepend`, `enforce_first_as`, `allow_private_as`, `accept_default`, `graceful_shutdown`,
`neighbor_interface`, `import_prefix_list`, `anycast_prepend`, `anycast_communities`, `path_asn`)
still need creating, but on object type **`netbox_routing.bgppeer`**, not `netbox_bgp.bgpsession`.

## Devices (5)

| id | name | role | site | primary IPv4 | primary IPv6 |
|---|---|---|---|---|---|
| 1 | `vultr-1` | router | Vultr NY | 45.63.74.51/32 | — |
| 2 | `vultr second` | router | Vultr NY | 104.207.142.187/23 | — |
| 3 | `nl-1` | router | nl-lungu | 142.249.174.11/32 | — |
| 6 | `bee-ix` | router | bee-ix | — | — |
| 7 | `AS219040 tunnel` | router | Vultr NY | 89.28.205.35/32 | — |

Custom field values actually set:

```
vultr-1          ansible_user=root     bgp_role=client  peers=['nl-1','ohio-1','f4ix']
vultr second                           bgp_role=client  peers={}
nl-1                                   bgp_role=client  peers=['ohio-1','vultr-1','f4ix']
bee-ix           ansible_user=vpsuser  bgp_role=client  peers=['vultr-1','nl-1']
AS219040 tunnel                        bgp_role=client  peers=['Vultr-1']
```

Notes:
* `peers` references **`ohio-1`** and **`f4ix`**, which are **not devices in NetBox**.
  `AS219040 tunnel` references `Vultr-1` (wrong case vs. `vultr-1`).
* Sites `kc-f4ix` and `ohio-virtnet` exist but hold no devices.
* Only one device role exists: `router`. The branch needs `transit-router`, `ix-router`,
  `monitoring`, and `inventory/netbox.yml` filters on exactly those three — so the
  dual-transit playbook currently resolves **zero hosts**.
* Device ids (1, 2, 3, 6, 7) are all < 128, so the tunnel addressing scheme is fine.

## Custom fields present (3)

`ansible_user` (text, device), `bgp_role` (select, device), `peers` (json, device).

Of the 24 the branch wants, only `ansible_user` exists. Missing on `dcim.device`:
`loopback_ip`, `loopback_ip6`, `bgp_primary`, `hub`, `short_name`, `mesh_ip`, `rpki_validator`,
`rpki_rtr`, `originate_prefixes`, `use_mesh`, `gre_endpoint`, `ipv6_block`, `mgmt_parent`,
`mgmt_ipv6`, `anycast_originate`.

## Tags

**None defined.** `bgp-originate` and `bgp-anycast` do not exist — hence the 400s above.

## IPAM

RIR: ARIN only. Aggregates: **none**.

ASNs: **AS17290** (sites ohio-virtnet + Vultr NY), **AS40064**, **AS40419**.
No AS205941, AS20473, AS64515 or AS219040 object.

Prefixes — 266 total, of which 260 are /31s pre-carved out of `10.255.0.0/16`:

```
10.255.0.0/16            'loopback peering addresses'   (container)
  ├─ 257 unused /31s in 10.255.0.0/23
  ├─ 10.255.2.0/31   GRE ohio-1 <-> vultr-1
  ├─ 10.255.2.2/31   GRE nl-1   <-> vultr-1
  ├─ 10.255.2.4/31   GRE nl-1   <-> ohio-1
  ├─ 10.255.2.6/31   GRE f4ix   <-> nl-1
  └─ 10.255.2.8/31   GRE f4ix   <-> vultr-1
23.190.216.0/24
2602:fa86::/36
2602:fa86:ffe::/48       'AS205941 Stefan'
2602:fa86:fff:ffff::/64
```

IP addresses (14):

```
10.255.0.0/31    nl-1:dummy1            GRE nl-1 <-> ohio-1
10.255.0.1/32    vultr-1:lo
10.255.0.3/32    nl-1:lo
10.255.0.5/32    vultr second:lo
10.255.2.1/31    vultr-1:dummy1         GRE ohio-1 <-> vultr-1
10.255.2.2/31    nl-1:dummy1            GRE nl-1 <-> vultr-1
10.255.2.3/31    vultr-1:dummy1         GRE nl-1 <-> vultr-1
10.255.2.4/31    nl-1:dummy2            GRE nl-1 <-> ohio-1
10.255.2.7/31    nl-1:dummy1            GRE f4ix <-> nl-1
10.255.2.9/31    vultr-1:dummy2         GRE f4ix <-> vultr-1
45.63.74.51/32   vultr-1:primary
89.28.205.35/32  AS219040 tunnel:Primary
104.207.142.187/23  vultr second:primary
142.249.174.11/32   nl-1:primary
```

Interfaces:

```
vultr-1        dummy1 [10.255.2.1/31, 10.255.2.3/31], dummy2 [10.255.2.9/31], dummy3 [], lo [10.255.0.1/32], primary [45.63.74.51/32]
vultr second   lo [10.255.0.5/32], primary [104.207.142.187/23]
nl-1           dummy1 [10.255.0.0/31, 10.255.2.2/31, 10.255.2.7/31], dummy2 [10.255.2.4/31], dummy3 [], lo [10.255.0.3/32], primary [142.249.174.11/32]
bee-ix         (none)
AS219040 tunnel  Primary [89.28.205.35/32]
```

## `netbox_routing` contents (the migrated netbox-bgp data)

**`BGPRouter`, `BGPPeer`, `BGPAddressFamily`, `BGPScope`, `BGPSetting` are all 0 rows** — there
are no actual BGP sessions modelled in NetBox at all. Only templates survived the migration:

```
BGPSessionTemplate  id=3  "Upstream v4"  local_as=AS17290
                    id=4  "Upstream v6"  local_as=AS17290
BGPPeerTemplate     id=3  "Upstream v4"  (remote_as unset)
                    id=4  "Upstream v6"  (remote_as unset)
BGPPolicyTemplate   id=3  "Upstream v4"  prefixlist_out=Internal v4
                    id=4  "Upstream v6"  prefixlist_out=internal v6
Community           id=2  17290:666
PrefixList          id=3  "Internal v4" (family 4)
                    id=4  "internal v6" (family 6)
PrefixListEntry     Internal v4 seq 5  permit -> IPAM prefix 23.190.216.0/24
                    Internal v4 seq 10 permit -> CustomPrefix 44.32.199.0/24
                    internal v6 seq 5  permit -> IPAM prefix 2602:fa86::/36
CustomPrefix        id=2  44.32.199.0/24
```

`netbox_peering_manager` is **completely empty** (0 rows in all 12 models).

## Where the branch's assumptions and NetBox disagree

1. **Plugin + schema.** Covered above: paths, response shapes and the custom-field object type
   all change. `netbox_bootstrap.py` cannot apply `schema.json` as written.
2. **No sessions exist anywhere** — not in `netbox_routing.BGPPeer`, not in
   `netbox_peering_manager.PeeringSession`. The Vultr (AS64515), NL (AS205941) and IX sessions
   the doc says to seed have never been created.
3. **Own IPv4.** The doc says `44.32.199.0/24`; that exists only as a `netbox_routing`
   CustomPrefix, not in IPAM. IPAM holds `23.190.216.0/24`, which per the AS40419 separation
   must not leave `vpn` except toward AS40419 — so it probably should **not** be in `OWN-V4`.
   `Internal v4` currently permits both, which looks wrong for this model.
4. **`OWN-V4` / `OWN-V6` / `REASSIGNED` do not exist** under those names. Nearest equivalents
   are `Internal v4` / `internal v6`; there is no `REASSIGNED` list even though
   `2602:fa86:ffe::/48` is in IPAM and labelled "AS205941 Stefan".
5. **The doc's `ffe`-vs-`fife` question resolves to `ffe`** — NetBox already has it that way.
6. **Devices.** No monitoring device; the IX routers referenced in `peers` (`ohio-1`, `f4ix`)
   do not exist. 45.63.74.51 is `vpn.as40064.org` (2 vCPU / 4 GB), 104.207.142.187 is
   `bgp2.as40064.org` (1 vCPU / 1 GB) — which also runs NetBox itself, so making it a transit
   router with a full table needs a hard look at the 1 GB RAM against the doc's ~4 GB figure.
7. **Anycast `2602:fa86:ff8::/46` is not in IPAM** and nothing is tagged `bgp-anycast`.
   `2602:fa86:fff:ffff::/64` is present, so `ff8`–`ffb` do look unused.
