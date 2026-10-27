#!/usr/bin/env python3
"""Render the BIRD templates for every node in the fixture without Ansible.

Usage: render_bird.py OUTDIR [--irr-json FILE]
Used for fast iteration and by tests/run_tests.sh before the Ansible render test.
"""
import json, os, sys, argparse
import jinja2
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "filter_plugins"))
import bgp_auto as b

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--irr-json")
ap.add_argument("--fixture", default=os.path.join(HERE, "fixtures", "netbox.json"))
a = ap.parse_args()

raw = json.load(open(a.fixture))
model = b.build_model(raw, {})
assert not model["errors"], model["errors"]
irr_data = json.load(open(a.irr_json)) if a.irr_json else {}
tdir = os.path.join(ROOT, "roles", "bgp_router", "templates")
env = jinja2.Environment(loader=jinja2.FileSystemLoader(tdir), keep_trailing_newline=True,
                         trim_blocks=True, lstrip_blocks=False, undefined=jinja2.StrictUndefined)
for k, f in {"bgp_prefix_set": b.bird_prefix_set, "bgp_bird_name": b.bird_name}.items():
    env.filters[k] = f
tier1 = [174, 209, 701, 1239, 1299, 2914, 3257, 3320, 3356, 3491, 5511, 6453, 6461, 6762, 6830, 7018, 12956]
for name, node in model["nodes"].items():
    if not node["is_router"]:
        continue
    out = os.path.join(a.out, name)
    os.makedirs(os.path.join(out, "generated"), exist_ok=True)
    irr_text, irr_empty = b.irr_defines(irr_data, node["sessions"])
    uses_bfd = node["policy"]["ibgp_bfd"] or any(s["bfd"] for s in node["sessions"])
    ctx = dict(node=node, bird_conf_root=out, ansible_managed="Ansible managed",
               bird_tier1_asns=tier1, gre_ifname_prefix="gre-", irr_text=irr_text,
               irr_empty=irr_empty, bird_uses_bfd=uses_bfd)
    files = {"bird.conf": "bird.conf.j2"}
    for fn in ["00-defs", "10-rpki", "20-filters", "25-prefix-sets", "30-base", "40-ospf", "50-ibgp", "60-originate", "70-ebgp", "80-bfd"]:
        files["generated/%s.conf" % fn] = fn + ".conf.j2"
    for dest, src in files.items():
        open(os.path.join(out, dest), "w").write(env.get_template(src).render(**ctx))
print("rendered", ", ".join(n for n, x in model["nodes"].items() if x["is_router"]))
