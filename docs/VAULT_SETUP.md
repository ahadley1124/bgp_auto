# Vault as Environment Storage

How to keep this project's secrets in an **Ansible Vault** instead of loose
environment variables, shell profiles, or CI secrets pasted into a terminal.

---

## Contents

1. [What needs to be stored](#1-what-needs-to-be-stored)
2. [The constraint that shapes everything](#2-the-constraint-that-shapes-everything)
3. [Create the vault](#3-create-the-vault)
4. [Pattern A — vault to environment (works today)](#4-pattern-a--vault-to-environment-works-today)
5. [Pattern B — vault variables read directly](#5-pattern-b--vault-variables-read-directly)
6. [Everyday vault operations](#6-everyday-vault-operations)
7. [CI / GitHub Actions](#7-ci--github-actions)
8. [Multiple environments](#8-multiple-environments)
9. [Security practices](#9-security-practices)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What needs to be stored

| Secret | Used by | Currently sourced from |
| ------ | ------- | ---------------------- |
| NetBox API token | `netbox.netbox.nb_inventory`, and every `uri` task in the roles | `NETBOX_TOKEN` env var |
| NetBox API URL | Same | `NETBOX_API`, falling back to `NETBOX_URL` |
| Become/sudo password | Privilege escalation on targets | `ANSIBLE_BECOME_PASS` env var |
| SSH key or password | Connecting to routers | SSH agent / key file |

The NetBox token is the one that really matters: it is a read/write credential
for your source of truth.

`group_vars/all.yml` maps the vault variables onto the names the code uses, and
falls back to the environment when no vault is loaded:

```yaml
netbox_token: "{{ vault_netbox_token | default(lookup('env', 'NETBOX_TOKEN'), true) }}"
netbox_url: "{{ vault_netbox_url | default(lookup('env', 'NETBOX_API') | default(lookup('env', 'NETBOX_URL'), true), true) }}"
```

The `vault_` prefix is the conventional indirection: the encrypted file defines
`vault_netbox_token`, and this plaintext file maps it to the name the code uses.
That way you can grep the repo to see *where* a secret is consumed without
decrypting anything. Because of the fallback, the file works with or without a
vault — a vault value wins, the environment fills in otherwise.

Both playbooks load `group_vars/all.yml` with `vars_files`. It is **not** on
Ansible's group_vars search path for this layout (Ansible resolves `group_vars`
next to the inventory and next to the playbook, and this file sits at the repo
root), so any new playbook must load it the same way.

> One caveat remains, and it shapes the rest of this guide: nothing in the roles
> reads the `netbox_token` variable yet — they read the environment. See
> [section 2](#2-the-constraint-that-shapes-everything).

---

## 2. The constraint that shapes everything

Both consumers of the NetBox credentials read the **controller's process
environment**, not Ansible variables:

```yaml
# roles/*/tasks/*.yml
url: "{{ lookup('env','NETBOX_API') | default(lookup('env','NETBOX_URL'), true) }}"
headers:
  Authorization: "Token {{ lookup('env','NETBOX_TOKEN') }}"
```

Two consequences that are easy to get wrong:

1. **`lookup('env', ...)` runs on the controller**, so it reads the environment
   of the `ansible-playbook` process itself.
2. **A play-level `environment:` keyword does not help.** That keyword sets
   variables for *remote task execution* only. A lookup evaluated on the
   controller will not see it:

   ```yaml
   environment:
     NETBOX_TOKEN: "{{ vault_netbox_token }}"   # does NOT reach lookup('env', ...)
   ```

3. **The inventory plugin runs before any play**, so it cannot use play vars at
   all. `inventory/netbox.yml` sets no `token:` or `api_endpoint:`, so
   `nb_inventory` falls back to `NETBOX_TOKEN` and `NETBOX_API`.

Loading `group_vars/all.yml` (which both playbooks now do) makes `netbox_token`
and `netbox_url` available as *variables*, but the roles do not read them yet, so
on its own that changes nothing about which credentials are used.

So a vault can be the *storage*, but the secrets must land in the environment
before `ansible-playbook` starts. That is [Pattern A](#4-pattern-a--vault-to-environment-works-today),
and it needs no code changes. [Pattern B](#5-pattern-b--vault-variables-read-directly)
removes the constraint by changing the roles to prefer variables.

> `ansible.cfg` contains `env_pass_list = NETBOX_TOKEN,NETBOX_URL`. That is not
> a recognized `ansible-core` setting and is silently ignored — confirm with
> `ansible-config dump --only-changed`, which lists only `roles_path` and
> `enable_plugins`. Do not rely on it to propagate anything.

---

## 3. Create the vault

### 3.1 The vault password

The vault password is what protects everything else. Keep it in a file outside
the repository:

```bash
mkdir -p ~/.config/bgp_auto
head -c 32 /dev/urandom | base64 > ~/.config/bgp_auto/vault_pass
chmod 600 ~/.config/bgp_auto/vault_pass
```

Point Ansible at it once, rather than typing `--vault-password-file` every time:

```bash
export ANSIBLE_VAULT_PASSWORD_FILE=~/.config/bgp_auto/vault_pass
```

Or make it permanent in `ansible.cfg`:

```ini
[defaults]
vault_password_file = ~/.config/bgp_auto/vault_pass
```

### 3.2 The encrypted secrets file

```bash
mkdir -p group_vars/all
ansible-vault create group_vars/all/vault.yml
```

Contents:

```yaml
---
vault_netbox_url: "https://netbox.example.com"
vault_netbox_token: "0123456789abcdef0123456789abcdef01234567"
vault_become_pass: "sudo-password-if-needed"
```

The file on disk starts with `$ANSIBLE_VAULT;1.1;AES256` and is safe to commit.
The password file is **not**.

### 3.3 Keep the password out of git

```bash
cat >> .gitignore <<'EOF'

# Ansible Vault — never commit the password itself
.vault_pass
.vault_password
vault_pass.txt
*.vault_pass
EOF
```

---

## 4. Pattern A — vault to environment (works today)

Decrypt the vault and export the values before running Ansible. No changes to
the roles.

Save as `scripts/with-vault-env.sh`:

```bash
#!/usr/bin/env bash
# Export secrets from the Ansible Vault, then exec the given command.
#   ./scripts/with-vault-env.sh ansible-playbook -i inventory/netbox.yml ...
set -euo pipefail

VAULT_FILE="${VAULT_FILE:-group_vars/all/vault.yml}"
VAULT_PASS="${VAULT_PASS:-${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/.config/bgp_auto/vault_pass}}"

eval "$(
  ansible-vault view --vault-password-file "$VAULT_PASS" "$VAULT_FILE" \
  | python3 -c '
import sys, yaml, shlex
data = yaml.safe_load(sys.stdin) or {}
mapping = {
    "vault_netbox_url":   "NETBOX_API",
    "vault_netbox_token": "NETBOX_TOKEN",
    "vault_become_pass":  "ANSIBLE_BECOME_PASS",
}
for key, env in mapping.items():
    if data.get(key):
        print(f"export {env}={shlex.quote(str(data[key]))}")
'
)"

exec "$@"
```

```bash
chmod +x scripts/with-vault-env.sh
```

Use it in front of any command that needs NetBox:

```bash
./scripts/with-vault-env.sh \
  ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
    -e device_name=router01 --become

./scripts/with-vault-env.sh ansible-inventory -i inventory/netbox.yml --list
```

Why `eval` of generated `export` lines, rather than piping into the environment
directly: it keeps the secret out of the process table and out of any argv that
another user could read with `ps`.

The values never touch your shell profile, and they exist only for the lifetime
of that one command.

> If your `group_vars/` is auto-loaded for the playbook you are running, also
> pass `--vault-password-file` (or set `ANSIBLE_VAULT_PASSWORD_FILE`), otherwise
> Ansible fails with *"Attempting to decrypt but no vault secrets found"* when it
> tries to read the encrypted file as ordinary vars.

---

## 5. Pattern B — vault variables read directly

Cleaner in the long run: have the roles read a variable and let Ansible's own
vault handling decrypt it. This requires two changes.

### 5.1 Load the vault file explicitly

`playbooks/deploy.yml` and `playbooks/purge_ips.yml` already load
`group_vars/all.yml`. Add the encrypted file alongside it:

```yaml
- hosts: "{{ device_name | default('all') }}"
  become: true
  vars_files:
    - ../group_vars/all/vault.yml     # encrypted; decrypted automatically
    - ../group_vars/all.yml           # maps vault_* to the names code uses
```

Order matters only for readability here — Ansible resolves the templates lazily,
so `netbox_token` picks up `vault_netbox_token` either way.

Alternatively move both files to `inventory/group_vars/all/`, where they are
picked up automatically for this inventory and no `vars_files` entry is needed.

### 5.2 Make the roles prefer the variable

Change the lookups so a variable wins and the environment remains a fallback:

```yaml
url: "{{ netbox_url | default(lookup('env','NETBOX_API')
                               | default(lookup('env','NETBOX_URL'), true), true) }}"
headers:
  Authorization: "Token {{ netbox_token | default(lookup('env','NETBOX_TOKEN'), true) }}"
```

Apply this in:

- `roles/common/tasks/get_netbox_peers.yml`
- `roles/common/tasks/get_loopback_ip.yml`
- `roles/common/tasks/sync_dummy_interfaces.yml`
- `roles/gre/tasks/main.yml`, `roles/gre/tasks/gre_peer.yml`
- `roles/wireguard/tasks/main.yml`

### 5.3 The inventory plugin still needs the environment

`nb_inventory` runs before any play, so Pattern B does not cover it. Either keep
using the wrapper for inventory, or put the credentials in
`inventory/netbox.yml` with an inline encrypted string:

```bash
ansible-vault encrypt_string 'your-api-token' --name 'token'
```

```yaml
plugin: netbox.netbox.nb_inventory
api_endpoint: https://netbox.example.com
token: !vault |
  $ANSIBLE_VAULT;1.1;AES256
  33646133386537373162323233323934326135633565303164633337343136376335396461353832
  ...
```

Ansible decrypts `!vault` values when it loads the file, so this works as long
as the vault password is available to the run.

---

## 6. Everyday vault operations

| Task | Command |
| ---- | ------- |
| Create | `ansible-vault create group_vars/all/vault.yml` |
| Edit in place | `ansible-vault edit group_vars/all/vault.yml` |
| View without editing | `ansible-vault view group_vars/all/vault.yml` |
| Encrypt an existing plaintext file | `ansible-vault encrypt secrets.yml` |
| Decrypt permanently (careful) | `ansible-vault decrypt group_vars/all/vault.yml` |
| Change the password | `ansible-vault rekey group_vars/all/vault.yml` |
| Encrypt one value for inline use | `ansible-vault encrypt_string 'secret' --name 'vault_netbox_token'` |
| Confirm a file is encrypted | `head -1 group_vars/all/vault.yml` |

`ansible-vault edit` decrypts to a temporary file, opens `$EDITOR`, and
re-encrypts on save. Prefer it over decrypt/edit/encrypt, which leaves plaintext
on disk if interrupted.

### Rotating the NetBox token

1. Issue a new token in NetBox, leaving the old one active.
2. `ansible-vault edit group_vars/all/vault.yml` and replace the value.
3. Run `ansible-inventory -i inventory/netbox.yml --list` through the wrapper to
   confirm the new token works.
4. Revoke the old token in NetBox.
5. Commit the re-encrypted file. The ciphertext changes; the plaintext never
   appears in history.

---

## 7. CI / GitHub Actions

`.github/workflows/ansible-deploy.yml` already carries the vault password as a
repository secret and materializes it as a file:

```yaml
- name: Write vault password file
  run: |
    vault_file="$RUNNER_TEMP/ansible-vault-password"
    printf '%s' '${{ secrets.ANSIBLE_VAULT_PASSWORD }}' > "$vault_file"
    chmod 600 "$vault_file"

- name: Dry run deployment
  run: |
    vault_file="$RUNNER_TEMP/ansible-vault-password"
    ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml \
      --become --check --diff --vault-password-file "$vault_file"
```

Today the workflow also passes `NETBOX_API` and `NETBOX_TOKEN` as separate
GitHub secrets. To make the vault the single source, drop those two secrets and
wrap the commands instead:

```yaml
- name: Dry run deployment
  run: |
    export ANSIBLE_VAULT_PASSWORD_FILE="$RUNNER_TEMP/ansible-vault-password"
    ./scripts/with-vault-env.sh \
      ansible-playbook -i inventory/netbox.yml playbooks/deploy.yml --become --check --diff
```

Then `ANSIBLE_VAULT_PASSWORD` is the only secret CI needs, and rotating the
NetBox token is a commit rather than a settings change.

Notes:

- `$RUNNER_TEMP` is cleaned up automatically; never write the password into the
  workspace, where a later step could commit it.
- GitHub masks secret values in logs, but only exact matches. A token that gets
  transformed (base64, JSON-escaped) is **not** masked — keep `no_log: true` on
  tasks that handle secrets, as `roles/wireguard/tasks/main.yml` already does.

---

## 8. Multiple environments

Use one vault file per environment and select at run time:

```text
inventory/
├── prod/
│   ├── netbox.yml
│   └── group_vars/all/vault.yml
└── staging/
    ├── netbox.yml
    └── group_vars/all/vault.yml
```

```bash
ansible-playbook -i inventory/staging/netbox.yml playbooks/deploy.yml --become
```

Because these live next to the inventory, they are auto-loaded — no `vars_files`
needed.

With distinct passwords per environment, label them with vault IDs so one run
can open several:

```bash
ansible-vault encrypt --encrypt-vault-id prod \
  --vault-id prod@~/.config/bgp_auto/prod_pass group_vars/all/vault.yml

ansible-playbook ... --vault-id prod@~/.config/bgp_auto/prod_pass
```

A staging password then cannot decrypt production secrets.

---

## 9. Security practices

- **Never commit the password file.** The encrypted vault is safe in git; the
  password is not. See [3.3](#33-keep-the-password-out-of-git).
- **Never pass secrets as `-e` on the command line.** They land in your shell
  history and in `ps` output for every user on the box.
- **Scope the NetBox token.** The playbook writes to NetBox (IP allocation, GRE
  pairs, WireGuard keys), so it needs write access — but only to IPAM and DCIM.
  Do not use an admin token.
- **Keep `no_log: true`** on tasks handling key material.
- **Treat a leaked vault file as compromised** if the password was ever weak or
  shared. Rotate the NetBox token first, then `rekey`, then force-push is *not*
  enough — the old ciphertext stays in clones.
- **Back up the vault password** somewhere you control, such as a password
  manager. Losing it means the encrypted file is unrecoverable.
- For centrally-managed secrets, Ansible Vault can be replaced with HashiCorp
  Vault via the `community.hashi_vault` collection. The environment constraint in
  [section 2](#2-the-constraint-that-shapes-everything) applies equally.

---

## 10. Troubleshooting

| Message | Cause | Fix |
| ------- | ----- | --- |
| `Attempting to decrypt but no vault secrets found` | No vault password given, but an encrypted file was loaded | Set `ANSIBLE_VAULT_PASSWORD_FILE` or pass `--vault-password-file` |
| `Decryption failed` | Wrong password, or the file was rekeyed | Confirm which password file; check `--vault-id` labels |
| `input is not vault encrypted data` | Running a vault command on a plaintext file | `head -1` the file to check for the `$ANSIBLE_VAULT` header |
| NetBox returns `403` | Token wrong, expired, or lacks write permission | `curl -H "Authorization: Token $NETBOX_TOKEN" "$NETBOX_API/api/dcim/devices/"` |
| Inventory is empty, playbook says no hosts | `NETBOX_TOKEN`/`NETBOX_API` not in the environment | Run through the wrapper; check with `env | grep NETBOX` |
| Secrets resolve in tasks but the inventory still fails | Inventory runs before plays, so play vars never reach it | See [5.3](#53-the-inventory-plugin-still-needs-the-environment) |
| `vault_netbox_token is undefined` | The vault file was never loaded | Add `vars_files`, or move it next to the inventory |

### Quick verification

```bash
# Is the file actually encrypted?
head -1 group_vars/all/vault.yml     # => $ANSIBLE_VAULT;1.1;AES256

# Can you open it?
ansible-vault view group_vars/all/vault.yml

# Does the wrapper populate the environment?
./scripts/with-vault-env.sh sh -c 'echo "${NETBOX_TOKEN:0:6}..."'

# Does the token work against NetBox?
./scripts/with-vault-env.sh sh -c \
  'curl -sS -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Token $NETBOX_TOKEN" "$NETBOX_API/api/dcim/devices/?limit=1"'
```

A `200` from the last command means the vault, the wrapper, and the token are
all correct.
