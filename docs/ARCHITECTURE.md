# Architecture Guide

## Overview

`bgp_auto` is a NetBox-driven network automation platform that automatically configures BGP, WireGuard, and GRE tunnels on routers. The system treats **NetBox as the source of truth** — you define your peers and topology once, and the playbook handles all configuration generation and deployment.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         NetBox (Source of Truth)                │
│                                                                 │
│  • Devices (routers)                                            │
│  • Custom Fields (BGP role, WG keys, loopback IPs, etc.)        │
│  • IP addresses (loopbacks, tunnel IPs)                         │
│  • Interfaces (physical for tunnel endpoints)                   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         │ API calls
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│        Ansible Inventory (netbox.netbox.nb_inventory)           │
│                                                                 │
│  • Queries NetBox API                                           │
│  • Builds dynamic host inventory                                │
│  • Populates host_vars from NetBox custom fields                │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│           Ansible Playbook (playbooks/deploy.yml)               │
│                                                                 │
│  1. Install packages (wireguard, bird, etc.)                    │
│  2. Execute roles → generate configs                            │
│  3. Deploy configs to target routers                            │
│  4. Validate and reload services                                │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│             Deployed Configurations (Routers)                   │
│                                                                 │
│  • /etc/bird/bird.conf (iBGP, OSPF, BFD)                        │
│  • /etc/wireguard/wg0.conf (VPN tunnels)                        │
│  • Optional: GRE tunnel configs                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow

### 1. Configuration Discovery

The Ansible inventory plugin (`inventory/netbox.yml`) queries NetBox and discovers:
- All devices with role = "Router"
- Device names → become inventory hostnames
- Device custom fields → populate host variables

Example host variable population:

```yaml
# From NetBox device custom_fields:
netbox_cf_loopback_ip: "10.0.0.1"
netbox_cf_bgp_role: "client"                    # or "rr" for route reflector
netbox_cf_wg_private_key: "..."
netbox_cf_wg_public_key: "..."
netbox_cf_peers: ["router02", "router03"]   # List of peer hostnames (preferred)
# Legacy: netbox_cf_wg_peers is also supported for compatibility
netbox_cf_ospf_enabled: true
```

### 2. Topology Discovery

The **bird** role discovers routers at runtime:

```yaml
# Build list of all routers
all_nodes: [router01, router02, router03, ...]

# Each router peers with all others (full mesh)
effective_bgp_peers: [router02, router03, ...]  # excluding self
```

Route reflectors reduce complexity:
- If any router has `netbox_cf_bgp_role = "rr"` → clients peer only with RRs
- If no RRs exist → full mesh topology

### 3. Configuration Generation

Each role generates configuration files from Jinja2 templates:

**BIRD Role:**
- Builds iBGP peer list dynamically
- Generates BFD adjacency checks
- Configures OSPF on loopbacks
- Supports route reflector mode

**WireGuard Role:**
- Generates or retrieves private/public key pair
- Syncs keys back to NetBox custom fields
-- Builds [Peer] sections from `netbox_cf_peers` (falls back to `netbox_cf_wg_peers`)

**GRE Role (optional):**
- Queries NetBox for tunnel prefix
- Allocates IP pairs dynamically
- Stores tunnel configs in NetBox

### 4. Service Validation & Reload

- **BIRD:** Config validated with `bird -p -c /etc/bird/bird.conf` before reload
- **WireGuard:** Config file permissions set to 0600, then systemd restarts service
- **Handlers:** Services only reload if config changed (Ansible handlers)

## Configuration Files

### Global Variables (`group_vars/all.yml`)

```yaml
bgp_as: 17290                                    # AS for all routers
wg_port: 51820                                   # WireGuard listen port
loopback_ip: "{{ netbox_cf_loopback_ip | ipaddr('address') }}"
netbox_url: "{{ lookup('env', 'NETBOX_URL') }}"
netbox_token: "{{ lookup('env', 'NETBOX_TOKEN') }}"
```

### Inventory Plugin (`inventory/netbox.yml`)

Configured to query NetBox API and filter for router devices. Variables like `NETBOX_API_TOKEN` and `NETBOX_API` are read from environment or use fallbacks.

### Deployment Playbook (`playbooks/deploy.yml`)

1. Installs WireGuard package
2. Applies `wireguard` role
3. Applies `bird` role
4. Optionally applies `gre` role

Uses `--become` for privilege escalation and `--check` for dry-run mode.

## Workflow: Adding a New Peer

1. **Create device in NetBox:**
   - Device name: `router04`
   - Device type: Router
   - Management IPv4: IP for SSH access

2. **Add custom fields:**
   - `loopback_ip`: `10.0.0.4`
   - `bgp_role`: `client` or `rr`
   - `wg_private_key`: Leave blank (auto-generated)
   - `wg_public_key`: Leave blank (auto-generated)
   - `peers`: `["router01", "router02", "router03"]`

3. **Add loopback interface & IP:**
   - Interface name: `lo`
   - Address: `10.0.0.4/32` attached to `lo`

4. **Run playbook:**
   ```bash
   ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml -e device_name=router04
   ```

5. **Verify:**
   - SSH to `router04`, check `/etc/bird/generated/ibgp.conf`
   - Verify BGP peers: `birdc show protocols`
   - Verify WireGuard: `wg show`

6. **WireGuard keys synced back to NetBox** after first run

## Custom Fields Reference

| Field | Type | Required | Example | Purpose |
|-------|------|----------|---------|---------|
| `loopback_ip` | Text | Yes | `10.0.0.1` | Router's loopback/management IP |
| `bgp_role` | Text | No | `client` or `rr` | BGP role (client or route reflector) |
| `wg_private_key` | Text | No | (auto-generated) | WireGuard private key |
| `wg_public_key` | Text | No | (auto-generated) | WireGuard public key |
| `peers` | JSON | Yes | `["router02", "router03"]` | List of WireGuard peer names (legacy `wg_peers` also accepted) |
| `ospf_enabled` | Boolean | No | `true` | Enable OSPF on loopbacks |

## Common Scenarios

### Scenario 1: Full Mesh BGP

```
All routers peer with each other.

routers: [router01, router02, router03]
all with bgp_role: client
→ Each router has 2 BGP peers
```

### Scenario 2: Route Reflector Topology

```
Two route reflectors, clients peer only with RRs.

routers:
  - router01 (bgp_role: rr)
  - router02 (bgp_role: rr)
  - router03 (bgp_role: client)
  - router04 (bgp_role: client)

→ router03 peers with router01 and router02 only
→ router01 and router02 peer with each other
```

### Scenario 3: Adding a New Router

```
Current setup: router01, router02, router03
Adding: router04

1. Create device in NetBox
2. Set custom fields (loopback_ip, peers: [router01, router02, router03])
3. Run playbook on router04
4. router04 auto-discovers peers via NetBox
5. WireGuard keys auto-sync back to NetBox
```

## Troubleshooting

### Device not discovered
- Verify device exists in NetBox
- Check device role is "Router"
- Verify NETBOX_TOKEN has API permissions

### Configuration not deployed
- Run with `-vvv` for debug output
- Check `ansible-playbook --syntax-check` first
- Verify BIRD config: `bird -p -c /etc/bird/bird.conf`

### WireGuard peers missing
- Check `netbox_cf_peers` (or legacy `netbox_cf_wg_peers`) custom field set on device
- Verify referenced peers exist in NetBox
- Confirm WireGuard public keys synced to NetBox after first run

## API Interactions

### NetBox API Calls Made by This Playbook

- `GET /api/dcim/devices/` — List all router devices
- `GET /api/dcim/interfaces/?device=X&name=lo` — Get loopback interface
- `GET /api/ipam/ip-addresses/?interface_id=Y` — Get loopback IP
- `GET /api/dcim/devices/?name=X` — Get device ID for key sync
- `PATCH /api/dcim/devices/ID/` — Update WireGuard keys in custom fields

See [NetBox Setup Guide](NETBOX_SETUP.md) for detailed custom field configuration.
