#!/usr/bin/env python3
"""Create the device roles, tag and custom fields bgp_auto expects in NetBox. Safe to re-run.

  NETBOX_URL=... NETBOX_TOKEN=... scripts/netbox_bootstrap.py [--dry-run]

Reads netbox/schema.json. NetBox 4.x calls the custom field key `object_types`, 3.x `content_types`;
both are tried. The netbox_bgp object type name (netbox_bgp.bgpsession) has not been checked against
every plugin release: if NetBox rejects it, run `netbox_fetch.py probe` and edit schema.json.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netbox_fetch as nbf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure(nb, path, key, body, dry):
    found = nb.all("%s?%s=%s" % (path, key, body[key]))
    if any(r.get(key) == body[key] for r in found):
        print("exists   %s %s" % (path.split("/")[-2], body[key]))
        return
    print("%s %s %s" % ("would create" if dry else "creating", path.split("/")[-2], body[key]))
    if not dry:
        nb.call(path, "POST", body)


def main(argv):
    dry = "--dry-run" in argv
    nb = nbf.client()
    schema = json.load(open(os.path.join(ROOT, "netbox", "schema.json")))
    for r in schema["device_roles"]:
        ensure(nb, "/api/dcim/device-roles/", "slug", r, dry)
    for t in schema["tags"]:
        ensure(nb, "/api/extras/tags/", "slug", t, dry)
    for cf in schema["custom_fields"]:
        body = {"name": cf["name"], "label": cf.get("label", cf["name"]), "type": cf["type"],
                "description": cf.get("description", ""), "required": False}
        if cf["type"] == "select":
            ch = nb.call("/api/extras/custom-field-choice-sets/?name=bgp_auto_%s" % cf["name"])
            if not ch["results"]:
                if not dry:
                    cs = nb.call("/api/extras/custom-field-choice-sets/", "POST",
                                 {"name": "bgp_auto_%s" % cf["name"], "extra_choices": [[c, c] for c in cf["choices"]]})
                    body["choice_set"] = cs["id"]
            else:
                body["choice_set"] = ch["results"][0]["id"]
        for key in ("object_types", "content_types"):
            body.pop("object_types", None); body.pop("content_types", None)
            body[key] = [cf["object"]]
            try:
                ensure(nb, "/api/extras/custom-fields/", "name", body, dry)
                break
            except SystemExit as e:
                if key == "content_types":
                    raise
                print("  retrying with content_types (%s)" % e)


if __name__ == "__main__":
    main(sys.argv)
