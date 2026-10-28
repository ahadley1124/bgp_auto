#!/usr/bin/env python3
"""Tiny fake NetBox for tests: serves tests/fixtures/netbox.json, paginates, accepts PATCH.

  mock_netbox.py [--port N] [--fixture FILE] [--page 2]
Prints the listening port on the first line. Requires token "testtoken".
"""
import argparse, json, os, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=0)
ap.add_argument("--fixture", default=os.path.join(HERE, "fixtures", "netbox.json"))
ap.add_argument("--page", type=int, default=2, help="page size, small on purpose to test pagination")
a = ap.parse_args()
with open(a.fixture) as f:
    DATA = json.load(f)
ROUTES = {"/api/dcim/devices/": "devices",
          "/api/plugins/routing/bgp/peer/": "peers",
          "/api/plugins/routing/bgp/scope/": "scopes",
          "/api/plugins/routing/bgp/router/": "routers",
          "/api/plugins/routing/objects/prefix-list/": "prefix_lists",
          "/api/plugins/routing/objects/prefix-list-entry/": "prefix_list_entries",
          "/api/ipam/prefixes/": "tagged_prefixes"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def authed(self):
        if self.headers.get("Authorization") != "Token testtoken":
            self.send(403, {"detail": "bad token"}); return False
        return True

    def do_GET(self):
        if not self.authed(): return
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        key = ROUTES.get(u.path)
        if not key: return self.send(404, {"detail": "not found"})
        if key == "tagged_prefixes" and q.get("tag") == ["bgp-anycast"]:
            key = "anycast_prefixes"
        rows = DATA.get(key, [])
        if "name" in q: rows = [r for r in rows if r.get("name") in q["name"]]
        off = int(q.get("offset", ["0"])[0]); lim = min(int(q.get("limit", ["50"])[0]), a.page)
        nxt = None
        if off + lim < len(rows):
            qq = {k: v for k, v in q.items() if k != "offset"}; qq["offset"] = [str(off + lim)]
            nxt = "http://127.0.0.1:%d%s?%s" % (self.server.server_port, u.path, urllib.parse.urlencode(qq, doseq=True))
        self.send(200, {"count": len(rows), "next": nxt, "results": rows[off:off + lim]})

    def do_PATCH(self):
        if not self.authed(): return
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n))
        base, _, oid = self.path.rstrip("/").rpartition("/")
        key = ROUTES.get(base + "/")
        for row in DATA.get(key, []):
            if row["id"] == int(oid):
                row.setdefault("custom_fields", {}).update(body.get("custom_fields", {})); return self.send(200, row)
        self.send(404, {})


srv = HTTPServer(("127.0.0.1", a.port), H)
print(srv.server_port, flush=True)
srv.serve_forever()
