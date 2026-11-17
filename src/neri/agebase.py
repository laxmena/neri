"""B: age baseline. Counts ALL public, non-fork, non-archived repos pushed since the census cutoff, by creation-date band and star group, via the
GitHub repository-search `total_count` (core search quota: 30 req/min; no repo content is read). The repos with an agent file are a tiny part of
these totals, so the totals are a baseline for 'active repos', not a no-agent-file sample (search cannot exclude them). Output reports/age_baseline.json."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
import urllib.parse
import urllib.request

from . import config

ROOT = config.ROOT


def _q(tok: str, query: str) -> int:
    for attempt in range(6):
        req = urllib.request.Request("https://api.github.com/search/repositories?per_page=1&q=" + urllib.parse.quote(query),
                                     headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=90))
            return int(r["total_count"])
        except Exception as e:
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(query)


def run(log=print) -> dict:
    cfg = config.load()
    snap = dt.date.fromisoformat(cfg["snapshot"]["date"])
    pushed = cfg["snapshot"]["pushed_cutoff"] if "pushed_cutoff" in cfg["snapshot"] else (snap - dt.timedelta(days=180)).isoformat()
    tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    edges = [snap - dt.timedelta(days=d) for d in (182, 365, 730)]
    bands = {"lt6m": f"created:>{edges[0]}", "6_12m": f"created:{edges[1]}..{edges[0]}", "1_2y": f"created:{edges[2]}..{edges[1]}", "2y_plus": f"created:<{edges[2]}"}
    base = f"pushed:>={pushed} fork:false archived:false"
    out = {"snapshot": str(snap), "pushed_since": str(pushed), "query_base": base, "note": "edge days may fall in two bands (inclusive ranges); counts are search total_count", "counts": {}}
    for grp, st in (("small", "stars:0..9"), ("popular", "stars:>=10"), ("all", "")):
        out["counts"][grp] = {}
        for b, c in bands.items():
            out["counts"][grp][b] = _q(tok, f"{base} {st} {c}".replace("  ", " "))
            time.sleep(3)
            log(f"{grp} {b}: {out['counts'][grp][b]:,}")
    for grp, d in out["counts"].items():
        tot = sum(d.values())
        d["total"] = tot
        d["share_lt6m"] = d["lt6m"] / tot if tot else None
    (ROOT / "reports/age_baseline.json").write_text(json.dumps(out, indent=1))
    return out
