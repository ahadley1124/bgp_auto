# bgp_auto

This repository contains Ansible playbooks and roles to automate BGP and WireGuard configuration (roles: `bird`, `wireguard`).

**Prerequisites**

- **Ansible:** install a recent Ansible release (2.9+ recommended).
- **Inventory:** configure `inventory/netbox.yml` or provide your own inventory file.
- **Credentials & Access:** SSH access to target hosts and any required privilege escalation (sudo) credentials.
- **NetBox:** set `NETBOX_API` and `NETBOX_TOKEN` in your environment for inventory access. The WireGuard sync role also accepts `NETBOX_URL` as a fallback.
- **Variables:** adjust repo variables in `group_vars/all.yml` as needed.

**Quick Start — run the playbook**

- From the repository root, run:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml
```

- If the playbook requires privilege escalation, add `--become` and optionally `--ask-become-pass`:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become
```

- To do a dry-run (no changes applied), add `--check`:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --check
```

- To get more output for debugging, add `-v`, `-vv` or `-vvv`.

**Examples**

- Run only the `bird` role or limit hosts/groups using `--limit`:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --limit "routers"
```

- Target a single device by its NetBox device name by passing the `device_name` extra var:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml -e device_name=router01
```

The `device_name` value should match the NetBox device name (inventory hostname provided by the `netbox.netbox.nb_inventory` plugin). If not specified, the playbook runs against `all` hosts.

**Notes**

- This repository includes an `ansible.cfg` which will be used by Ansible when running from the repo root.
- Edit `group_vars/all.yml` to set global variables used by the roles.
- If you are using dynamic inventory or NetBox integration, ensure any required API tokens or inventory plugins are configured.

If you want, I can add usage examples for specific environments or add a section on configuring NetBox inventory.