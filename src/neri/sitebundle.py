"""site-data/: aggregates-only bundle for the concepts page (no text, no repo names). Built from reports/numbers.json,
reports/widget_percentiles.json, reports/top_commands.json and the weighted data (ECDF points, chars-per-token calibration)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

from . import config, report

ROOT = config.ROOT
GROUPS = ("established", "long_tail", "overall")
EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop", "SubagentStop", "Notification", "PreCompact")
LINKS = ("symlink", "import_bridge", "prose_pointer", "near_copy", "independent")


def _est(nums, key):
    e = nums.get(key)
    if not e:
        return None
    return {k: e.get(k) for k in ("value", "ci_lo", "ci_hi", "n", "unweighted", "low_n")}


def build(out: Path, tag=None) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    full = json.loads((ROOT / "reports/numbers.json").read_text())
    nums, meta, st = full["numbers"], full["meta"], full["strata"]
    # --- ECDF points (<=200 per curve) and chars-per-token calibration
    t = report._tables(tag)
    ecdf, cal = {}, {}
    for ft, name in report.FILES.items():
        df = report.load_file(tag, name, t).filter(pl.col("structure") != "nested-only")
        ecdf[ft] = {}
        for g in GROUPS:
            sub = df if g == "overall" else df.filter(pl.col("stratum") == g)
            x, w = sub["always_loaded_tokens"].to_numpy().astype(float), sub["w"].to_numpy()
            o = np.argsort(x)
            xs, cw = x[o], np.cumsum(w[o]) / w.sum()
            idx = np.unique(np.linspace(0, len(xs) - 1, 200).round().astype(int))
            ecdf[ft][g] = {"n": int(sub.height), "tokens": [float(xs[i]) for i in idx], "cum_share": [round(float(cw[i]), 5) for i in idx]}
        if ft == "claude":
            d = report.load_file(tag, name, t).filter((pl.col("tokens") > 0) & (pl.col("chars") > 0))
            r = (d["chars"] / d["tokens"]).to_numpy()
            w = d["w"].to_numpy()
            o = np.argsort(r)
            cw = np.cumsum(w[o]) / w.sum()
            q = lambda p: float(r[o][int(np.searchsorted(cw, p))])
            cal = {"n": int(d.height), "weighted": {"p25": q(.25), "median": q(.5), "p75": q(.75)},
                   "unweighted": {"p25": float(np.percentile(r, 25)), "median": float(np.median(r)), "p75": float(np.percentile(r, 75))},
                   "note": "characters per token (o200k_base proxy) of the primary CLAUDE.md of each repo; estimate tokens in the browser as ceil(chars / median). Weighted by repo weight."}
    (out / "ecdf.json").write_text(json.dumps({"note": "weighted ECDF of always-loaded tokens (root file + @imports; nested-only repos excluded), <=200 points per curve", "files": ecdf}))
    (out / "calibration.json").write_text(json.dumps(cal, indent=1))
    # --- percentiles (lines, tokens) from the widget file
    wp = json.loads((ROOT / "reports/widget_percentiles.json").read_text())
    pct = {"note": wp.get("percentiles_note"), "files": {}}
    for ft, sc in wp["files"].items():
        pct["files"][ft] = {g: {grp: {m: v for m, v in d.items() if m in ("lines", "always_loaded_tokens", "tokens")} for grp, d in gv.items()} for g, gv in sc.items()}
    (out / "percentiles.json").write_text(json.dumps(pct))
    (out / "top_commands.json").write_text((ROOT / "reports/top_commands.json").read_text())
    # --- curated findings
    F = {}
    for ft in ("claude", "agents"):
        for g in GROUPS:
            for k in ("a8.overview.share_files", "a8.overview.token_share", "a8.tree_dump.share_files", "a8.tree_dump.token_share", "a1.always_loaded_tokens.median", "a1.always_loaded_tokens.p90", "a1.lines.median"):
                F[f"{ft}.{g}.{k}"] = _est(nums, f"{ft}.{g}.{k}")
    for g in GROUPS:
        F[f"claude.{g}.g.sig.share"] = _est(nums, f"claude.{g}.g.sig.share")
        for grp in ("sig", "nosig"):
            for k in ("overview.share_files", "overview.token_share", "tokens.median"):
                F[f"claude.{g}.g.{grp}.{k}"] = _est(nums, f"claude.{g}.g.{grp}.{k}")
        for c in LINKS + ("linked_any",):
            F[f"repo.{g}.link.{'class.' + c if c != 'linked_any' else 'linked_any'}"] = _est(nums, f"repo.{g}.link.{'class.' + c if c != 'linked_any' else 'linked_any'}")
        F[f"repo.{g}.h.any.share_of_repos"] = _est(nums, f"repo.{g}.h.any.share_of_repos")
        for e in EVENTS:
            F[f"repo.{g}.h.ev.{e}.share_of_repos_with_hooks"] = _est(nums, f"repo.{g}.h.ev.{e}.share_of_repos_with_hooks")
    F["repo.overall.stack.has_agents_md"] = _est(nums, "repo.overall.stack.has_agents_md")
    F["repo.overall.stack.has_claude_md"] = _est(nums, "repo.overall.stack.has_claude_md")
    F["repo.overall.pop.small_personal_share"] = _est(nums, "repo.overall.pop.small_personal_share")
    F = {k: v for k, v in F.items() if v}
    nb = meta["n_by_cell"]
    totals = {"snapshot_date": st["snapshot_date"], "n_files_total": nb["claude.overall"] + nb["agents.overall"], "n_claude_files": nb["claude.overall"], "n_agents_files": nb["agents.overall"],
              "n_repos_overall": nb["repo.overall"], "n_repos_popular": nb["repo.established"], "n_repos_small_personal": nb["repo.long_tail"],
              "n_claude_files_popular": nb["claude.established"], "n_claude_files_small_personal": nb["claude.long_tail"],
              "n_agents_files_popular": nb["agents.established"], "n_agents_files_small_personal": nb["agents.long_tail"],
              "population_popular_repos": st["established"]["N_nondev"], "population_small_personal_repos": st["long_tail"]["N_h"]}
    (out / "numbers.json").write_text(json.dumps({"generated": full.get("generated"), "bootstrap": full.get("bootstrap"), "definitions": {"overall": full["definitions"]["overall"], "strata": full["definitions"]["strata"]},
                                                  "totals": totals, "numbers": F}, indent=1))
    # figure tables
    for f in sorted((ROOT / "figures/post").glob("*.csv")):
        (out / f.name).write_text(f.read_text())
    return [f"site-data: {len(F)} numbers, ecdf {sum(len(v) for v in ecdf.values())} curves, chars/token median {cal['weighted']['median']:.2f}"]
