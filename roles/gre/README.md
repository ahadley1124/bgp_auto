# GRE Tunnel Role

This role configures Generic Routing Encapsulation (GRE) tunnels between edge routers using NetBox as the source of truth for IP addresses and device information.

## Features

- Retrieves loopback IP from NetBox
- Allocates tunnel IP pairs from a NetBox prefix
- Configures GRE tunnel interfaces
- Supports multiple tunnel pairs (A and B)

## Requirements

- NetBox instance accessible at `netbox_url`
- Valid NetBox API token in `netbox_token`
- Inventory hostname must match device name in NetBox

## Variables

### Required

- `gre_tunnel_prefix_id`: NetBox prefix ID for tunnel IP allocation
- `gre_peer_name`: Hostname of the peer device for tunnel endpoint

### Optional

- `netbox_url`: NetBox API URL (default: http://netbox.local)
- `netbox_token`: NetBox API token (from `NETBOX_TOKEN` env var)

## Example

```yaml
- name: Configure GRE tunnels
  hosts: edge
  gather_facts: no
  
  vars:
    gre_tunnel_prefix_id: 123
    gre_peer_name: edge-2
  
  roles:
    - gre
```

## Generated Variables

The role sets the following facts that can be used in subsequent tasks:

- `gre_loopback_ip`: Loopback IP address from NetBox
- `gre_a_local`: Local IP for tunnel A
- `gre_a_peer`: Peer IP for tunnel A
- `gre_b_local`: Local IP for tunnel B
- `gre_b_peer`: Peer IP for tunnel B
