# Workflow Guide: Adding and Managing Routers

This guide provides step-by-step instructions for common tasks: adding routers, managing BGP topology, and troubleshooting deployments.

## Quick Reference: Task Matrix

| Task | Steps | Time |
|------|-------|------|
| Add new router | 1–10 (Setup in NetBox) + 11–14 (Deploy) | ~30 min |
| Change BGP role (client ↔ RR) | Update NetBox custom field + redeploy | ~5 min |
| Add WireGuard peer | Add to `wg_peers` list in NetBox + redeploy | ~5 min |
| Scale to 10+ routers | Use route reflector topology (Step 18) | depends |
| Troubleshoot peer connection | Follow Troubleshooting section | ~15 min |

---

## Workflow 1: Add a New Router (Full Setup)

### Phase 1: NetBox Configuration (15 minutes)

**Assumptions:**
- NetBox running and accessible
- Custom fields created (see [NetBox Setup Guide](NETBOX_SETUP.md))
- Organization, site, and device role exist
- Loopback prefix created

**Step 1: Create router device in NetBox**

1. Navigate to: **Devices → Add Device**
2. Fill in:
   - **Name:** `router04` (must match hostname/SSH name)
   - **Device Type:** Select router type (e.g., "Generic Router")
   - **Device Role:** `Router`
   - **Site:** Your site
   - **Status:** Active
   - **Primary IPv4 Address:** Management IP (for SSH access)
3. Click **Create**

**Step 2: Add custom field values**

1. On device page, scroll down to **Custom Fields**
2. Set:
   - **loopback_ip:** `10.0.0.4` (must be unique, from your prefix)
   - **bgp_role:** `client` (or `rr` for route reflector)
   - **wg_peers:** `["router01", "router02", "router03"]` (JSON list)
   - Leave **wg_private_key** and **wg_public_key** empty (auto-generated)
3. Click **Save**

**Step 3: Create loopback interface**

1. On device page, go to: **Interfaces → Add Interface**
2. Set:
   - **Name:** `lo`
   - **Type:** Virtual
   - **MTU:** 65535
   - **Enabled:** Yes
3. Click **Create**

**Step 4: Assign loopback IP**

1. Navigate to: **IP Address Management → IP Addresses → Add IP Address**
2. Set:
   - **Address:** `10.0.0.4/32` (same as custom field)
   - **Interface:** `router04 / lo`
   - **Status:** Active
3. Click **Create**

**Step 5: Verify in NetBox**

1. Go to **Devices → router04**
2. Confirm you see:
   - Device role: `Router`
   - Custom fields filled in
   - Loopback interface with IP

**Checkpoint:** Device is discoverable by Ansible inventory plugin.

### Phase 2: SSH & OS Configuration (10 minutes)

**Assumptions:**
- Debian/Ubuntu Linux on router
- Root or sudo access

**Step 6: SSH to router and verify connectivity**

```bash
# From your workstation
ssh ubuntu@<router04_management_ip>

# Verify SSH works
whoami  # Should return your user
```

**Step 7: Verify OS prerequisites**

```bash
# Check kernel version
uname -r  # 5.4+ recommended

# Check if WireGuard module available
modprobe wireguard  # Should succeed silently

# Check if BIRD package available
apt update && apt-cache search bird  # Should show bird package
```

**Step 8: Ensure sudo without password (recommended)**

Add to `/etc/sudoers` (use `visudo`):
```sudoers
ansible ALL=(ALL) NOPASSWD: ALL
```

### Phase 3: Ansible Inventory Discovery (5 minutes)

**Assumptions:**
- `bgp_auto` repo cloned locally
- `NETBOX_API` and `NETBOX_TOKEN` environment variables set

**Step 9: Verify inventory discovers the new router**

```bash
cd /path/to/bgp_auto

# List all discovered hosts
ansible-inventory -i inventory/netbox.yml --list | jq '.all.hosts'

# Should include "router04"
ansible-inventory -i inventory/netbox.yml --list | jq '.router04'
```

Expected output includes:
```json
{
  "ansible_host": "10.x.x.x",
  "device_role": {"slug": "router"},
  "loopback_ip": "10.0.0.4",
  "netbox_cf_bgp_role": "client",
  "netbox_cf_wg_peers": ["router01", "router02", "router03"],
  ...
}
```

**If router04 not found:**
- Check device role is exactly `Router` (slug: `router`)
- Check device status is `Active`
- Verify API token: `curl -H "Authorization: Token $NETBOX_TOKEN" "$NETBOX_API/api/dcim/devices/?name=router04"`

**Step 10: Test SSH connectivity from Ansible**

```bash
ansible -i inventory/netbox.yml router04 -m ping
```

Expected output:
```
router04 | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
```

**If ping fails:**
- Verify SSH key auth works: `ssh -v ubuntu@<ip>`
- Check Ansible inventory has correct `ansible_host`
- Verify firewall allows SSH (port 22)

### Phase 4: Deploy Ansible Playbook (5 minutes)

**Step 11: Run playbook with dry-run mode**

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
  -e device_name=router04 \
  --become \
  --check  # Dry run only
```

Review output for:
- Package installation steps
- Config file locations
- No errors or failures

**Step 12: Run actual playbook**

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
  -e device_name=router04 \
  --become
```

Expected tasks:
- Install wireguard package
- Generate WireGuard keys (first run only)
- Deploy WireGuard config
- Build BIRD peer list
- Deploy BIRD iBGP config
- Restart BIRD/WireGuard services

**Step 13: Verify configs deployed**

SSH to router04:

```bash
# Check WireGuard config
sudo cat /etc/wireguard/wg0.conf

# Check BIRD configs
sudo ls -la /etc/bird/generated/
sudo cat /etc/bird/generated/ibgp.conf

# Check BIRD status
sudo systemctl status bird
```

### Phase 5: Verification (10 minutes)

**Step 14: Verify WireGuard is up**

```bash
# On router04
sudo wg show

# Should show:
# interface: wg0
# public key: [key]
# private key: [key]
# peers: 3 (router01, router02, router03)
```

**If WireGuard down:**
```bash
sudo systemctl restart wg-quick@wg0
sudo systemctl status wg-quick@wg0
```

**Step 15: Verify BGP is configured**

```bash
# On router04
sudo birdc show protocols

# Should show:
# BIRD 2.x ready
# name       proto      table  state  since    info
# ibgp_router01  bgp  master   idle   [time]  Passive
# ibgp_router02  bgp  master   idle   [time]  Passive
# ...
```

**If BGP sessions are "down":**
- Verify loopback IPs reachable: `ping 10.0.0.1`
- Wait 30 seconds for BFD to converge
- Check BIRD logs: `sudo journalctl -u bird -f`

**Step 16: Verify WireGuard keys in NetBox**

1. Go to **Devices → router04 → Custom Fields**
2. Verify:
   - `wg_private_key` now has a value (auto-generated, encrypted in NetBox)
   - `wg_public_key` now has a value
   - If still empty, keys didn't sync (see troubleshooting)

**Step 17: Verify peer connectivity**

On router04, check peer addresses:

```bash
# WireGuard peer handshake
sudo wg show

# Should show "latest handshake" for each peer within 30 seconds

# BGP neighbor state (after ~60 seconds)
sudo birdc show protocols ibgp_router01 all

# Should show 'established' or 'connect' state
```

**Checkpoint:** Router04 is fully deployed and connecting to peers.

---

## Workflow 2: Scale to 10+ Routers with Route Reflectors

When you have many routers, use **route reflector (RR) topology** to avoid BGP mesh explosion.

### Current Problem (Full Mesh)

```
With 10 routers: Each has 9 BGP peers = high memory/CPU
BGP scaling formula: BGP peers = N - 1 (N = number of routers)
```

### Solution: Route Reflector Topology

```
Recommended:
- 2 Route Reflectors (RR)
- All other routers: clients

Result: Each client has 2 peers (the RRs)
BGP scaling: Much better for 10+ routers
```

### Implementation

**Step 1: Choose 2 routers as route reflectors**

```
router01 → bgp_role: rr
router02 → bgp_role: rr
router03–router10 → bgp_role: client
```

**Step 2: Update custom fields in NetBox**

For router01:
- `bgp_role`: `rr`
- `wg_peers`: `["router02", "router03", ..., "router10"]` (all others)

For router02:
- `bgp_role`: `rr`
- `wg_peers`: `["router01", "router03", ..., "router10"]` (all others)

For router03–router10:
- `bgp_role`: `client`
- `wg_peers`: `["router01", "router02"]` (RRs only)

**Step 3: Redeploy playbook**

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become
```

**Step 4: Verify RR topology**

On a client router:

```bash
sudo birdc show protocols

# Should show only 2 BGP peers:
# ibgp_router01  bgp  master  established
# ibgp_router02  bgp  master  established
```

On an RR:

```bash
sudo birdc show protocols

# Should show many BGP peers (clients + other RRs)
```

---

## Workflow 3: Add a WireGuard Peer to Existing Router

**Scenario:** Router04 should add router05 as a WireGuard peer (already deployed).

**Step 1:** Update NetBox custom field

1. Go to **Devices → router04**
2. In Custom Fields, update **wg_peers**:
   - From: `["router01", "router02", "router03"]`
   - To: `["router01", "router02", "router03", "router05"]`
3. Click **Save**

**Step 2:** Redeploy

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
  -e device_name=router04 \
  --become
```

**Step 3:** Verify

```bash
# On router04
sudo wg show

# Should now show 4 peers (router01, router02, router03, router05)
```

---

## Workflow 4: Change Router from Client to Route Reflector

**Scenario:** Promote router04 to route reflector.

**Step 1:** Update NetBox custom field

1. Go to **Devices → router04**
2. Set **bgp_role**: `rr`
3. Click **Save**

**Step 2:** Redeploy

```bash
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become
```

**Step 3:** Verify

```bash
# On router04
sudo birdc show protocols

# Should now have "route reflector client" config
```

**Step 4:** Update peer WireGuard lists (if needed)

If using RR topology, update WireGuard peers for existing routers to include router04 as needed.

---

## Troubleshooting Guide

### Issue 1: Router not discovered by Ansible

**Symptoms:**
- `ansible-inventory` returns empty list or no router04
- Playbook: "No hosts matched"

**Diagnosis:**

```bash
# Check if device exists in NetBox API
curl -H "Authorization: Token $NETBOX_TOKEN" \
  "$NETBOX_API/api/dcim/devices/?name=router04"

# Should return: {"count": 1, "results": [{...}]}
```

**Solutions:**

1. **Device exists but not discovered:**
   - Check device role is `Router` (slug must be lowercase `router`)
   - Verify device status is `Active`, not `Offline` or `Failed`
   - Clear inventory cache: `rm -rf /tmp/ansible_netbox_*`

2. **Device doesn't exist in API:**
   - Verify you created device in NetBox UI
   - Check network connectivity to NetBox
   - Verify NETBOX_API URL: `curl $NETBOX_API/api/dcim/devices/`

3. **API token invalid:**
   ```bash
   curl -H "Authorization: Token $NETBOX_TOKEN" \
     "$NETBOX_API/api/dcim/devices/" 2>&1 | head
   # Should return 200 OK, not 401 Unauthorized
   ```

### Issue 2: SSH connection fails

**Symptoms:**
- `ansible -i inventory/netbox.yml router04 -m ping` fails
- Error: "unreachable" or "connection timeout"

**Diagnosis:**

```bash
# Test manual SSH
ssh -v ubuntu@<ansible_host_from_inventory>

# Check what Ansible thinks is the host
ansible-inventory -i inventory/netbox.yml --list | jq '.router04.ansible_host'
```

**Solutions:**

1. **Wrong IP address:**
   - Verify device's **Primary IPv4 Address** is set in NetBox
   - Verify it's the management IP (SSH reachable)
   - Update: **Devices → router04 → Primary IPv4 Address**

2. **SSH key not trusted:**
   - Add router SSH key to `~/.ssh/known_hosts`:
     ```bash
     ssh-keyscan <router_ip> >> ~/.ssh/known_hosts
     ```
   - Or configure Ansible to ignore unknown hosts in `ansible.cfg`

3. **SSH user wrong:**
   - Default user from NetBox might not match your system
   - Override in `group_vars/all.yml`:
     ```yaml
     ansible_user: ubuntu  # or root, debian, etc.
     ```

### Issue 3: WireGuard keys not syncing to NetBox

**Symptoms:**
- First playbook run completes but `wg_private_key` field still empty in NetBox
- Error message: "Device not found" or "401 Unauthorized"

**Diagnosis:**

```bash
# Check playbook logs for WireGuard key sync step
# Run with: -vvv flag
ansible-playbook ... -vvv | grep -A 5 "Update NetBox with WG keys"
```

**Solutions:**

1. **API token lacks write permission:**
   - Regenerate NetBox token with "write" scope
   - Test: `curl -X PATCH -H "Authorization: Token $TOKEN" ...`

2. **Custom fields don't exist:**
   - Verify `wg_private_key` and `wg_public_key` exist in NetBox
   - Path: **Administration → Custom Fields**
   - Check they're assigned to DCIM / Device content type

3. **Device not found by name:**
   - Verify device name exactly matches inventory hostname
   - Query NetBox: `curl "$NETBOX_API/api/dcim/devices/?name=router04"`

4. **SSL certificate issues:**
   - If using self-signed NetBox cert:
     ```bash
     export NETBOX_VALIDATE_CERTS=false
     ansible-playbook ...
     ```

### Issue 4: BGP peers not connecting

**Symptoms:**
- `birdc show protocols` shows BGP as "idle" or "connect" (not "established")
- Peers not exchanging routes

**Diagnosis:**

```bash
# On router
sudo birdc show protocols ibgp_router02 all

# Look for error messages or state transitions

# Test loopback reachability
ping 10.0.0.2

# Check BGP port listening
sudo ss -tuln | grep 179
```

**Solutions:**

1. **Loopback IPs not reachable:**
   - Configure loopback IPs on peer routers (usually automatic from OS)
   - Test: `ping <peer_loopback>`
   - Check routing: `ip route show | grep 10.0`

2. **Firewall blocking BGP port 179:**
   - Open port 179/tcp for BGP:
     ```bash
     sudo ufw allow 179/tcp
     ```
   - Or update security group in cloud provider

3. **BFD not converging:**
   - Wait 30–60 seconds for BFD to establish
   - Check: `sudo birdc show bfd sessions`
   - If BFD fails, BGP takes longer to converge

4. **Router role wrong in NetBox:**
   - Verify device role slug is `router` (lowercase)
   - BIRD filters peers by `device_role.slug = 'router'`

5. **Loopback IP custom field not set:**
   - Verify **netbox_cf_loopback_ip** is set on each device
   - BIRD generates peer config from this field

### Issue 5: Playbook deploy fails midway

**Symptoms:**
- Playbook starts but fails with error
- Partial config deployed

**Diagnosis:**

```bash
# Rerun with verbose output
ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become -vvv
# Look for task name and error message
```

**Common failures:**

| Error | Cause | Fix |
|-------|-------|-----|
| "Module not found: wireguard" | Package installation failed | SSH manually, run `apt install wireguard` |
| "Failed to validate BIRD config" | Syntax error in template | Check jinja2 template, test `bird -p -c` manually |
| "Device not found in NetBox" | WireGuard key sync step | Verify device name matches, API token valid |
| "Permission denied" | Missing sudo or sudoers config | Add `ansible ALL=(ALL) NOPASSWD: ALL` to sudoers |
| "Connection timeout" | SSH unreachable | Verify IP, firewall, SSH keys |

### Issue 6: Configuration changes not applied

**Symptoms:**
- Updated custom field in NetBox but config didn't change
- Playbook runs but "ok" instead of "changed"

**Diagnosis:**

```bash
# Check current config on router
sudo cat /etc/bird/generated/ibgp.conf
sudo cat /etc/wireguard/wg0.conf

# Check if NetBox values are really updated
ansible-inventory -i inventory/netbox.yml --list | jq '.router04 | .netbox_cf_bgp_role'
```

**Solutions:**

1. **Inventory cache stale:**
   ```bash
   rm -rf /tmp/ansible_netbox_*
   ansible-inventory -i inventory/netbox.yml --list > /dev/null
   ```

2. **Custom field not synced to Ansible:**
   - Verify field name in `inventory/netbox.yml` `compose` section
   - Re-run: `ansible-inventory -i inventory/netbox.yml --list | jq '.router04'`

3. **Run playbook again:**
   ```bash
   # Even though Ansible says "ok", changes may need reload
   ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become
   ```

---

## Success Checklist

After deploying a new router:

- [ ] Device created in NetBox with correct name
- [ ] Custom fields filled in (loopback_ip, bgp_role, wg_peers)
- [ ] Loopback interface created with IP address
- [ ] SSH connectivity verified: `ssh ubuntu@<ip>`
- [ ] Ansible inventory discovers device: `ansible-inventory ... --list`
- [ ] Ansible ping succeeds: `ansible -i ... router04 -m ping`
- [ ] Dry-run completes: `ansible-playbook ... --check`
- [ ] Playbook deploys: `ansible-playbook ...`
- [ ] WireGuard up: `sudo wg show` shows interfaces
- [ ] WireGuard keys in NetBox: custom fields populated
- [ ] BGP peers up: `sudo birdc show protocols` shows "established"
- [ ] Peer connectivity verified: WireGuard handshakes and BGP routes exchanged

---

## Next Steps

- **Monitor deployments:** Set up Prometheus/Grafana for BIRD/WireGuard metrics
- **Backup configs:** Periodically export device configs from NetBox
- **Disaster recovery:** Document and test NetBox and router recovery procedures
- **CI/CD integration:** Automate validation checks in GitLab/GitHub CI

See [Architecture Guide](ARCHITECTURE.md) for more advanced topics.
