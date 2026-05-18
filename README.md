# bgp_auto

Automated BGP, WireGuard, and GRE tunnel configuration using Ansible and NetBox.

This repository automates the deployment of network infrastructure including:
- **iBGP mesh** with route reflector support using BIRD
- **WireGuard VPN** tunnels with automatic key management
- **GRE tunnels** with dynamic IP allocation from NetBox

All configuration is **source-controlled in NetBox** — devices, custom fields, and IP addresses are defined once and propagated automatically.

## Quick Start

### Prerequisites

- **Ansible:** 2.9+ recommended
- **Python:** 3.6+
- **SSH access** to target routers with sudo privileges
- **NetBox instance** with API access
- Environment variables set:
  ```bash
  export NETBOX_API="https://netbox.example.com"
  export NETBOX_TOKEN="your-api-token"
  ```

### Run the Deployment

```bash
# From repo root, deploy to all devices
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become

# Dry-run to preview changes
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become --check

# Target a single device
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml -e device_name=router01 --become

# Verbose output for debugging
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become -vvv
```

## Documentation

- **[Architecture](docs/ARCHITECTURE.md)** — How the system works end-to-end
- **[NetBox Setup Guide](docs/NETBOX_SETUP.md)** — Create and configure NetBox objects for each peer
- **[Role Documentation](docs/ROLE_DOCUMENTATION.md)** — Detailed breakdown of each Ansible role
- **[Workflow Guide](docs/WORKFLOW_GUIDE.md)** — Step-by-step instructions to add new peers

## Project Structure

```
├── ansible.cfg                 # Ansible configuration
├── group_vars/all.yml         # Global variables (BGP AS, WireGuard port, etc.)
├── inventory/netbox.yml       # NetBox dynamic inventory plugin config
├── playbooks/deploy.yml       # Main deployment playbook
├── roles/
│   ├── bird/                  # iBGP/OSPF/BFD configuration
│   ├── wireguard/             # WireGuard VPN setup
│   └── gre/                   # GRE tunnel configuration
└── docs/                      # Documentation
```

## Key Features

- **NetBox-driven configuration**: All peer data lives in NetBox custom fields
- **Automatic iBGP mesh**: Discovers all routers and peers them together
- **Route reflector support**: Designated route reflectors reduce mesh complexity
- **WireGuard key sync**: Private/public keys automatically stored in NetBox
- **Configuration validation**: BIRD config validated before reload
- **Idempotent playbook**: Safe to run multiple times

## Configuration Hierarchy

1. **NetBox** is the source of truth (devices, IPs, custom fields)
2. **Inventory plugin** dynamically builds host inventory from NetBox
3. **Host facts** from NetBox populate role variables
4. **Roles** generate and deploy configuration files

See [Architecture](docs/ARCHITECTURE.md) for detailed flow diagrams and examples.