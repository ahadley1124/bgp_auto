#!/usr/bin/env python3
"""Talk to NetBox (and the netbox-routing plugin) for bgp_auto. Standard library only.

  netbox_fetch.py fetch   > raw.json         everything build_model needs
  netbox_fetch.py probe                      check each endpoint and show custom field names
  netbox_fetch.py set-cf DEVICE FIELD VALUE  write one device custom field (used for mesh_ip)

Environment: NETBOX_URL, NETBOX_TOKEN. Optional NETBOX_VERIFY=0 to skip TLS verification
(never do this over the internet), NETBOX_ENDPOINTS=<json> to override endpoint paths.

Sessions come from netbox-routing (`BGPPeer`), which replaced the netbox-bgp plugin.
This script only fetches; filter_plugins/bgp_auto.py reshapes those objects into the
model (see _adapt_netbox_routing there). Note that
`/api/plugins/bgp/` still resolves on a NetBox with netbox-peering-manager installed,
but that is a different plugin with a different layout: its presence does not mean
netbox-bgp is there. Run `probe` after upgrading NetBox or the plugins.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = {
    "devices": "/api/dcim/devices/?role=transit-router&role=ix-router&role=monitoring",
    # netbox-routing. A BGPPeer points at a BGPScope, which points at a BGPRouter,
    # which is the thing actually bound to a device -- so all three are needed to
    # work out which router a session belongs to.
    "peers": "/api/plugins/routing/bgp/peer/",
    "scopes": "/api/plugins/routing/bgp/scope/",
    "routers": "/api/plugins/routing/bgp/router/",
    "prefix_lists": "/api/plugins/routing/objects/prefix-list/",
    "prefix_list_entries": "/api/plugins/routing/objects/prefix-list-entry/",
    "tagged_prefixes": "/api/ipam/prefixes/?tag=bgp-originate",
    "anycast_prefixes": "/api/ipam/prefixes/?tag=bgp-anycast",
}
REQUIRED = ("devices", "peers")


class NetBox:
    def __init__(self, url, token, verify=True):
        self.url = url.rstrip("/")
        self.token = token
        self.ctx = None if verify else ssl._create_unverified_context()

    def call(self, path, method="GET", body=None):
        url = path if path.startswith("http") else self.url + path
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", "Token " + self.token)
        req.add_header("Accept", "application/json")
        # urllib's default User-Agent (Python-urllib/3.x) is rejected outright by the
        # Cloudflare in front of some NetBox installs (error 1010), so send our own.
        req.add_header("User-Agent", os.environ.get("NETBOX_USER_AGENT", "bgp_auto/1.0"))
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise SystemExit("NetBox %s %s -> HTTP %s" % (method, path, e.code))
        except urllib.error.URLError as e:
            raise SystemExit("NetBox %s unreachable: %s" % (self.url, e.reason))

    def all(self, path):
        sep = "&" if "?" in path else "?"
        nxt, out = path + sep + "limit=500", []
        while nxt:
            page = self.call(nxt)
            if isinstance(page, dict) and "results" in page:
                out += page["results"]
                nxt = page.get("next")
            else:
                out += page or []
                nxt = None
        return out


def client():
    url, token = os.environ.get("NETBOX_URL") or os.environ.get("NETBOX_API"), os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        raise SystemExit("NETBOX_URL and NETBOX_TOKEN must be set")
    return NetBox(url, token, os.environ.get("NETBOX_VERIFY", "1") != "0")


def endpoints():
    ep = dict(ENDPOINTS)
    if os.environ.get("NETBOX_ENDPOINTS"):
        ep.update(json.loads(os.environ["NETBOX_ENDPOINTS"]))
    return ep


def cmd_fetch(nb):
    raw = {}
    for key, path in endpoints().items():
        if not path:
            raw[key] = []
            continue
        raw[key] = nb.all(path)
    json.dump(raw, sys.stdout)
    sys.stdout.write("\n")


def cmd_probe(nb):
    bad = 0
    for key, path in endpoints().items():
        try:
            rows = nb.all(path)
            print("ok    %-18s %4d rows  %s" % (key, len(rows), path))
            if key == "peers" and rows:
                print("        peer fields   :", ", ".join(sorted(rows[0].keys())))
                print("        custom fields :", ", ".join(sorted((rows[0].get("custom_fields") or {}).keys())))
        except SystemExit as e:
            print("FAIL  %-18s %s" % (key, e))
            bad += key in REQUIRED or 1
    sys.exit(1 if bad else 0)


def cmd_set_cf(nb, device, field, value):
    rows = nb.all("/api/dcim/devices/?name=" + urllib.parse.quote(device))
    if len(rows) != 1:
        raise SystemExit("device %s: found %d matches" % (device, len(rows)))
    cur = (rows[0].get("custom_fields") or {}).get(field)
    if cur == value:
        print("unchanged")
        return
    nb.call("/api/dcim/devices/%d/" % rows[0]["id"], "PATCH", {"custom_fields": {field: value}})
    print("changed")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("fetch", "probe", "set-cf"):
        raise SystemExit(__doc__)
    nb = client()
    if argv[1] == "fetch":
        cmd_fetch(nb)
    elif argv[1] == "probe":
        cmd_probe(nb)
    else:
        if len(argv) != 5:
            raise SystemExit("usage: set-cf DEVICE FIELD VALUE")
        cmd_set_cf(nb, *argv[2:])


if __name__ == "__main__":
    main(sys.argv)
