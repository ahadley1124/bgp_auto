# Dual-transit BGP (Vultr primary, Netherlands second)

AS17290, IPv4 and IPv6. NetBox is the only place you edit; Ansible turns it into BIRD 3 config.

## Shape

```
        Cloudflare Mesh (100.96.0.0/12), GRE-in-UDP tunnels, OSPF, loopback iBGP
 
 Vultr transit (PRIMARY, RR) <------- iBGP ------->  NL transit (RR)
   | eBGP Vultr (AS64515)                              | eBGP AS205941
   +-- IX VM(s) (RR clients)                           +-- IX VM(s) (RR clients)
 Monitoring VM (Prometheus) scrapes everything over the mesh
```

* Each router has an IPv4 (and optional IPv6) loopback. OSPFv2/v3 run over the `gre-*` tunnels and
  announce the loopbacks; iBGP runs loopback to loopback, so it follows OSPF.
* The two transit routers are route reflectors for their own IX routers and peer with each other.
  An IX router tunnels only to its `hub`.
* Own prefixes are originated only on the primary (static blackhole routes, tagged with the
  local-origin large community) and only that tag is exported to eBGP.

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
4. Cloudflare: Mesh enabled, device profile = split tunnel **include** `100.96.0.0/12` only, API token.
5. Each VM needs 3+ vCPU for the Mesh client and about 4 GB RAM for two full tables.

## Not verified here

Only config syntax (BIRD 2.14), filter behaviour, the Ansible flow (fake NetBox, fake birdc) and the
tunnel logic (fake `ip`) were tested. These need a real look on first deploy: netbox_bgp API paths, how
BIRD 3 and bird_exporter get on, Cloudflare Mesh with GRE-in-UDP (and whether it leaves the BIRD routes
alone), the Vultr BGP specifics, the Routinator apt repo and the bgpq4 JSON format.

## Tests

`tests/run_tests.sh` (unit, render, BIRD filter behaviour, Ansible flow against `tests/mock_netbox.py`).
