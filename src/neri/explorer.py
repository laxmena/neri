"""`neri explore`: write reports/explorer.html, a LOCAL-ONLY, self-contained page (no network, no CDN).
It embeds third-party path strings and repo names, so it is gitignored and must never be published or shared.
The page itself lives in explorer_template.html; this module only builds the data it embeds."""
from __future__ import annotations

import json
import time
from pathlib import Path

import polars as pl

from . import config, strata
from .features import COLUMN_DOCS

ROOT = config.ROOT
OUT = ROOT / "reports/explorer.html"
TEMPLATE = Path(__file__).with_name("explorer_template.html")
ECO = ["skills", "commands", "agents", "settings_json", "cursor_rules", "copilot_instructions", "mcp_json"]


def latest_tag() -> str | None:
    tags = sorted(int(p.name[1:]) for p in (ROOT / "data/features").glob("c*")
                  if p.name[1:].isdigit() and (p / "repos.parquet").exists())
    return str(tags[-1]) if tags else None


def _rd(d: Path, n: str):
    p = d / f"{n}.parquet"
    return pl.read_parquet(p) if p.exists() else None


def build_data(tag: str | None) -> dict:
    d = ROOT / "data/features" / (f"c{tag}" if tag else "")
    sdir = ROOT / "data/sample" / (f"c{tag}" if tag else "")
    repos, a1r, a1f, a2r, a2d, a3, a7, sk, cm = (_rd(d, n) for n in (
        "repos", "a1_repo", "a1_files", "a2_repo", "a2_detail", "a3", "a7", "skills", "commands"))
    meta = strata.load_explore(tag).select(
        "owner", "repo", "stars", "license", "head_sha", "pushed_at", "star_stratum", "stratum")
    mm = {(r["owner"], r["repo"]): r for r in meta.iter_rows(named=True)}
    key = lambda r, n: (r["owner"], r["repo"], n)
    a1_by = {key(r, r["name"]): r for r in a1r.iter_rows(named=True)}
    a2_by = {key(r, r["name"]): r for r in a2r.iter_rows(named=True)}
    a3_by = {key(r, r["name"]): r for r in a3.iter_rows(named=True)}
    shape = {key(r, r["name"]): r for r in a1f.filter(pl.col("primary")).iter_rows(named=True)}
    a7_by = {(r["owner"], r["repo"]): r for r in a7.iter_rows(named=True)}
    flagged: dict[tuple, list] = {}
    if a2d is not None:
        for r in a2d.filter(pl.col("dead")).iter_rows(named=True):
            tier = ("moved" if r.get("moved") else "missing") if r["type"] == "path" else "command"
            flagged.setdefault((r["owner"], r["repo"]), []).append(
                {"file": r["name"], "type": r["type"], "kind": r["kind"], "ref": r["ref"], "tier": tier})
    skill_names: dict[tuple, list] = {}
    if sk is not None:
        for r in sk.iter_rows(named=True):
            skill_names.setdefault((r["owner"], r["repo"]), []).append(r["skill"])
    cmds: dict[tuple, list] = {}
    if cm is not None:
        top = cm.group_by(["owner", "repo", "cmd"]).agg(pl.col("n").sum()).sort(["owner", "repo", "n"], descending=[False, False, True])
        for r in top.iter_rows(named=True):
            cmds.setdefault((r["owner"], r["repo"]), []).append(r["cmd"])
    rows = []
    for r in repos.iter_rows(named=True):
        k = (r["owner"], r["repo"])
        m = mm.get(k)
        names = [n for n in ("CLAUDE.md", "AGENTS.md") if key(r, n) in a1_by]
        if not m or not names:
            continue
        prim = names[0]
        a, b, c, s = a1_by[key(r, prim)], a2_by[key(r, prim)], a3_by[key(r, prim)], shape.get(key(r, prim), {})
        fl = flagged.get(k, [])
        e = a7_by.get(k, {})
        rows.append({
            "repo": f"{k[0]}/{k[1]}", "sha": m["head_sha"], "stars": m["stars"], "bucket": m["star_stratum"].replace("-1000000000", "+"),
            "license": m["license"] or "none", "pushed": m["pushed_at"], "stratum": m["stratum"].replace("_", " "), "toolchain": r["toolchain"],
            "files": "+".join(n[0] for n in names), "names": names, "dev": bool(r.get("dev", False)),
            "tokens": a["tokens"], "always_loaded": a["always_loaded_tokens"], "on_demand": a["on_demand_tokens"],
            "structure": a["structure"], "n_imports": a["n_imports"], "lines": s.get("lines"), "headings": s.get("headings"),
            "code_blocks": s.get("code_blocks"), "links": s.get("links"),
            "shout": c["shout_per_1k"], "neg": c["negation_share"], "imp": c["imperative_share"],
            "paths_checked": sum(a2_by[key(r, n)]["paths_checked"] for n in names),
            "paths_dead": sum(a2_by[key(r, n)]["paths_dead"] for n in names),
            "cmds_checked": sum(a2_by[key(r, n)]["cmds_checked"] for n in names),
            "cmds_dead": sum(a2_by[key(r, n)]["cmds_dead"] for n in names),
            "unknown": sum(a2_by[key(r, n)]["paths_unknown"] + a2_by[key(r, n)]["cmds_unknown"] for n in names),
            "skills": len(skill_names.get(k, [])), "skill_names": sorted(skill_names.get(k, [])),
            "cmds": cmds.get(k, [])[:30], "eco": {x: bool(e.get(x, False)) for x in ECO}, "flagged": fl[:40]})
    return {"tag": tag or "full", "generated": time.strftime("%Y-%m-%d %H:%M"), "n_repos": len(rows), "rows": rows,
            "eco": ECO, "docs": COLUMN_DOCS}


def write(tag: str | None = None, full: bool = False) -> str:
    tag = None if full else (tag or latest_tag())
    data = build_data(tag)
    blob = json.dumps(data).replace("</", "<\\/")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(TEMPLATE.read_text().replace("__DATA__", blob))
    return f"wrote {OUT.relative_to(ROOT)} (checkpoint {data['tag']}, {data['n_repos']} repos, {OUT.stat().st_size // 1024} KB; local only, gitignored)"
