# Documentation Index

Welcome to `bgp_auto` documentation. This guide will help you understand the project and get started.

## Quick Navigation

### 🚀 **First Time?**
Start here → [Workflow Guide](WORKFLOW_GUIDE.md) — Step-by-step instructions to add your first router.

### 📋 **Setting Up NetBox?**
Read → [NetBox Setup Guide](NETBOX_SETUP.md) — Exact custom fields, devices, and IPs you need to create.

### 🏗️ **How does it work?**
Read → [Architecture Guide](ARCHITECTURE.md) — Data flows, configuration discovery, and topology logic.

### 🔧 **Understanding the roles?**
Read → [Role Documentation](ROLE_DOCUMENTATION.md) — Detailed breakdown of BIRD, WireGuard, and GRE roles.

### 🐛 **Something broke?**
Jump to → [Troubleshooting](#troubleshooting) section or [Workflow Guide Troubleshooting](WORKFLOW_GUIDE.md#troubleshooting-guide).

---

## Document Overview

### 1. [Architecture Guide](ARCHITECTURE.md)
**What:** System architecture, data flow, configuration discovery
**For:** Understanding how the project works
**Length:** ~15 min read

**Covers:**
- System architecture diagram
- Data flow (NetBox → Ansible → Routers)
- Topology discovery (full mesh vs route reflector)
- Configuration files and variables
- Common deployment scenarios

---

### 2. [NetBox Setup Guide](NETBOX_SETUP.md)
**What:** Exact NetBox configuration required for each peer
**For:** Setting up NetBox before running playbooks
**Length:** ~20 min + hands-on setup

**Covers:**
- Custom fields to create (6 fields with exact settings)
- Device creation and configuration
- Loopback interface setup
- Inventory plugin configuration
- Testing and verification
- Troubleshooting NetBox setup issues

**Action items:**
- [ ] Create custom fields in NetBox
- [ ] Create devices for routers
- [ ] Add loopback interfaces and IPs
- [ ] Configure inventory plugin

---

### 3. [Role Documentation](ROLE_DOCUMENTATION.md)
**What:** Deep dive into each Ansible role
**For:** Understanding how configuration is generated and deployed
**Length:** ~20 min read

**Covers:**
- **WireGuard role:** Key generation, peer config, NetBox sync
- **BIRD role:** iBGP peer discovery, route reflector logic, OSPF/BFD
- **GRE role:** Tunnel allocation, endpoint configuration
- Shared concepts and troubleshooting

---

### 4. [Workflow Guide](WORKFLOW_GUIDE.md)
**What:** Step-by-step procedures for common tasks
**For:** Hands-on deployment and management
**Length:** ~30 min + hands-on execution

**Covers:**
- Adding a new router (15 steps with checkpoints)
- Scaling with route reflectors (10+ routers)
- Managing peers and topology changes
- Comprehensive troubleshooting guide
- Success checklist

**Common tasks:**
1. Add a new router (Workflow 1)
2. Scale to 10+ routers with RR topology (Workflow 2)
3. Add WireGuard peer (Workflow 3)
4. Promote router to route reflector (Workflow 4)

---

## Quick Start Path

### Path 1: Deploy First Router (30 minutes)

1. **Setup NetBox** (10 min)
   - Read: [NetBox Setup Guide](NETBOX_SETUP.md) steps 1–7
   - Create custom fields, device type, organization/site

2. **Create Router in NetBox** (10 min)
   - Follow [NetBox Setup Guide](NETBOX_SETUP.md) step 6–8
   - Create device, add custom fields, add loopback IP

3. **Deploy with Ansible** (10 min)
   - Follow [Workflow Guide](WORKFLOW_GUIDE.md) steps 9–17
   - Run playbook, verify BGP and WireGuard

### Path 2: Understand Architecture (20 minutes)

1. **High-level overview** (5 min)
   - Skim [README](../README.md)

2. **Architecture & data flow** (10 min)
   - Read [Architecture Guide](ARCHITECTURE.md) sections 1–2

3. **How roles work** (5 min)
   - Skim [Role Documentation](ROLE_DOCUMENTATION.md)

---

## Key Concepts

### NetBox as Source of Truth

All configuration lives in NetBox custom fields:

```
NetBox Device Custom Fields:
├── loopback_ip: "10.0.0.1"
├── bgp_role: "client"
├── wg_peers: ["router02", "router03"]
├── wg_private_key: "..." (auto-generated)
└── wg_public_key: "..." (auto-generated)
         ↓
   Ansible Inventory Plugin
         ↓
   Playbook generates configs
         ↓
   Deployed to routers
```

### Three Roles (Playbooks)

| Role | Purpose | Generates |
|------|---------|-----------|
| **wireguard** | VPN tunnels | `/etc/wireguard/wg0.conf` |
| **bird** | BGP routing daemon | `/etc/bird/generated/*.conf` (iBGP, OSPF, BFD) |
| **gre** | GRE tunnels (optional) | GRE interface configs |

### Two BGP Topologies

**Full Mesh:**
- All routers peer with each other
- Simpler setup, fewer moving parts
- Works for ~5 routers
- High convergence time with many peers

**Route Reflector:**
- 2 RRs peer with each other
- Clients peer only with RRs
- Scales to 50+ routers
- Faster convergence, reduced BGP churn

---

## Common Questions

### Q: Where do I start?
**A:** Follow [Workflow Guide](WORKFLOW_GUIDE.md) for step-by-step instructions.

### Q: What do I need to create in NetBox?
**A:** See [NetBox Setup Guide](NETBOX_SETUP.md) — custom fields, devices, interfaces, IPs.

### Q: Why isn't my router being discovered?
**A:** Check [Troubleshooting: Router not discovered](WORKFLOW_GUIDE.md#issue-1-router-not-discovered-by-ansible) in Workflow Guide.

### Q: How do I scale to 10+ routers?
**A:** Use route reflector topology — see [Workflow 2: Scale with Route Reflectors](WORKFLOW_GUIDE.md#workflow-2-scale-to-10-routers-with-route-reflectors).

### Q: What if BGP peers won't connect?
**A:** Check [Troubleshooting: BGP peers not connecting](WORKFLOW_GUIDE.md#issue-4-bgp-peers-not-connecting) in Workflow Guide.

### Q: Can I use this with my own BGP setup?
**A:** Yes! Modify templates in `roles/bird/templates/` and `roles/wireguard/templates/` to fit your network.

---

## File Structure

```
docs/
├── INDEX.md (this file)               ← Start here
├── ARCHITECTURE.md                    ← How it works
├── NETBOX_SETUP.md                    ← Setup guide
├── ROLE_DOCUMENTATION.md              ← Deep dive
└── WORKFLOW_GUIDE.md                  ← Step-by-step tasks

../
├── README.md                          ← Project overview
├── ansible.cfg                        ← Ansible config
├── group_vars/all.yml                 ← Global variables
├── inventory/netbox.yml               ← NetBox inventory plugin
├── playbooks/deploy.yml               ← Main playbook
└── roles/
    ├── bird/                          ← BGP/OSPF/BFD role
    ├── wireguard/                     ← VPN role
    └── gre/                           ← GRE tunnel role
```

---

## Troubleshooting

### Quick Checks

**1. Is NetBox accessible?**
```bash
curl -H "Authorization: Token $NETBOX_TOKEN" \
  "$NETBOX_API/api/dcim/devices/" | jq .
```

**2. Is Ansible discovering routers?**
```bash
ansible-inventory -i inventory/netbox.yml --list | jq '.all.hosts'
```

**3. Can Ansible SSH to routers?**
```bash
ansible -i inventory/netbox.yml all -m ping
```

**4. Is playbook syntax valid?**
```bash
ansible-playbook playbooks/deploy.yml --syntax-check
```

### Common Issues

| Issue | Link |
|-------|------|
| Router not discovered | [Workflow Guide](WORKFLOW_GUIDE.md#issue-1-router-not-discovered-by-ansible) |
| SSH connection fails | [Workflow Guide](WORKFLOW_GUIDE.md#issue-2-ssh-connection-fails) |
| WireGuard keys not syncing | [Workflow Guide](WORKFLOW_GUIDE.md#issue-3-wireguard-keys-not-syncing-to-netbox) |
| BGP peers not connecting | [Workflow Guide](WORKFLOW_GUIDE.md#issue-4-bgp-peers-not-connecting) |
| Playbook deploy fails | [Workflow Guide](WORKFLOW_GUIDE.md#issue-5-playbook-deploy-fails-midway) |
| Configuration not applied | [Workflow Guide](WORKFLOW_GUIDE.md#issue-6-configuration-changes-not-applied) |

For more detailed troubleshooting, see [Troubleshooting Guide](WORKFLOW_GUIDE.md#troubleshooting-guide) in the Workflow Guide.

---

## Getting Help

1. **Check the docs** — This documentation covers 95% of questions
2. **Search issue tracker** — Look for similar problems in GitHub issues
3. **Run with verbose** — `ansible-playbook ... -vvv` for debug output
4. **Test manually** — SSH to router, verify configs directly
5. **NetBox API** — Use curl to test API calls independently

---

## Next Steps

- ✅ Read [README](../README.md) for project overview
- ✅ Follow [Workflow Guide](WORKFLOW_GUIDE.md) to add first router
- ✅ Read [Architecture Guide](ARCHITECTURE.md) for deeper understanding
- ✅ Set up monitoring and backups for production use

---

**Last updated:** 2026-05-18
**Project:** bgp_auto
**Documentation version:** 1.0
