# Role Documentation

Detailed documentation of each Ansible role in the `bgp_auto` playbook.

## Role: `wireguard`

Configures WireGuard VPN tunnels for encrypted peer-to-peer communication.

### Purpose

- Generates or retrieves WireGuard private/public key pair
- Syncs keys to NetBox custom fields for persistence
- Generates WireGuard interface config with peer list
- Manages WireGuard systemd service

### Prerequisites

- WireGuard package installed (handled by playbook)
- WireGuard kernel module loaded (automatic on modern kernels)
- All peers must be reachable via Endpoint IP

### Configuration Files Generated

**Location:** `/etc/wireguard/wg0.conf`

```ini
[Interface]
Address = 10.0.0.1/32          # From netbox_cf_loopback_ip
PrivateKey = xxx...            # Private key (no log)
ListenPort = 51820

[Peer]
PublicKey = yyy...             # Peer public key
Endpoint = 192.168.1.2:51820   # Peer management IP
AllowedIPs = 10.0.0.2/32       # Peer loopback IP
PersistentKeepalive = 25       # Keep connection alive

# ... more [Peer] sections for each wg_peer
```

### Variables

**Required:**
- `wg_port` — Listen port (default: 51820)
- `netbox_cf_loopback_ip` — Router's loopback IP
- `netbox_cf_wg_peers` — List of peer hostnames (from NetBox)

**Auto-Generated:**
- `wg_private_key` — Generated if not in NetBox
- `wg_public_key` — Derived from private key

**Optional:**
- `wg_port_override` — Override listen port per-host
- `NETBOX_VALIDATE_CERTS` — SSL verification (default: true)

### Key Sync Process

1. If `netbox_cf_wg_private_key` is empty or undefined:
   - Generate new private key: `wg genkey`
   - Derive public key: `wg pubkey`
2. Query NetBox API for device ID by hostname
3. PATCH device custom fields with new keys
4. Keys stored in NetBox for next run

**Note:** Keys are `no_log: true` in playbook output for security.

### Handlers

**Handler:** `Restart WireGuard`

- Triggered when `/etc/wireguard/wg0.conf` changes
- Runs: `systemctl restart wg-quick@wg0`
- Service automatically starts on boot

### Troubleshooting

**"Device not found in NetBox"**
- Verify device name matches inventory hostname exactly
- Check NETBOX_TOKEN has device read/write permissions
- Test: `curl "$NETBOX_API/api/dcim/devices/?name=router01"`

**"No peers connecting"**
- Verify `netbox_cf_wg_peers` is a valid JSON list: `["router02", "router03"]`
- Check peer hostnames exist in inventory
- Test connectivity: `ping <peer_endpoint>`

**"Keys not syncing to NetBox"**
- Verify `netbox_cf_wg_private_key` and `netbox_cf_wg_public_key` custom fields exist
- Check API token permissions (POST/PATCH on devices)
- Run with `-vvv` to see API responses

---

## Role: `bird`

Configures BIRD Internet Routing Daemon for BGP, OSPF, and BFD.

### Purpose

- Discovers all router peers in NetBox dynamically
- Generates full-mesh or route-reflector iBGP topology
- Configures OSPF for loopback reachability
- Enables BFD for fast failure detection

### Prerequisites

- BIRD installed (usually: `apt install bird bird2`)
- Loopback IP configured on router
- All peers reachable on loopback network

### Configuration Files Generated

**Location:** `/etc/bird/generated/`

#### File: `ibgp.conf`

iBGP peer configuration (full-mesh or RR-aware):

```
protocol bgp ibgp_router02 {
    local as 17290;
    neighbor 10.0.0.2 as 17290;
    source address 10.0.0.1;
    multihop;
    bfd yes;

    ipv4 { import all; export all; };
    ipv6 { import all; export all; };

    rr client;  # Only if this router is a route reflector
}
```

#### File: `ospf.conf`

OSPF for loopback reachability (if enabled):

```
protocol ospf v3 ospf_v6 {
    ipv6 {
        import all;
        export all;
    };
    area 0 {
        interface "lo" { stub yes; };
    };
}
```

#### File: `bfd.conf`

BFD (Bidirectional Forwarding Detection) for fast adjacency failure:

```
protocol bfd {
    multihop {
        neighbor 10.0.0.2 dev lo;
    };
}
```

### Variables

**From NetBox:**
- `bgp_as` — BGP Autonomous System (from `group_vars/all.yml`)
- `loopback_ip` — Router's loopback IP (from `netbox_cf_loopback_ip`)
- `netbox_cf_bgp_role` — `client` or `rr` (route reflector)
- `netbox_cf_ospf_enabled` — Enable OSPF (default: true)

**Discovered at Runtime:**
- `all_nodes` — All routers with role = "Router" in NetBox
- `bgp_peers` — All nodes except self
- `route_reflectors` — Nodes with `bgp_role = "rr"`
- `effective_bgp_peers` — Final peer list based on topology

### Topology Logic

**Full Mesh (default):**
```
If no route reflectors defined:
  → Each router peers with all other routers
  → BGP convergence slower, more resilient
```

**Route Reflector:**
```
If any router has bgp_role = "rr":
  → Clients (bgp_role = "client") peer only with RRs
  → RRs peer with each other and clients
  → Reduces BGP churn, enables easy scaling
```

### Handlers

**Handler:** `reload bird`

- Triggered when any config file changes
- Runs: `systemctl reload bird`
- Graceful reload (no connection drops)

### Validation

Before reload, playbook runs:
```bash
bird -p -c /etc/bird/bird.conf
```

Fails playbook if syntax errors exist.

### Verify BGP Status

SSH to router and check:

```bash
# List all protocols
$ birdc show protocols

# Check specific BGP session
$ birdc show protocols ibgp_router02

# View BGP tables
$ birdc show route

# Test BGP connectivity
$ birdc show bgp neighbors
```

### Troubleshooting

**"No peers discovered"**
- Verify NetBox has multiple devices with role = "Router"
- Check NETBOX_API and NETBOX_TOKEN are set
- Run: `ansible-inventory -i inventory/netbox.yml --list`

**"BIRD config validation fails"**
- Manual test: `bird -p -c /etc/bird/bird.conf`
- Check jinja2 template syntax in `roles/bird/templates/`
- Run playbook with `-vvv` to see generated configs

**"BGP peers not connecting"**
- Verify loopback connectivity: `ping <peer_loopback>`
- Check firewall allows BGP port 179: `netstat -tuln | grep 179`
- Verify `netbox_cf_loopback_ip` is set for all peers
- Check BFD status: `birdc show bfd sessions`

**"Route reflector not working"**
- Verify RR has `netbox_cf_bgp_role: "rr"` in NetBox
- RR should peer with all clients and other RRs
- Clients should NOT peer with each other
- Test: `birdc show route where source = BGP`

---

## Role: `gre`

Configures Generic Routing Encapsulation tunnels (optional, advanced).

### Purpose

- Allocates tunnel IP pairs dynamically from NetBox
- Establishes GRE tunnels between peers
- Enables layer 3 connectivity over existing network

### Prerequisites

- Loopback IP configured on router
- NetBox prefix defined for tunnel IPs
- GRE module loaded on kernel

### Configuration Process

1. Query NetBox for loopback interface IP
2. Allocate two IP addresses from tunnel prefix
3. Create GRE interface with tunnel config
4. Bring up GRE interface

### Variables

**From NetBox:**
- `netbox_url` — NetBox URL
- `netbox_token` — API token
- `gre_tunnel_prefix_id` — NetBox prefix ID for tunnel IPs
- `loopback_ip` — Router loopback (tunnel endpoint)

### Example Generated Config

```bash
# Allocate tunnel IPs from prefix
Tunnel IP A: 172.16.0.1/31
Tunnel IP B: 172.16.0.3/31

# Create GRE interface
ip tunnel add gre0 mode gre local 10.0.0.1 remote 10.0.0.2 ttl 255
ip link set gre0 up
ip addr add 172.16.0.1/31 dev gre0

# Static route to peer loopback via GRE
ip route add 10.0.0.2/32 via 172.16.0.2 dev gre0
```

### Troubleshooting

**"Tunnel allocation fails"**
- Verify tunnel prefix exists in NetBox
- Check prefix has available IPs
- Test API: `curl "$NETBOX_API/api/ipam/prefixes/ID/available-ips/"`

**"GRE interface not up"**
- Check kernel module: `lsmod | grep gre`
- Verify endpoint IPs are reachable: `ping <remote_loopback>`
- Check MTU settings (GRE reduces MTU by 24 bytes)

---

## Shared Concepts

### Variable Composition

Variables come from multiple sources with this priority:

1. **Playbook arguments** (highest priority): `ansible-playbook ... -e var=value`
2. **Host-specific vars** (from NetBox via inventory plugin)
3. **Group vars** (from `group_vars/all.yml`)
4. **Role defaults** (lowest priority)

### Custom Fields

NetBox custom fields are mapped to Ansible variables:
- NetBox field `loopback_ip` → Ansible `netbox_cf_loopback_ip`
- NetBox field `bgp_role` → Ansible `netbox_cf_bgp_role`
- Etc.

This mapping happens in `inventory/netbox.yml` via the `compose` section.

### Idempotency

All roles are idempotent:
- Running playbook multiple times = same result
- No duplicate configs or side effects
- Safe for continuous deployment

### Service Management

All roles use systemd handlers:
- Config changes automatically trigger service reload/restart
- No manual service management needed
- Handlers ensure atomic service updates

---

## Adding Custom Configuration

### Extend BIRD Role

To add custom BIRD configuration:

1. Create new template in `roles/bird/templates/custom.conf.j2`
2. Add task in `roles/bird/tasks/main.yml`:
   ```yaml
   - name: Deploy custom protocol config
     template:
       src: custom.conf.j2
       dest: /etc/bird/generated/custom.conf
     notify: reload bird
   ```

### Extend WireGuard Role

To add custom WireGuard config:

1. Modify `roles/wireguard/templates/wg0.conf.j2` or create new config
2. Handle config file permissions (600)
3. Ensure handler triggers restart

See role template files for detailed structure and Jinja2 filters used.
