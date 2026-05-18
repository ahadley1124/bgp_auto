# NetBox Setup Guide

This guide explains exactly what needs to be created in NetBox for each peer (router) to work with `bgp_auto`.

## Prerequisites

- NetBox instance running and accessible
- Admin or API token with read/write permissions
- API base URL (e.g., `https://netbox.example.com`)
- Organization/site already created in NetBox

## Step 1: Create Device Custom Fields

Custom fields store peer-specific configuration. Create these fields in NetBox:

**Path:** Administration → Customization → Custom Fields

### Field 1: `loopback_ip`

| Setting | Value |
|---------|-------|
| **Name** | `loopback_ip` |
| **Content Type** | DCIM / Device |
| **Type** | Text |
| **Required** | Yes |
| **Description** | Loopback/management IP address for BGP and WireGuard |

### Field 2: `bgp_role`

| Setting | Value |
|---------|-------|
| **Name** | `bgp_role` |
| **Content Type** | DCIM / Device |
| **Type** | Text |
| **Required** | No |
| **Default** | `client` |
| **Description** | BGP role: `client` for full-mesh peers, `rr` for route reflector |

### Field 3: `wg_private_key`

| Setting | Value |
|---------|-------|
| **Name** | `wg_private_key` |
| **Content Type** | DCIM / Device |
| **Type** | Text |
| **Required** | No |
| **Description** | WireGuard private key (auto-generated on first run) |

### Field 4: `wg_public_key`

| Setting | Value |
|---------|-------|
| **Name** | `wg_public_key` |
| **Content Type** | DCIM / Device |
| **Type** | Text |
| **Required** | No |
| **Description** | WireGuard public key (auto-generated on first run) |

### Field 5: `wg_peers`

| Setting | Value |
|---------|-------|
| **Name** | `wg_peers` |
| **Content Type** | DCIM / Device |
| **Type** | Text (or JSON if available) |
| **Required** | Yes (if WireGuard enabled) |
| **Description** | JSON list of peer device names: `["router02", "router03"]` |

### Field 6: `ospf_enabled` (Optional)

| Setting | Value |
|---------|-------|
| **Name** | `ospf_enabled` |
| **Content Type** | DCIM / Device |
| **Type** | Boolean |
| **Required** | No |
| **Default** | `true` |
| **Description** | Enable OSPF adjacencies on loopback interfaces |

## Step 2: Create Device Role

**Path:** Administration → Device Types → Device Roles

Create a device role named **"Router"** (required for device filtering):

| Setting | Value |
|---------|-------|
| **Name** | `Router` |
| **Slug** | `router` |
| **Color** | (your choice) |

## Step 3: Create Device Type

**Path:** Administration → Device Types → Device Types

Create a device type (e.g., "Generic Router") to assign to routers:

| Setting | Value |
|---------|-------|
| **Name** | `Generic Router` |
| **Slug** | `generic-router` |
| **Manufacturer** | (any) |

## Step 4: Create Organization and Site

**Path:** Organization and Tenancy → Organizations / Sites

You need at least one organization and site:

| Setting | Value |
|---------|-------|
| **Organization** | `YourOrg` |
| **Site** | `PrimarySite` |

## Step 5: Create Loopback Interface Prefix

**Path:** IP Address Management → Prefixes

Create a prefix for loopback addresses (e.g., `10.0.0.0/24`):

| Setting | Value |
|---------|-------|
| **Prefix** | `10.0.0.0/24` |
| **Type** | Loopback |
| **Site** | `PrimarySite` |
| **Description** | Loopback addresses for routers |

## Step 6: Create Each Router Device

**Path:** Devices → Devices

For each router, create a device:

### Example: Router 01

| Setting | Value |
|---------|-------|
| **Name** | `router01` |
| **Device Type** | `Generic Router` |
| **Device Role** | `Router` |
| **Site** | `PrimarySite` |
| **Status** | Active |
| **Management IPv4 Address** | (SSH connection IP) |

**Custom Fields:**
- `loopback_ip`: `10.0.0.1`
- `bgp_role`: `client`
- `wg_peers`: `["router02", "router03"]`

### Example: Router 02

| Setting | Value |
|---------|-------|
| **Name** | `router02` |
| **Device Type** | `Generic Router` |
| **Device Role** | `Router` |
| **Site** | `PrimarySite` |
| **Status** | Active |
| **Management IPv4 Address** | (SSH connection IP) |

**Custom Fields:**
- `loopback_ip`: `10.0.0.2`
- `bgp_role`: `client`
- `wg_peers`: `["router01", "router03"]`

### Example: Router 03 (Route Reflector)

| Setting | Value |
|---------|-------|
| **Name** | `router03` |
| **Device Type** | `Generic Router` |
| **Device Role** | `Router` |
| **Site** | `PrimarySite` |
| **Status** | Active |
| **Management IPv4 Address** | (SSH connection IP) |

**Custom Fields:**
- `loopback_ip`: `10.0.0.3`
- `bgp_role`: `rr`
- `wg_peers`: `["router01", "router02"]`

## Step 7: Create Loopback Interfaces

For each device, create a loopback interface:

**Path:** Devices → [Device Name] → Interfaces

### Interface for router01

| Setting | Value |
|---------|-------|
| **Name** | `lo` |
| **Type** | Virtual |
| **Enabled** | Yes |
| **MTU** | 65535 |

Then assign an IP address to this interface:

**Path:** Devices → [Device Name] → IP Addresses

| Setting | Value |
|---------|-------|
| **Address** | `10.0.0.1/32` |
| **Interface** | `router01 / lo` |
| **Status** | Active |

Repeat for each router with appropriate loopback IPs.

## Step 8: Configure Inventory Plugin

Create or verify `inventory/netbox.yml`:

```yaml
plugin: netbox.netbox.nb_inventory
api_endpoint: "{{ lookup('env', 'NETBOX_API') }}"
token: "{{ lookup('env', 'NETBOX_TOKEN') }}"
validate_certs: true
interfaces: true
cache: false
compose:
  ansible_host: primary_ip
  loopback_ip: netbox_cf_loopback_ip
  ansible_connection: "{{ 'local' if inventory_hostname == (lookup('env', 'BGP_AUTO_LOCAL_MACHINE') | default('', true)) else 'ssh' }}"
  netbox_cf_loopback_ip: netbox_cf_loopback_ip
  netbox_cf_bgp_role: netbox_cf_bgp_role
  netbox_cf_wg_private_key: netbox_cf_wg_private_key
  netbox_cf_wg_public_key: netbox_cf_wg_public_key
  netbox_cf_wg_peers: netbox_cf_wg_peers
  netbox_cf_ospf_enabled: netbox_cf_ospf_enabled
group_by:
  - device_role
```

If you are running the playbook on one of the managed devices, set `BGP_AUTO_LOCAL_MACHINE` to that device's NetBox name before launching Ansible. That host will use `ansible_connection=local` and will not SSH back into itself.

## Step 9: Test Connectivity

Before running the playbook:

1. **Verify API token works:**
   ```bash
   export NETBOX_API="https://netbox.example.com"
   export NETBOX_TOKEN="your-token"
   curl -H "Authorization: Token $NETBOX_TOKEN" \
     "$NETBOX_API/api/dcim/devices/?role=router"
   ```

2. **Verify inventory plugin discovers devices:**
   ```bash
   cd /path/to/bgp_auto
   ansible-inventory -i inventory/netbox.yml --list | jq .
   ```

3. **Test SSH connectivity:**
   ```bash
   ansible all -i inventory/netbox.yml -m ping
   ```

## Step 10: Complete WireGuard Peer Setup

Once routers are deployed, WireGuard keys are auto-generated and stored in NetBox. Verify in NetBox:

**Path:** Devices → [Device Name] → Custom Fields

Check that `wg_private_key` and `wg_public_key` are now populated.

## Field Reference: What Gets Used Where

### BIRD (BGP)

- **Input:** `loopback_ip`, `bgp_role`
- **Output:** None (read-only)
- **Generates:** iBGP peer config using loopback IPs, RR config if `bgp_role=rr`

### WireGuard

- **Input:** `wg_private_key`, `wg_public_key`, `wg_peers`
- **Output:** `wg_private_key`, `wg_public_key` (synced on first run)
- **Generates:** WireGuard config with peer list

### GRE (Optional)

- **Input:** `loopback_ip`
- **Output:** Tunnel IPs (allocated from NetBox prefix)
- **Generates:** GRE tunnel config

## Common Issues & Solutions

### Issue: Devices not discovered

**Problem:** `ansible-inventory` returns empty list

**Solution:**
1. Verify device role is exactly `Router` (case-sensitive slug: `router`)
2. Check device status is `Active`
3. Run: `curl "$NETBOX_API/api/dcim/devices/?role=router"`

### Issue: Custom fields appear as `None`

**Problem:** `netbox_cf_loopback_ip` shows `None` in playbook

**Solution:**
1. Verify custom field exists (Administration → Custom Fields)
2. Verify custom field is assigned to Content Type: DCIM / Device
3. Verify value is entered on the device page
4. Clear inventory cache: `rm -rf /tmp/ansible_netbox_inv_*`

### Issue: WireGuard keys not syncing to NetBox

**Problem:** First playbook run fails or keys not stored

**Solution:**
1. Check NETBOX_TOKEN has write permissions
2. Check `netbox_cf_wg_private_key` and `netbox_cf_wg_public_key` custom fields exist
3. Run with: `ansible-playbook ... -vvv` to see API errors

### Issue: Loopback IP not assigned

**Problem:** BIRD complains about missing loopback IP

**Solution:**
1. Verify interface `lo` exists on device (Devices → Interfaces)
2. Verify IP address `X.X.X.X/32` is assigned to `lo` interface
3. Check IP status is `Active`

## NetBox Backup

Regularly backup your NetBox database to preserve peer configuration:

```bash
# Docker backup example
docker exec netbox python manage.py dumpdata > netbox_backup.json

# PostgreSQL backup
pg_dump netbox > netbox_backup.sql
```

## Next Steps

1. Create devices for all routers
2. Set custom fields for each device
3. Test inventory discovery: `ansible-inventory -i inventory/netbox.yml --list`
4. Run initial deployment: `ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml`
5. Verify BGP peering: `ssh router01 birdc show protocols`

See [Workflow Guide](WORKFLOW_GUIDE.md) for step-by-step instructions.
