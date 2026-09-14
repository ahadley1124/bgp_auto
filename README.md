# bgp_auto

Automated BGP and GRE tunnel configuration using Ansible and NetBox.

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
  export BGP_AUTO_LOCAL_MACHINE="router01"
  ```

Set `BGP_AUTO_LOCAL_MACHINE` to the inventory name of the device you are running on when that device is managed by the same playbook. Ansible will use a local connection for that host instead of SSH.

### Environment Variables

- `NETBOX_API` or `NETBOX_URL`: NetBox API endpoint used by the inventory plugin and API calls.
- `NETBOX_TOKEN`: NetBox API token with read/write access as needed.
- `BGP_AUTO_LOCAL_MACHINE`: Optional inventory hostname that should use a local connection instead of SSH.

To keep the NetBox token out of your shell environment and history, store it in an
Ansible Vault and run through the wrapper — see
[Vault as Environment Storage](docs/VAULT_SETUP.md):

```bash
./scripts/with-vault-env.sh \
  ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become
```

### Run the Deployment

```bash
# From repo root, deploy to all devices
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become

# Dry-run to preview changes
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become --check

# Target a single device
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml -e device_name=router01 --become

# Run against a device from that device itself without SSHing back into it
export BGP_AUTO_LOCAL_MACHINE="router01"
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml -e device_name=router01 --become

# Verbose output for debugging
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become -vvv
```

### Enforce Retired Prefixes

`playbooks/purge_ips.yml` removes every address inside `purged_ip_prefixes` from
every interface on every host, without deploying BIRD or GRE configuration. It
verifies afterwards and fails if anything survived.

```bash
# Audit the fleet - reports what would be removed, changes nothing
ansible-playbook -i inventory/netbox.yml playbooks/purge_ips.yml --become --check

# Enforce across every server
ansible-playbook -i inventory/netbox.yml playbooks/purge_ips.yml --become

# One host
ansible-playbook -i inventory/netbox.yml playbooks/purge_ips.yml --become -e device_name=router01

# A different prefix, without editing group_vars
ansible-playbook -i inventory/netbox.yml playbooks/purge_ips.yml --become \
  -e '{"purged_ip_prefixes": ["198.51.100.0/24"]}'
```

> The purge removes matching addresses from **every** interface, physical ones
> included. Audit with `--check` before the first run — see
> [Single Server Runbook](docs/SINGLE_SERVER.md#before-the-first-purge-run-check-what-it-will-remove).

## Documentation

- **[Single Server Runbook](docs/SINGLE_SERVER.md)** — Set up, run, test, and recreate one server
- **[Vault as Environment Storage](docs/VAULT_SETUP.md)** — Keep NetBox credentials in an Ansible Vault
- **[Architecture](docs/ARCHITECTURE.md)** — How the system works end-to-end
- **[NetBox Setup Guide](docs/NETBOX_SETUP.md)** — Create and configure NetBox objects for each peer
- **[Role Documentation](docs/ROLE_DOCUMENTATION.md)** — Detailed breakdown of each Ansible role
- **[Workflow Guide](docs/WORKFLOW_GUIDE.md)** — Step-by-step instructions to add new peers

## Project Structure

```text
├── ansible.cfg                 # Ansible configuration
├── group_vars/all.yml         # Shared variables, loaded via vars_files (see note)
├── inventory/netbox.yml       # NetBox dynamic inventory plugin config
├── playbooks/
│   ├── deploy.yml             # Main deployment playbook
│   └── purge_ips.yml          # Retired-prefix enforcement, standalone
├── roles/
│   ├── bird/                  # iBGP/OSPF/BFD configuration
│   ├── common/                # Shared tasks (NetBox lookups, IP prefix purge)
│   └── gre/                   # GRE tunnel configuration
├── scripts/                   # Helper scripts (vault -> environment wrapper)
└── docs/                      # Documentation
```

> `group_vars/all.yml` is not on Ansible's group_vars search path for this layout
> — Ansible resolves `group_vars` next to the inventory and next to the playbook.
> Both playbooks load it explicitly with `vars_files`, and any new playbook must
> do the same.

## Key Features

- **NetBox-driven configuration**: All peer data lives in NetBox custom fields
- **Automatic iBGP mesh**: Discovers all routers and peers them together
- **Route reflector support**: Designated route reflectors reduce mesh complexity
- **Configuration validation**: BIRD config validated before reload
- **Idempotent playbook**: Safe to run multiple times
- **Retired prefix enforcement**: Addresses in `purged_ip_prefixes` are removed from
  every interface on every host, and are never reapplied from NetBox. Enforce on its
  own with `playbooks/purge_ips.yml`, which verifies the result and fails if an
  address survived

## Configuration Hierarchy

1. **NetBox** is the source of truth (devices, IPs, custom fields)
2. **Inventory plugin** dynamically builds host inventory from NetBox
3. **Host facts** from NetBox populate role variables
4. **Roles** generate and deploy configuration files

See [Architecture](docs/ARCHITECTURE.md) for detailed flow diagrams and examples.
