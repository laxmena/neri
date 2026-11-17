"""reports/cmd_check.md: 20 random non-dev extracted commands, each with its source file, line and +-2 lines of context, so a human can
verify that the normalized command is what the line really tells the agent to run (POST1-PLAN section 5). LOCAL ONLY: it quotes
third-party text, so it is gitignored with the rest of reports/."""
from __future__ import annotations

import json
import random

import polars as pl

from . import census, config, devset, strata
from .features import _blob

ROOT = config.ROOT
OUT = ROOT / "reports/cmd_check.md"


def write(tag: str | None, n: int = 20) -> str:
    d = ROOT / "data/features" / (f"c{tag}" if tag else "")
    cm = devset.nondev(pl.read_parquet(d / "commands.parquet")).select("owner", "repo", "name", "cmd")
    cm = cm.unique(subset=["owner", "repo", "name", "cmd"]).sort(["owner", "repo", "name", "cmd"])
    prim = pl.read_parquet(d / "a1_files.parquet").filter(pl.col("primary")).select("owner", "repo", "name", "path")
    ex = devset.nondev(strata.load_explore(tag)).select("owner", "repo", "stratum", "head_sha")
    cm = cm.join(prim, on=["owner", "repo", "name"], how="inner").join(ex, on=["owner", "repo"], how="inner")
    idx = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        idx[(j["owner"], j["repo"])] = j
    rng = random.Random(f"{config.load()['snapshot']['seed']}|cmdcheck")
    order = list(range(cm.height)); rng.shuffle(order)
    rows, md = [], ["# Command check (local only; contains third-party text)\n",
                    "For each item: does the line tell the agent to run the **normalized command** shown? Mark OK or WRONG (and why).",
                    "The normalizer maps e.g. `npm run test` -> `npm:test`, `uv run pytest` -> `uv:pytest`, `pytest -x` -> `pytest`.\n"]
    for i in order:
        r = cm.row(i, named=True)
        rec = idx.get((r["owner"], r["repo"]))
        f = next((f for f in (rec or {}).get("files", []) if f["path"] == r["path"]), None)
        text = _blob(f["blob_sha"]) if f else None
        if not text:
            continue
        hit = next(((ln, seg) for c, ln, seg in census.extract_with_lines(text) if c == r["cmd"]), None)
        if not hit:
            continue
        ln, seg = hit
        lines = text.splitlines()
        ctx = "\n".join(f"{k + 1:>4}{'>' if k + 1 == ln else ' '} {lines[k][:200]}" for k in range(max(0, ln - 3), min(len(lines), ln + 2)))
        k = len(rows) + 1
        md += [f"### {k}. normalized: `{r['cmd']}`   ({r['stratum'].replace('_', ' ')})",
               f"- extracted from the segment: `{seg[:160]}`",
               f"- source: {r['path']} line {ln} - https://github.com/{r['owner']}/{r['repo']}/blob/{r['head_sha']}/{r['path']}#L{ln}",
               "```", ctx, "```", "- verdict (OK / WRONG, why): ", ""]
        rows.append({"n": k, "repo": f"{r['owner']}/{r['repo']}", "cmd": r["cmd"], "line": ln})
        if len(rows) == n:
            break
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(md))
    return f"wrote reports/cmd_check.md ({len(rows)} commands drawn at random from {cm.height:,} distinct (repo, file, command) rows; seeded)"
