#!/usr/bin/env bash
# Export secrets from the Ansible Vault into the environment, then exec a command.
#
# The roles read NetBox credentials with lookup('env', ...), which is evaluated
# on the control node, and the nb_inventory plugin runs before any play. Neither
# can see Ansible variables, so the secrets have to be in the environment of the
# ansible-playbook process itself. See docs/VAULT_SETUP.md.
#
# Usage:
#   ./scripts/with-vault-env.sh ansible-playbook -i inventory/netbox.yml \
#       playbooks/deploy.yml --become
#   ./scripts/with-vault-env.sh ansible-inventory -i inventory/netbox.yml --list
#
# Override the defaults with VAULT_FILE and VAULT_PASS.
set -euo pipefail

VAULT_FILE="${VAULT_FILE:-group_vars/all/vault.yml}"
VAULT_PASS="${VAULT_PASS:-${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/.config/bgp_auto/vault_pass}}"

if [ ! -f "$VAULT_FILE" ]; then
    echo "with-vault-env.sh: vault file not found: $VAULT_FILE" >&2
    echo "Create it with: ansible-vault create $VAULT_FILE" >&2
    exit 1
fi

if [ ! -f "$VAULT_PASS" ]; then
    echo "with-vault-env.sh: vault password file not found: $VAULT_PASS" >&2
    echo "Set VAULT_PASS or ANSIBLE_VAULT_PASSWORD_FILE to point at it." >&2
    exit 1
fi

# Generate `export` lines and eval them, so no secret is ever passed as an
# argument where another user could read it out of the process table.
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
    value = data.get(key)
    if value:
        print(f"export {env}={shlex.quote(str(value))}")
'
)"

exec "$@"
