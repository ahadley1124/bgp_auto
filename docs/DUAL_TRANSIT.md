# Dual-transit BGP (Vultr primary, Netherlands second)

AS17290, IPv4 and IPv6. NetBox is the only place you edit; Ansible turns it into BIRD 3 config.

## Shape

```
 Vultr transit (PRIMARY, RR) <== direct GRE + GRE over Cloudflare Mesh ==> NL transit (RR)
   | eBGP Vultr (AS64515)                                                  | eBGP AS205941
   +-- IX VM(s): direct GRE to this hub (RR clients)                       +-- IX VM(s): direct GRE to this hub
 Monitoring VM (Prometheus): on the mesh, scrapes the transits over it and the IX VMs on their public address
```

* Each router has an IPv4 (and optional IPv6) loopback. OSPFv2/v3 run over every tunnel and
  announce the loopbacks; iBGP runs loopback to loopback, so it follows OSPF.
* **IX VM to its hub:** one direct GRE tunnel (`gre-<short>`, raw GRE, IP protocol 47) between the two
  public addresses. No Cloudflare Mesh on IX VMs.
* **Hub to hub (the two transit routers):** a direct GRE tunnel (`gre-<short>`, OSPF cost 10) and a second
  GRE-in-UDP tunnel over the Mesh (`grm-<short>`, cost 20), so OSPF prefers the direct path and the Mesh one
  is the fallback. Only nodes with `use_mesh` (transit routers and monitoring by default) join the Mesh.
* The two transit routers are route reflectors for their own IX routers and peer with each other.
* Own prefixes are originated only on the primary (static blackhole routes, tagged with the
  local-origin large community) and only that tag is exported to eBGP.

Firewalls: every router must allow IP protocol 47 (GRE) from the peer's public address. Tunnel endpoints are
the device's primary IPv4 unless `gre_endpoint` is set (use it if the VM sits behind NAT, which raw GRE
does not survive). Tunnel /31s come from `10.255.0.0/17` (direct) and `10.254.0.0/17` (mesh), derived from
the two device ids; change the pools with `bgp_tunnel` if they collide with something of yours.

## IPv6 per VM

Give a VM its own /48 in the device custom field `ipv6_block` (must be inside `OWN-V6`, unique, no overlaps;
the model rejects anything else before a host is touched). The **first /64** of that /48 is its management
network: the router gets `<that /64>::1` on a dummy interface `mgmt0`, and OSPFv3 announces the /64 so every
router can reach it. VMs inside the same IX use addresses from that /64: set `mgmt_parent` (the IX router's
device name) and `mgmt_ipv6` (an address inside the parent's management /64). Only routers get `mgmt0`
configured by these playbooks; configuring the addresses on the other VMs is not automated yet.
The router also **announces its /48** (a blackhole route tagged as locally originated, so the management /64 and
any /64 you put on a local interface stay more specific). It goes to that router's IX peers and, through iBGP and
the hub, to the transits, so someone outside can reach the VM's management over the IX path instead of entering
at Vultr and coming back. Every router treats all the /48s as our own space, so a hub passes an IX router's /48
on but never originates it, and the /48 learned back from an IX peer is rejected as our own space. The rest of the
/48 is dropped on the owning router until you route it somewhere.

## Anycast prefix and tier-1 watching

* **Prefix:** create the anycast prefix in NetBox IPAM and tag it `bgp-anycast`. For AS17290 that is planned as
  `2602:fa86:ff8::/46` (next free /46 below the `ffe` /48; confirm `ff8` to `ffb` are unused first). It must be
  inside your own space and must not overlap a VM's /48. It also needs a ROA and an IRR `route6` object.
* **Sites:** set `anycast_originate` on both transit routers. Each originates the prefix (a blackhole route, so
  service addresses learned from the VMs that serve it are more specific and win).
* **Steering, per session, only on the anycast prefix:** `anycast_prepend` (0-5) and `anycast_communities`
  (comma separated, `a:b` or `a:b:c`, e.g. Vultr's `20473:6001:3356` to prepend once toward AS3356).
  Set `path_asn` when the AS shown in paths differs from the session AS (Vultr defaults to 20473).
* **Watcher (monitoring VM):** every 15 minutes it asks RIPEstat's looking glass (RIPE RIS) how tier-1 networks
  reach the prefix, exports `bgp_auto_anycast_*` metrics (tier-1s that see it, AS path length per tier-1, share of
  tier-1 views entering via each transit) and writes `/var/lib/bgp-auto/anycast-suggestions.json`.
  Set `anycast_target_share` (e.g. `{20473: 0.6}`) to get suggestions such as "set `anycast_prepend=1` on
  session `vultr-v6`". **Suggestions are never applied automatically.** You approve by editing the session in
  NetBox and running the playbook (the usual validate, apply, rollback flow). Alerts: prefix not seen at any
  tier-1, change suggested.
* Limits: RIS has few tier-1 peers, so this is a sample, not a census; the looking-glass output format and the
  transit communities of AS205941 are not verified here.

## Policy

| What | How |
|---|---|
| Inbound preference | Vultr wins: the NL session prepends 3x (`export_prepend`), Vultr prepends 0 |
| Outbound | both transits get local-pref 100, so the shortest AS path wins; IX rs 200, IX bilateral 220, customer 300 |
| RPKI | ROV on every import, invalid is rejected, fail-open if the validator is down. Routinator on transit routers; IX routers use their hub |
| IRR | per-session as-set (default `AS<remote>`) via bgpq4 on bilateral and customer sessions; transit and route-server sessions rely on the other filters. Set `irr_filter` per session to change that. Empty set = reject all |
| Bogons | bogon prefixes, bogon and private ASNs (private allowed only where `allow_private_as` is set, i.e. Vultr), too long/short prefixes, own space, default route, tier-1 leak check on IX |
| Max prefix | class defaults (transit 1.4M v4 / 400k v6), override with `max_prefix`. Transit restarts, IX sessions stay down |

Large communities: `17290:1000:1..5` origin class, `:2000:<device id>`, `:1100:1/2` RPKI valid/unknown,
`:9000:1` neighbor route. `65535:0` (graceful shutdown) is honoured on import.

## Adding things (all in NetBox)

* **IX peer**: add a netbox_bgp session on the IX router device, set `session_class` to `ix-rs` or
  `ix-bilateral`, put the password in the session. Merge/run the playbook.
* **Disable a session**: set its status to anything but `active`.
* **New router**: create the device with role `transit-router` / `ix-router`, a primary IP, `loopback_ip`,
  `hub`, `short_name`. Device ids must be below 128 (tunnel addressing).
* Everything the model rejects is listed before any host is touched.

## Running it

Nothing deploys automatically. GitHub Actions only runs read-only checks (tests and `validate.yml`); you run the deploy by hand.

```
export NETBOX_URL=... NETBOX_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...
ansible-playbook -i localhost, playbooks/validate.yml          # read-only: render + `bird -p` everything
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml -e bgp_limit=vultr-transit   # Vultr first
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml  # everything, one router at a time
```

Per router: tunnels, RPKI validator, render into a staging dir, `bird -p`, back up the live config,
copy, `birdc configure check`, `configure timeout`, wait for the expected sessions (transit sessions and
iBGP to already-deployed routers), `configure confirm`. If anything fails the previous config is restored
and the rollout stops before the next router. A run with no NetBox change does not reconfigure BIRD.

## Before the first run

1. `scripts/netbox_bootstrap.py` creates the roles, tag and custom fields (`netbox/schema.json`).
2. `scripts/netbox_fetch.py probe` checks the netbox_bgp endpoints and field names (not verified here).
3. Seed NetBox: devices, the Vultr session (AS64515, multihop, MD5 password, `neighbor_interface` if needed),
   the NL sessions (IPs, password), prefix lists `OWN-V4` / `OWN-V6`, IX sessions.
4. Cloudflare (transit routers and monitoring only): Mesh enabled, device profile = split tunnel **include** `100.96.0.0/12` only, API token.
5. The VMs on the Mesh need 3+ vCPU for the client; every router needs about 4 GB RAM for two full tables.

## Not verified here

Only config syntax (BIRD 2.14), filter behaviour, the Ansible flow (fake NetBox, fake birdc) and the
tunnel logic (fake `ip`) were tested. These need a real look on first deploy: netbox_bgp API paths, how
BIRD 3 and bird_exporter get on, Cloudflare Mesh with GRE-in-UDP (and whether it leaves the BIRD routes
alone), raw GRE between the Vultr and NL public addresses, the Vultr BGP specifics, the Routinator apt repo and the bgpq4 JSON format.

## Tests

`tests/run_tests.sh` (unit, render, BIRD filter behaviour, Ansible flow against `tests/mock_netbox.py`).

## Own address space and reassigned blocks

- IPv4: `44.32.199.0/24`. IPv6: `2602:fa86::/36`. Put both in the NetBox prefix lists `OWN-V4` / `OWN-V6`
  (OWN-V6 with `le 48`).
- Space inside the /36 that belongs to someone else goes in a NetBox prefix list named `REASSIGNED`
  (setting `reassigned_prefix_list`). The model refuses to use it for a VM `ipv6_block` or an anycast prefix,
  and routers stop treating it as own space, so a route for it from its real holder is not dropped as "our space".
- Enter the reassigned block as `2602:fa86:ffe::/48`. The request said `fife`, which is not valid hex, so this
  is assumed to be `ffe` and needs your confirmation.
- The primary still originates the /36 aggregate as a static blackhole. Make sure the friend's /48 reaches
  its holder (they announce it themselves, or you route it), otherwise the aggregate will swallow its traffic.

## Findings from the account lookups

- Vultr (read-only connector): only two instances exist, both in `ord` (Chicago): `vpn.as40064.org`
  (2 vCPU / 4 GB, 45.63.74.51) and `bgp2.as40064.org` (1 vCPU / 1 GB, 104.207.142.187). The larger one is
  `vpn.as40064.org`. Their labels use as40064.org, not AS17290; confirm that is the box meant to be the primary.
- bgpv6.com lists AS17290 upstreams AS20473 and AS205941 (shown as LSG, country RO, not NL), and 11 IXPs: Amateur IX
  (Ashland), EVIX (Fremont), F4IX MCI (Kansas City), ixFabric Chicago / Kansas / Frankfurt / London,
  NEO-IX (Akron), ONIX (Toronto), BEE-IX (Lviv), INTERIX (Amsterdam).
- as205941.org/communities names informational, action and relationship categories in standard and large format
  but publishes no values on that page, so no AS205941 community is encoded yet.
