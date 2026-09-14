# Role Documentation

Detailed documentation of each Ansible role in the `bgp_auto` playbook.

## Role: `wireguard`

Configures WireGuard VPN tunnels for encrypted peer-to-peer communication.

### WireGuard Purpose

- Generates or retrieves the WireGuard private/public key pair
- Syncs keys to NetBox custom fields for persistence
- Generates WireGuard interface config with the peer list
- Manages the WireGuard systemd service

### WireGuard Prerequisites

- WireGuard package installed, handled by the playbook
- WireGuard kernel module loaded, automatic on modern kernels
- All peers must be reachable via the endpoint IP

### WireGuard Configuration Files Generated

**Location:** `/etc/wireguard/wg0.conf`

```ini
[Interface]
Address = 10.0.0.1/32          # From netbox_cf_loopback_ip
PrivateKey = xxx...            # Private key, no log
ListenPort = 51820

[Peer]
PublicKey = yyy...             # Peer public key
Endpoint = 192.168.1.2:51820   # Peer management IP
AllowedIPs = 10.0.0.2/32       # Peer loopback IP
PersistentKeepalive = 25       # Keep connection alive
```

### WireGuard Variables

#### Required

- `wg_port` - Listen port, default `51820`
- `netbox_cf_loopback_ip` - Router's loopback IP
- `netbox_cf_peers` - List of peer hostnames from NetBox; falls back to `netbox_cf_wg_peers` if needed

#### Auto-Generated

- `wg_private_key` - Generated if not in NetBox
- `wg_public_key` - Derived from the private key

#### Optional

- `wg_port_override` - Override listen port per host
- `NETBOX_VALIDATE_CERTS` - SSL verification, default `true`

### WireGuard Key Sync Process

1. If `netbox_cf_wg_private_key` is empty or undefined:
   - Generate a new private key with `wg genkey`
   - Derive a public key with `wg pubkey`
2. Query the NetBox API for the device ID by hostname
3. PATCH device custom fields with the new keys
4. Keys are stored in NetBox for the next run

**Note:** Keys are marked `no_log: true` in playbook output for security.

### WireGuard Handlers

**Handler:** `Restart WireGuard`

- Triggered when `/etc/wireguard/wg0.conf` changes
- Runs `systemctl restart wg-quick@wg0`
- Service automatically starts on boot

### WireGuard Troubleshooting

#### Device not found in NetBox

- Verify the device name matches the inventory hostname exactly
- Check `NETBOX_TOKEN` has device read/write permissions
- Test with `curl "$NETBOX_API/api/dcim/devices/?name=router01"`

#### No peers connecting

- Verify `netbox_cf_peers` or legacy `netbox_cf_wg_peers` is a valid JSON list such as `["router02", "router03"]`
- Check peer hostnames exist in inventory
- Test connectivity with `ping <peer_endpoint>`

#### Keys not syncing to NetBox

- Verify `netbox_cf_wg_private_key` and `netbox_cf_wg_public_key` custom fields exist
- Check API token permissions for POST and PATCH on devices
- Run with `-vvv` to see API responses

---

## Role: `bird`

Configures BIRD Internet Routing Daemon for BGP, OSPF, and BFD.

### BIRD Purpose

- Discovers all router peers in NetBox dynamically
- Generates full-mesh or route-reflector iBGP topology
- Configures OSPF for loopback reachability and exports the managed local address for BGP/OSPF
- Enables BFD for fast failure detection

### BIRD Prerequisites

- BIRD installed, usually `apt install bird bird2`
- Loopback IP configured on the router
- All peers reachable on the loopback network

### BIRD Configuration Files Generated

**Location:** `/etc/bird/generated/`

#### File: `ibgp.conf`

```text
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

```text
protocol ospf v2 ospf_underlay {
  ipv4 {
    table master4;
        import all;
        export all;
    };
    area 0 {
    interface "*gre*" {
      type pointopoint;
      hello 1;
      dead 4;
    };
    };
}
```

#### File: `direct.conf`

```text
protocol direct local_connected {
  interface "lo", "local";
}
```

#### File: `bfd.conf`

```text
protocol bfd {
    multihop {
        neighbor 10.0.0.2 dev lo;
    };
}
```

### BIRD Variables

#### BIRD From NetBox

- `bgp_as` - BGP autonomous system from `group_vars/all.yml`
- `loopback_ip` - Router loopback IP from `netbox_cf_loopback_ip`
- `netbox_cf_bgp_role` - `client` or `rr` for route reflector
- `netbox_cf_ospf_enabled` - Enable OSPF, default `true`
- `local_ip` - Managed local service IP. Has no default; the GRE role sets it to
  `ansible_host` before use, and it can be overridden with `-e local_ip=<ip>`.
  An address inside `purged_ip_prefixes` is never assigned - see
  [Single Server Runbook](SINGLE_SERVER.md#retired-prefixes).

#### Discovered at Runtime

- `all_nodes` - All routers with role `Router` in NetBox
- `bgp_peers` - All nodes except self
- `route_reflectors` - Nodes with `bgp_role = "rr"`
- `effective_bgp_peers` - Final peer list based on topology

### BIRD Topology Logic

#### Full Mesh, default

```text
If no route reflectors are defined:
  -> Each router peers with all other routers
  -> BGP convergence is slower, but more resilient
```

#### Route Reflector

```text
If any router has bgp_role = "rr":
  -> Clients, bgp_role = "client", peer only with RRs
  -> RRs peer with each other and clients
  -> Reduces BGP churn and enables easier scaling
```

### BIRD Handlers

**Handler:** `reload bird`

- Triggered when any config file changes
- Runs `systemctl reload bird`
- Performs a graceful reload with no connection drops

### BIRD Validation

Before reload, the playbook runs:

```bash
bird -p -c /etc/bird/bird.conf
```

### BIRD Status Checks

SSH to the router and check:

```bash
birdc show protocols
birdc show protocols ibgp_router02
birdc show route
birdc show bgp neighbors
```

### BIRD Troubleshooting

#### No peers discovered

- Verify NetBox has multiple devices with role `Router`
- Check `NETBOX_API` and `NETBOX_TOKEN` are set
- Run `ansible-inventory -i inventory/netbox.yml --list`

#### BIRD config validation fails

- Run `bird -p -c /etc/bird/bird.conf` manually
- Check Jinja2 template syntax in `roles/bird/templates/`
- Run the playbook with `-vvv` to see the generated configs

#### BGP peers not connecting

- Verify loopback connectivity with `ping <peer_loopback>`
- Check the firewall allows BGP port 179 with `netstat -tuln | grep 179`
- Verify `netbox_cf_loopback_ip` is set for all peers
- Check BFD status with `birdc show bfd sessions`

#### Route reflector not working

- Verify the RR has `netbox_cf_bgp_role: "rr"` in NetBox
- RR should peer with all clients and other RRs
- Clients should not peer with each other
- Test with `birdc show route where source = BGP`

---

## Role: `gre`

Configures Generic Routing Encapsulation tunnels, optional and advanced.

### GRE Purpose

- Allocates tunnel IP pairs dynamically from NetBox
- Establishes GRE tunnels between peers
- Enables layer 3 connectivity over the existing network

### GRE Prerequisites

- Loopback IP configured on the router
- NetBox prefixes tagged with the `gre-tunnels` role
- GRE module loaded on the kernel

### GRE Configuration Process

1. Query NetBox for the loopback interface IP
2. Discover the next available `/31` GRE prefixes from NetBox
3. Allocate two IP addresses from each tunnel prefix
4. Create the GRE interface with the tunnel config
5. Bring up the GRE interface

### GRE Variables

#### GRE From NetBox

- `netbox_url` - NetBox URL
- `netbox_token` - API token
- `gre_tunnel_prefix_role` - NetBox prefix role used to find GRE tunnel `/31` prefixes
- `loopback_ip` - Router loopback, the tunnel endpoint

### GRE Example Generated Config

```bash
# Allocate tunnel IPs from GRE prefixes
Tunnel IP A: 172.16.0.1/31
Tunnel IP B: 172.16.0.3/31

# Create GRE interface
ip tunnel add gre0 mode gre local 10.0.0.1 remote 10.0.0.2 ttl 255
ip link set gre0 up
ip addr add 172.16.0.1/31 dev gre0

# Static route to peer loopback via GRE
ip route add 10.0.0.2/32 via 172.16.0.2 dev gre0
```

### GRE Troubleshooting

#### Tunnel allocation fails

- Verify GRE prefixes exist in NetBox with role `gre-tunnels`
- Check there are enough `/31` prefixes for the number of hosts
- Test the API with `curl "$NETBOX_API/api/ipam/prefixes/?role=gre-tunnels&limit=0"`

#### GRE interface not up

- Check the kernel module with `lsmod | grep gre`
- Verify endpoint IPs are reachable with `ping <remote_loopback>`
- Check MTU settings, because GRE reduces MTU by 24 bytes

---

## Shared Concepts

### Variable Composition

Variables come from multiple sources in this priority order:

1. **Playbook arguments**, highest priority, for example `ansible-playbook ... -e var=value`
2. **Host-specific vars** from NetBox via the inventory plugin
3. **Group vars** from `group_vars/all.yml`
4. **Role defaults**, lowest priority

### Custom Fields

NetBox custom fields are mapped to Ansible variables:

- NetBox field `loopback_ip` -> Ansible `netbox_cf_loopback_ip`
- NetBox field `bgp_role` -> Ansible `netbox_cf_bgp_role`
- NetBox field `peers` -> Ansible `netbox_cf_peers`

This mapping happens in `inventory/netbox.yml` via the `compose` section.

### Idempotency

All roles are idempotent:

- Running the playbook multiple times produces the same result
- No duplicate configs or side effects
- Safe for continuous deployment

### Service Management

All roles use systemd handlers:

- Config changes automatically trigger service reload or restart
- No manual service management is needed
- Handlers ensure atomic service updates

## Adding Custom Configuration

### Extend BIRD Role

To add custom BIRD configuration:

1. Create a new template in `roles/bird/templates/custom.conf.j2`
2. Add a task in `roles/bird/tasks/main.yml`:

   ```yaml
   - name: Deploy custom protocol config
     template:
       src: custom.conf.j2
       dest: /etc/bird/generated/custom.conf
     notify: reload bird
   ```

### Extend WireGuard Role

To add custom WireGuard config:

1. Modify `roles/wireguard/templates/wg0.conf.j2` or create a new config
2. Handle config file permissions, 600
3. Ensure the handler triggers a restart

See the role template files for detailed structure and the Jinja2 filters used.
