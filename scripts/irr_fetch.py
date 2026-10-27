#!/usr/bin/env python3
"""Fetch IRR-derived prefix lists with bgpq4 and cache them.

  irr_fetch.py --out irr.json [--cache DIR] [--ttl HOURS] [--sources RIPE,RADB]
               [--bgpq4 PATH] AS-SET [AS-SET ...]

Output: {"AS-SET": {"4": [{"prefix","exact","greater-equal","less-equal"}], "6": [...]}}

If bgpq4 fails (IRR unreachable) the last cached copy is used and a warning is
printed, so a broken IRR mirror never wipes working filters. With no cache the
script exits 2.
"""
import argparse
import json
import os
import subprocess
import sys
import time


def fetch(bgpq4, name, family, sources):
    cmd = [bgpq4, "-j", "-A", "-l", "X", "-%d" % family, "-m", "24" if family == 4 else "48"]
    if sources:
        cmd += ["-S", sources]
    cmd.append(name)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError("bgpq4 %s: %s" % (name, p.stderr.strip() or p.returncode))
    data = json.loads(p.stdout or "{}")
    entries = next(iter(data.values()), []) if data else []
    out = []
    for e in entries:
        out.append({"prefix": e["prefix"], "exact": bool(e.get("exact", True)),
                    "greater-equal": e.get("greater-equal"), "less-equal": e.get("less-equal")})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=".cache/irr")
    ap.add_argument("--ttl", type=float, default=12.0, help="hours a cached copy is considered fresh")
    ap.add_argument("--sources", default="RIPE,RADB,ARIN,APNIC,AFRINIC,LACNIC")
    ap.add_argument("--bgpq4", default="bgpq4")
    ap.add_argument("sets", nargs="*")
    a = ap.parse_args(argv)
    os.makedirs(a.cache, exist_ok=True)
    result = {}
    rc = 0
    for name in sorted(set(a.sets)):
        cpath = os.path.join(a.cache, name.replace("/", "_") + ".json")
        fresh = os.path.exists(cpath) and (time.time() - os.path.getmtime(cpath)) < a.ttl * 3600
        if fresh:
            result[name] = json.load(open(cpath))
            continue
        try:
            data = {str(f): fetch(a.bgpq4, name, f, a.sources) for f in (4, 6)}
            json.dump(data, open(cpath, "w"))
            result[name] = data
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if os.path.exists(cpath):
                print("WARNING: IRR lookup for %s failed (%s); using cached copy" % (name, exc), file=sys.stderr)
                result[name] = json.load(open(cpath))
            else:
                print("ERROR: IRR lookup for %s failed and nothing is cached: %s" % (name, exc), file=sys.stderr)
                rc = 2
    json.dump(result, open(a.out, "w"), indent=1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
