#!/bin/sh
# Offline test suite: no NetBox, no hosts. Needs python3 + jinja2; the BIRD and Ansible tests
# skip themselves when bird / ansible-playbook are not installed.
set -e
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -p 'test_*.py'
if command -v bird >/dev/null 2>&1; then
    python3 tests/bird_filter_test.py | tail -1
else
    echo "bird not installed: skipped the filter behaviour test"
fi
