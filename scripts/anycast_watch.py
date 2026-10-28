#!/usr/bin/env python3
"""Watch how tier-1 networks reach our anycast prefixes and suggest (never apply) community changes.

  anycast_watch.py --config anycast.json --out-dir DIR [--input looking-glass.json]

The config comes from the model (`bgp_anycast_watch` filter):
  {"own_asn": 17290, "prefixes": ["2602:fa86:ff8::/46"],
   "entries": {"20473": {"name": "AS20473", "sessions": [{"device","label","family","prepend"}]}, ...},
   "tier1_asns": [...], "target_share": {"20473": 0.7}, "tolerance": 0.15, "max_prepend": 3}

Data: RIPEstat looking-glass (RIPE RIS collectors). For each prefix it takes the collector peers that
are tier-1 networks, reads their AS path and finds the "entry" AS, the network right before ours.
Writes to DIR: anycast.prom (Prometheus textfile metrics) and anycast-suggestions.json. Changes are
made by a person editing the NetBox session fields; nothing here touches a router.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

LG_URL = "https://stat.ripe.net/data/looking-glass/data.json?resource="
TIER1 = [174, 209, 701, 1239, 1299, 2914, 3257, 3320, 3356, 3491, 5511, 6453, 6461, 6762, 6830, 7018, 12956]


def fetch(prefix, timeout=60):
    with urllib.request.urlopen(LG_URL + urllib.parse.quote(prefix, safe=""), timeout=timeout) as r:
        return json.loads(r.read())


def _path(peer):
    p = peer.get("as_path")
    if isinstance(p, str):
        p = p.replace("{", " ").replace("}", " ").replace(",", " ").split()
    out = []
    for x in p or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def views(lg, prefix, own_asn, tier1):
    """[(tier1_asn, path, entry_asn_or_None)] for the tier-1 collector peers seeing this exact prefix."""
    out = []
    for rrc in (lg.get("data") or {}).get("rrcs") or []:
        for peer in rrc.get("peers") or []:
            if peer.get("prefix") not in (None, prefix):
                continue
            path = _path(peer)
            if not path or path[0] not in tier1:
                continue
            # drop our own prepends at the end, the entry is the AS right before them
            if path[-1] != own_asn:
                continue                      # not originated by us, ignore
            core = list(path)
            while core and core[-1] == own_asn:
                core.pop()
            entry = core[-1] if core else None
            out.append((path[0], path, entry))
    return out


def analyze(lg_by_prefix, cfg):
    own = cfg["own_asn"]
    tier1 = set(cfg.get("tier1_asns") or TIER1)
    tol = cfg.get("tolerance", 0.15)
    maxp = cfg.get("max_prepend", 3)
    target = {str(k): v for k, v in (cfg.get("target_share") or {}).items()}
    result = {"prefixes": {}, "suggestions": [], "alerts": []}
    for prefix in cfg["prefixes"]:
        vs = views(lg_by_prefix.get(prefix) or {}, prefix, own, tier1)
        # one view per tier-1: keep its shortest path
        best = {}
        for t1, path, entry in vs:
            if t1 not in best or len(path) < len(best[t1][0]):
                best[t1] = (path, entry)
        shares = {}
        for path, entry in best.values():
            shares[str(entry)] = shares.get(str(entry), 0) + 1
        total = len(best)
        info = {"tier1_seen": total,
                "per_tier1": {str(t): {"length": len(p), "entry": e, "path": p} for t, (p, e) in sorted(best.items())},
                "share": {k: v / total for k, v in shares.items()} if total else {}}
        result["prefixes"][prefix] = info
        if total == 0:
            result["alerts"].append("%s: no tier-1 collector peer sees this prefix" % prefix)
            continue
        for entry, want in target.items():
            have = info["share"].get(entry, 0.0)
            fam = 6 if ":" in prefix else 4
            sessions = [s for s in (cfg.get("entries", {}).get(entry) or {}).get("sessions", []) if s.get("family", fam) == fam]
            others = [(k, e) for k, e in (cfg.get("entries") or {}).items() if k != entry]
            if have > want + tol:
                # this entry attracts too much: prepend more on it
                for s in sessions:
                    new = min(s.get("prepend", 0) + 1, maxp)
                    if new != s.get("prepend", 0):
                        result["suggestions"].append({
                            "prefix": prefix, "action": "raise_prepend", "entry_asn": entry,
                            "device": s["device"], "session": s["label"], "from": s.get("prepend", 0), "to": new,
                            "reason": "%d%% of tier-1 views enter via AS%s, target %d%%" % (round(have * 100), entry, round(want * 100)),
                            "netbox": "set custom field anycast_prepend=%d on session %s" % (new, s["label"])})
            elif have < want - tol:
                for s in sessions:
                    if s.get("prepend", 0) > 0:
                        new = s["prepend"] - 1
                        result["suggestions"].append({
                            "prefix": prefix, "action": "lower_prepend", "entry_asn": entry,
                            "device": s["device"], "session": s["label"], "from": s["prepend"], "to": new,
                            "reason": "only %d%% of tier-1 views enter via AS%s, target %d%%" % (round(have * 100), entry, round(want * 100)),
                            "netbox": "set custom field anycast_prepend=%d on session %s" % (new, s["label"])})
                    elif not others:
                        pass
                if not any(s.get("prepend", 0) > 0 for s in sessions):
                    result["alerts"].append("%s: AS%s carries %d%% of tier-1 views, target %d%%, and has no prepend left to remove; "
                                            "raise prepend on the other entry instead" % (prefix, entry, round(have * 100), round(want * 100)))
    return result


def prom(result, cfg):
    lines = ["# HELP bgp_auto_anycast_tier1_seen Tier-1 collector peers that see the anycast prefix",
             "# TYPE bgp_auto_anycast_tier1_seen gauge"]
    for pfx, info in result["prefixes"].items():
        lines.append('bgp_auto_anycast_tier1_seen{prefix="%s"} %d' % (pfx, info["tier1_seen"]))
    lines += ["# HELP bgp_auto_anycast_path_length AS path length at a tier-1 network", "# TYPE bgp_auto_anycast_path_length gauge"]
    for pfx, info in result["prefixes"].items():
        for t1, v in info["per_tier1"].items():
            lines.append('bgp_auto_anycast_path_length{prefix="%s",tier1="%s",entry="%s"} %d' % (pfx, t1, v["entry"], v["length"]))
    lines += ["# HELP bgp_auto_anycast_entry_share Share of tier-1 views entering via this AS", "# TYPE bgp_auto_anycast_entry_share gauge"]
    for pfx, info in result["prefixes"].items():
        for entry, sh in info["share"].items():
            lines.append('bgp_auto_anycast_entry_share{prefix="%s",entry="%s"} %.4f' % (pfx, entry, sh))
    lines += ["# HELP bgp_auto_anycast_suggestion A change is suggested; see anycast-suggestions.json", "# TYPE bgp_auto_anycast_suggestion gauge"]
    for s in result["suggestions"]:
        text = s["netbox"].replace('"', "'")
        lines.append('bgp_auto_anycast_suggestion{prefix="%s",session="%s",change="%s",reason="%s"} 1' % (s["prefix"], s["session"], text, s["reason"].replace('"', "'")))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--input", help="saved looking-glass JSON {prefix: response} instead of querying RIPEstat")
    a = ap.parse_args(argv)
    cfg = json.load(open(a.config))
    if a.input:
        data = json.load(open(a.input))
    else:
        data = {}
        for p in cfg["prefixes"]:
            try:
                data[p] = fetch(p)
            except Exception as exc:                      # keep the last good metrics on a network blip
                print("looking glass query failed for %s: %s" % (p, exc), file=sys.stderr)
                return 1
    res = analyze(data, cfg)
    os.makedirs(a.out_dir, exist_ok=True)
    tmp = os.path.join(a.out_dir, ".anycast.prom.tmp")
    open(tmp, "w").write(prom(res, cfg))
    os.replace(tmp, os.path.join(a.out_dir, "anycast.prom"))
    json.dump({"suggestions": res["suggestions"], "alerts": res["alerts"], "prefixes": res["prefixes"]},
              open(os.path.join(a.out_dir, "anycast-suggestions.json"), "w"), indent=1)
    for s in res["suggestions"]:
        print("SUGGESTION: %s (%s)" % (s["netbox"], s["reason"]))
    for al in res["alerts"]:
        print("ALERT: " + al)
    return 0


if __name__ == "__main__":
    sys.exit(main())
