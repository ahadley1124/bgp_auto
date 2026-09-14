# Single Server Runbook

How to set up, run, test, and recreate **one** router from scratch.

The rest of the documentation describes the fleet. This guide is deliberately
scoped to a single machine, so you can stand one router up, prove it works,
and rebuild it without touching anything else.

Throughout, `router01` is the example server. Substitute your own device name —
it must match the NetBox device name exactly, because the inventory plugin uses
the NetBox name as the Ansible `inventory_hostname`.

---

## Contents

1. [What the playbook actually does to a server](#1-what-the-playbook-actually-does-to-a-server)
2. [Setup](#2-setup)
3. [Run](#3-run)
4. [Test](#4-test)
5. [Recreate](#5-recreate)
6. [Troubleshooting a single server](#6-troubleshooting-a-single-server)

---

## 1. What the playbook actually does to a server

Knowing the blast radius makes both testing and recreation straightforward.
`playbooks/deploy.yml` runs two roles, `bird` and `gre`, plus a pre-task purge.

| Step | Where it runs | What it creates or changes |
| ---- | ------------- | -------------------------- |
| Purge retired prefixes | `pre_tasks`, every host | Deletes every IPv4 address inside `purged_ip_prefixes` from **every** interface |
| `bird` role | every host | `/etc/bird/` and `/etc/bird/generated/`, then `ibgp.conf`, `ospf.conf`, `direct.conf`, `bfd.conf` inside `generated/` |
| `bird` role | every host | Validates with `bird -p -c /etc/bird/bird.conf`, reloads the `bird` service |
| dummy sync | every host | Creates `dummyN` interfaces and adds their NetBox IPs |
| `gre` role | every host with peers | Creates the `local` dummy interface, adds the loopback to `lo`, creates one `<peer>-gre` tunnel per peer |

Two things the playbook does **not** do, which you must provide yourself:

- It does not install BIRD, iproute2, or any package.
- It does not create `/etc/bird/bird.conf`. It only writes into
  `/etc/bird/generated/`. You must create the top-level `bird.conf` once, and it
  must include the generated directory (see [2.3](#23-prepare-the-target-server)).

### Retired prefixes

`playbooks/deploy.yml` defines:

```yaml
purged_ip_prefixes:
  - 23.190.216.0/24
```

Every address inside these prefixes is removed from every interface on every
host, on every run, and is never reapplied — the NetBox dummy sync and the GRE
role both skip addresses that fall inside a purged prefix. If NetBox still has
records in a purged prefix, the run prints a warning naming them; delete them in
NetBox to silence it.

### Before the first purge run, check what it will remove

The purge deletes matching addresses from **every** interface, including
physical ones. If a server is reachable over an address inside a purged prefix,
removing it will cut the connection mid-play.

Check the whole fleet before the first run:

```bash
ansible -i inventory/netbox.yml all -m command \
  -a 'ip -o -4 addr show to 23.190.216.0/24' --become
```

Anything reported on a management interface, or matching the host's
`ansible_host`, needs a new address before you deploy. A `--check` run also
lists exactly what would be removed, per host, without removing it.

The GRE role's `local_ip` is set to `ansible_host`, and the loopback comes from
NetBox. If either falls inside a purged prefix the playbook now skips that
assignment rather than reapplying it — so a router whose loopback is in a purged
prefix will not form BGP sessions. Move it to a live prefix in NetBox.

> The purge is defined in `playbooks/deploy.yml`, **not** in
> `group_vars/all.yml`. With the documented run command Ansible resolves
> `group_vars` next to the inventory and next to the playbook, so the repo-root
> `group_vars/all.yml` is never loaded and a value placed there has no effect.

---

## 2. Setup

### 2.1 Control node

The control node is wherever you run `ansible-playbook` — your workstation, a
bastion, or the router itself.

```bash
python3 -m venv ~/.venv/bgp_auto
source ~/.venv/bgp_auto/bin/activate

pip install "ansible-core==2.17.*" pynetbox netaddr
ansible-galaxy collection install netbox.netbox ansible.utils
```

`ansible.utils` provides the `ipaddr` filter used for prefix matching, and
`netbox.netbox` provides the dynamic inventory plugin. Both are required.

Then point the control node at NetBox:

```bash
export NETBOX_API="https://netbox.example.com"
export NETBOX_TOKEN="your-api-token"
```

To keep the token out of your shell history and environment, store it in an
Ansible Vault instead — see [Vault as Environment Storage](VAULT_SETUP.md).

### 2.2 Create the server in NetBox

NetBox is the source of truth; the playbook reads it and writes almost nothing
back. For one router you need:

| Object | Requirement |
| ------ | ----------- |
| Device | Name exactly matches the Ansible host you will target, e.g. `router01` |
| Device role | Slug `router` — peer discovery selects on `device_role.slug == 'router'` |
| Primary IP | Set; becomes `ansible_host` (the SSH target) |
| Custom field `loopback_ip` | The router's loopback, e.g. `10.0.0.1` |
| Custom field `ansible_user` | The SSH user; becomes `ansible_user` |
| Custom field `bgp_role` | `client`, or `rr` for a route reflector |
| Custom field `peers` | JSON list of peer device names, or empty for the first router |
| Interface `lo` | With the loopback IP assigned to it |

Full field-by-field settings are in the [NetBox Setup Guide](NETBOX_SETUP.md).

For GRE, also create prefixes with the role `gre-tunnels` for the playbook to
allocate /31 tunnel pairs from. `inventory/gre_blocked_prefixes_10.255.0.0_23.txt`
lists /31s the allocator will skip.

> The very first router has no peers. That is fine — the `gre` role ends cleanly
> for hosts with no peers, and BIRD is configured with no iBGP sessions. Add the
> second router before expecting any BGP adjacency.

### 2.3 Prepare the target server

On the router itself, as root:

```bash
# 1. Packages
apt-get update
apt-get install -y bird2 iproute2        # Debian/Ubuntu
# dnf install -y bird iproute             # RHEL/Fedora

# 2. Kernel modules for the interfaces the playbook creates
modprobe dummy
modprobe ip_gre
printf 'dummy\nip_gre\n' > /etc/modules-load.d/bgp_auto.conf

# 3. Forwarding — a router must forward
cat > /etc/sysctl.d/99-bgp_auto.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
sysctl --system
```

Create the top-level BIRD config once. The playbook never writes this file, but
its validation step reads it, so the deploy fails without it:

```bash
install -d -m 0755 /etc/bird /etc/bird/generated

cat > /etc/bird/bird.conf <<'EOF'
router id 10.0.0.1;          # this router's loopback

log syslog all;
protocol device { scan time 10; }
protocol kernel {
    ipv4 { import none; export all; };
    learn;
}
protocol kernel {
    ipv6 { import none; export all; };
    learn;
}

include "/etc/bird/generated/*.conf";
EOF
```

Set `router id` to this router's own loopback. The `include` line is what makes
everything the playbook generates take effect.

Finally, confirm the control node can reach the server with sudo:

```bash
ssh <ansible_user>@router01 'sudo -n true && echo sudo-ok'
```

If you are running the playbook **on** the router, export
`BGP_AUTO_LOCAL_MACHINE=router01` so Ansible uses a local connection instead of
SSHing back into itself.

---

## 3. Run

Everything below targets one host via `-e device_name=router01`. Run from the
repository root.

### 3.1 Pre-flight

```bash
# Playbook parses? (this is exactly what CI runs)
ansible-playbook --syntax-check -i localhost, playbooks/deploy.yml

# Does NetBox return the device, and with the right variables?
ansible-inventory -i inventory/netbox.yml --host router01

# Is it reachable, with privilege escalation?
ansible -i inventory/netbox.yml router01 -m ping --become
```

`ansible-inventory --host router01` should show `ansible_host`, `ansible_user`,
and `loopback_ip`. If `loopback_ip` is missing, the `loopback_ip` custom field is
unset in NetBox and BIRD will generate an empty iBGP config.

### 3.2 Dry run

Always dry-run first. Check mode reports what the purge *would* remove without
removing it:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
  -e device_name=router01 --become --check --diff
```

### 3.3 Deploy

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
  -e device_name=router01 --become
```

Useful variations:

```bash
# Only the routing daemon config
... --become --tags bird

# Only tunnels
... --become --tags gre

# Verbose, for debugging a failing task
... --become -vvv
```

The purge is tagged `always`, so it runs even under `--tags bird` or
`--tags gre`.

### 3.4 Supported overrides

| Extra var | Effect |
| --------- | ------ |
| `device_name=<host>` | Deploy to one inventory host instead of all |
| `bgp_as=<asn>` | Override the BGP AS number |
| `local_ip=<ip>` | Override the local service IP used by GRE |
| `gre_force_create=true` | Allow GRE creation even when NetBox data is incomplete |
| `purged_ip_prefixes=[...]` | Override the prefixes purged from every host |

| Environment variable | Effect |
| -------------------- | ------ |
| `NETBOX_API` / `NETBOX_URL` | NetBox API endpoint |
| `NETBOX_TOKEN` | NetBox API token |
| `BGP_AUTO_LOCAL_MACHINE` | Inventory hostname that uses a local connection |

---

## 4. Test

### 4.1 The playbook is idempotent

The strongest single check. Run the deploy twice; the second run should report
no removals in the purge task:

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
  -e device_name=router01 --become
```

Look for `Purging 0 address(es)` in the `Report addresses to be purged` task.

### 4.2 Retired prefixes are gone

This must return **nothing** on every server:

```bash
ssh router01 'ip -o -4 addr show to 23.190.216.0/24'
```

Any output means an address in a purged prefix is still configured. Re-run the
deploy; if it comes back, something outside this playbook is adding it (a
`systemd-networkd` unit, `/etc/network/interfaces`, or a cloud-init script).

### 4.3 Interfaces

```bash
ip -br addr show                 # local, lo, dummyN, <peer>-gre
ip -d tunnel show                # GRE endpoints, one per peer
ip -br link show type gre
```

Each peer should have one `<peer>-gre` tunnel, up, with a /31 from a
`gre-tunnels` prefix.

### 4.4 BIRD

```bash
# Config is valid — this is the same check the playbook runs before reloading
bird -p -c /etc/bird/bird.conf

# What was actually generated
ls -l /etc/bird/generated/
cat /etc/bird/generated/ibgp.conf

# Protocol state
birdc show protocols
birdc show protocols all ibgp_router02
birdc show route
birdc show bfd sessions
```

`birdc show protocols` should list `ibgp_<peer>` sessions in state
**Established**, `ospf_underlay`, `local_connected`, and `bfd1`.

An empty `ibgp.conf` means `loopback_ip` was undefined for this host or for
every peer — the template skips any peer where either side is missing.

### 4.5 Connectivity

```bash
ping -c3 <peer-gre-address>      # tunnel is passing traffic
ping -c3 -I <loopback> <peer-loopback>   # loopback reachability via BGP/OSPF
```

---

## 5. Recreate

Rebuilding one server from nothing. The NetBox records are the durable state —
keep them and the server is reproducible.

### 5.1 Tear down the old server

If you are reusing the machine rather than reprovisioning it, remove what the
playbook created. This does not touch NetBox:

```bash
# Stop routing first so neighbours converge away
systemctl stop bird

# Tunnels
for t in $(ip -o link show type gre | awk -F': ' '{print $2}'); do
    ip link delete "$t" || true
done

# Dummy interfaces, including `local`
for d in $(ip -o link show type dummy | awk -F': ' '{print $2}'); do
    ip link delete "$d" || true
done

# Generated config only — leave bird.conf in place
rm -f /etc/bird/generated/*.conf
```

To also drop the loopback address: `ip addr del <loopback>/32 dev lo`.

> Tunnels, dummy interfaces and addresses created by this playbook live in the
> kernel only — none of it survives a reboot unless you persisted it separately.
> A reboot is therefore a clean teardown of everything except `/etc/bird/`.

### 5.2 Rebuild

On a fresh machine, or the machine you just stripped:

1. Confirm NetBox still has the device, its role, primary IP, `lo` interface,
   loopback IP and peer list. This is the part you must not lose.
2. Redo [2.3 Prepare the target server](#23-prepare-the-target-server) —
   packages, kernel modules, sysctl, and `/etc/bird/bird.conf`.
3. Re-run the deploy:

   ```bash
   ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
     -e device_name=router01 --become --check --diff   # preview
   ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
     -e device_name=router01 --become                  # apply
   ```

4. Work through [4. Test](#4-test).

### 5.3 Recreating under a different name

If the replacement has a new name, rename or recreate the device in NetBox
**first**, and update the `peers` custom field on every device that referenced
the old name. Peer lists are by device name; a stale name silently drops the
session, because `ibgp.conf.j2` skips peers it cannot resolve.

### 5.4 What is not recreated automatically

| Item | Why | Action |
| ---- | --- | ------ |
| `/etc/bird/bird.conf` | The playbook only writes `generated/` | Recreate by hand, per [2.3](#23-prepare-the-target-server) |
| Packages, kernel modules, sysctl | Out of scope for the playbook | Recreate by hand, per [2.3](#23-prepare-the-target-server) |
| WireGuard config | The `wireguard` role exists but is **not** in `playbooks/deploy.yml` | Run the role explicitly if you need it |
| GRE /31 allocations | Held in NetBox, reused on rebuild | Nothing, as long as NetBox is intact |

---

## 6. Troubleshooting a single server

| Symptom | Likely cause | Check |
| ------- | ------------ | ----- |
| Host not in inventory | Device role slug is not `router`, or NetBox creds wrong | `ansible-inventory -i inventory/netbox.yml --list` |
| `Validate BIRD config` fails | `/etc/bird/bird.conf` missing or has no `include` line | `bird -p -c /etc/bird/bird.conf` on the server |
| `ibgp.conf` is empty | `loopback_ip` undefined for this host or all peers | `ansible-inventory -i inventory/netbox.yml --host router01` |
| No GRE tunnels created | Host has no peers, or no `gre-tunnels` prefixes free | Look for `Stop if no peers` in the run output |
| BGP stuck in `Connect` | Tunnel down, or loopback not reachable | `birdc show protocols all <name>`, `ping` the peer loopback |
| Purged address keeps returning | Something outside this playbook adds it | Search `/etc/network/`, `/etc/systemd/network/`, cloud-init |
| `ip: command not found` in tasks | `iproute2` not installed, or `ip` not on root's `PATH` | `ssh router01 'sudo ip -br addr'` |

For fleet-wide issues, see the
[Workflow Guide troubleshooting section](WORKFLOW_GUIDE.md#troubleshooting-guide).
