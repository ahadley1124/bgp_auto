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
