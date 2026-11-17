"""`neri report`: weighted estimates with repo-clustered bootstrap CIs -> reports/numbers.json, plus RAW-distribution figures in
figures/explore/ (post figures come from postfigs.py after the human reviews these).

Rules (docs/DECISIONS.md, docs/POST1-PLAN.md): non-dev repos only (the c5000 explore repos are the DEV set); every cell carries n
(repos), the query that made it, weighted AND unweighted values, a 95% CI from 1,000 repo-level bootstrap resamples (the repo is
the cluster: one row per repo and file type), a trimmed-weights sensitivity value, and a low_n flag when n < 100. A2 (wrongness)
is OUT of post 1 and is not reported.

Scopes: file types `claude` and `agents` (repos whose primary file of that name is the file of record; a repo with both appears in
both), and `repo` (one row per repo, repo-level metrics: ecosystem, stacks, skills depth). Strata: `established`, `long_tail`,
`overall` (weighted union) plus `diff` = long tail minus established.
"""
from __future__ import annotations

import json
import time

import numpy as np
import polars as pl

from . import config, devset, strata

ROOT = config.ROOT
OUT = ROOT / "reports/numbers.json"
FIG = ROOT / "figures/explore"
FILES = {"claude": "CLAUDE.md", "agents": "AGENTS.md"}
LOW_N = 100
TRIM_Q = 0.99

DEFINITIONS = {
    "overall": ("Weighted 'overall' = the candidate population covered by the sampled buckets (repos that code search returned through "
                "the randomly selected size buckets of CLAUDE.md and AGENTS.md files), scaled to GitHub-wide via inclusion probabilities: "
                "each repo is weighted by 1 / (P(it entered the candidate pool) x P(it was drawn within its stratum)). 'GitHub-wide' "
                "means public, active, non-archived repositories that code search indexes (see limitations)."),
    "strata": "established = stars >= 10; long_tail = stars 0-9; both: not a fork, not archived, pushed within 180 days, size 200 B-100 KB, non-dev.",
    "unit": "the repo (also the bootstrap cluster); one row per repo and file type",
    "sensitivity": f"trimmed = weights capped at the {int(TRIM_Q * 100)}th percentile of the cell's weights; delta = trimmed - untrimmed; "
                   "sensitive = |delta| exceeds the CI half-width. Weights are nearly two-valued, so trimming is often a no-op (n_trimmed = 0).",
}


# ---------- estimators ----------

def wquant(x: np.ndarray, w: np.ndarray, q: float) -> float:
    o = np.argsort(x, kind="stable")
    x, w = x[o], w[o]
    c = np.cumsum(w)
    return float(x[min(len(x) - 1, int(np.searchsorted(c, q * c[-1])))])


AGE_BANDS = ((0, 182, "lt6m"), (182, 365, "6_12m"), (365, 730, "1_2y"), (730, 1e9, "2y_plus"))
AGE_KEEP = ("a1.tokens", "a1.always_loaded", "a1.lines", "a3.", "a8.", "cmd.any", "sc.score", "sc.share_5_plus", "x2.smell_any", "stack.", "a7_with_claude.any_of_four", "d3.", "d4.share_non_english", "d5.")
QUANT = {"median": 0.5, "p25": 0.25, "p75": 0.75, "p90": 0.90}


def _stat(kind: str, x: np.ndarray, w: np.ndarray) -> float:
    if kind in QUANT:
        return wquant(x, w, QUANT[kind])
    if kind == "ratio":  # x has two columns: numerator, denominator
        return float(np.sum(w * x[:, 0]) / np.sum(w * x[:, 1]))
    return float(np.sum(w * x) / np.sum(w))  # share / mean


def _boot(kind: str, x: np.ndarray, w: np.ndarray, rng: np.random.Generator, B: int) -> np.ndarray:
    """Vectorised repo-level bootstrap: B resamples of the n repos with replacement."""
    n = len(x)
    I = rng.integers(0, n, (B, n))
    wi = w[I]
    if kind in QUANT:
        xs = x[I]
        o = np.argsort(xs, axis=1, kind="stable")
        xs, ws = np.take_along_axis(xs, o, 1), np.take_along_axis(wi, o, 1)
        c = np.cumsum(ws, axis=1)
        idx = np.minimum((c < QUANT[kind] * c[:, -1:]).sum(axis=1), n - 1)
        return xs[np.arange(B), idx]
    if kind == "ratio":
        return (wi * x[I, 0]).sum(1) / (wi * x[I, 1]).sum(1)
    return (wi * x[I]).sum(1) / wi.sum(1)


def _clean(x, w):
    x, w = np.asarray(x, dtype=float), np.asarray(w, dtype=float)
    ok = ~np.isnan(x) if x.ndim == 1 else ~np.isnan(x).any(axis=1)
    return x[ok], w[ok]


def estimate(x, w, kind: str, rng: np.random.Generator, B: int) -> dict:
    x, w = _clean(x, w)
    n = len(x)
    if n == 0:
        return {"n": 0, "value": None, "unweighted": None, "ci_lo": None, "ci_hi": None}
    boots = _boot(kind, x, w, rng, B)
    return {"n": n, "value": _stat(kind, x, w), "unweighted": _stat(kind, x, np.ones(n)),
            "ci_lo": float(np.quantile(boots, 0.025)), "ci_hi": float(np.quantile(boots, 0.975))}


def estimate_diff(x1, w1, x2, w2, kind: str, rng: np.random.Generator, B: int) -> dict:
    """Long tail (2) minus established (1): each stratum is resampled independently at repo level."""
    x1, w1 = _clean(x1, w1)
    x2, w2 = _clean(x2, w2)
    if len(x1) == 0 or len(x2) == 0:
        return {"n": [len(x1), len(x2)], "value": None, "ci_lo": None, "ci_hi": None}
    d = _boot(kind, x2, w2, rng, B) - _boot(kind, x1, w1, rng, B)
    return {"n": [len(x1), len(x2)], "value": _stat(kind, x2, w2) - _stat(kind, x1, w1),
            "ci_lo": float(np.quantile(d, 0.025)), "ci_hi": float(np.quantile(d, 0.975))}


def sensitivity(x, w, kind: str, entry: dict, cap: float) -> dict:
    """Same estimate with weights capped at `cap` (the p99 of the cell's weights): the robustness check."""
    x, w = _clean(x, w)
    if len(x) == 0 or entry["value"] is None:
        return {}
    wt = np.minimum(w, cap)
    tv = _stat(kind, x, wt)
    half = (entry["ci_hi"] - entry["ci_lo"]) / 2
    return {"trimmed_value": tv, "delta": tv - entry["value"], "cap_p99": float(cap), "n_trimmed": int((w > cap).sum()),
            "sensitive": bool(abs(tv - entry["value"]) > half)}


# ---------- data ----------

def _tables(tag: str | None) -> dict[str, pl.DataFrame]:
    d = ROOT / "data/features" / (f"c{tag}" if tag else "")
    rd = lambda n: devset.nondev(pl.read_parquet(d / f"{n}.parquet")) if (d / f"{n}.parquet").exists() else None
    t = {n: rd(n) for n in ("a1_repo", "a1_files", "a3", "a5", "a7", "repos", "a8", "stacks", "skills", "commands")}
    ex = devset.nondev(strata.load_explore(tag)).select("owner", "repo", "stars", "sample_prob", "stratum")
    t["ex"] = ex
    for n in ("x_files", "x_repo", "x_repo2", "x_files2", "x_repo3"):  # extras (neri extras): already non-dev
        t[n] = pl.read_parquet(d / f"{n}.parquet") if (d / f"{n}.parquet").exists() else None
    return t


def _require_strata(tag):
    if not (strata.table(tag).get("established") or {}).get("N_nondev_by_stratum"):
        raise SystemExit("strata.json lacks the non-dev populations: run `neri strata --checkpoint N` (or the chain) first")


_WARNED: set = set()


def _weights(df: pl.DataFrame) -> pl.DataFrame:
    bad = df.filter(~(pl.col("incl_prob_repo") > 0)).height
    if bad and bad not in _WARNED:
        _WARNED.add(bad)
        print(f"WARNING: {bad} repo(s) have inclusion probability 0 (cannot be weighted) and are excluded")
    return df.filter(pl.col("incl_prob_repo") > 0).with_columns(w=1.0 / (pl.col("incl_prob_repo") * pl.col("sample_prob")))


def load_file(tag: str | None, name: str, t: dict | None = None) -> pl.DataFrame:
    """One row per repo whose primary file of this name is the file of record (root file, or the primary nested file)."""
    t = t or _tables(tag)
    a1 = t["a1_repo"].filter(pl.col("name") == name).select("owner", "repo", "tokens", "always_loaded_tokens", "on_demand_tokens", "structure", "nested_only")
    sh = t["a1_files"].filter(pl.col("primary") & (pl.col("name") == name)).select("owner", "repo", "path", "lines", "headings", "code_blocks", "links", "chars")
    a3 = t["a3"].filter(pl.col("name") == name).select("owner", "repo", "shout_per_1k", "shout_words", "caps_lines", "negation_share", "imperative_share")
    a8 = t["a8"].filter(pl.col("name") == name).select("owner", "repo", "tree_dump", "tree_dump_tokens", "overview", "overview_tokens")
    df = a1
    for o in (sh, a3, a8):
        df = df.join(o, on=["owner", "repo"], how="left")
    if t["a5"] is not None:
        a5 = t["a5"].select("owner", "repo", "path", "cluster_size", "exact_dup_size")
        df = df.join(a5, on=["owner", "repo", "path"], how="left")
    if t.get("x_files") is not None:
        xf = t["x_files"].filter(pl.col("name") == name).drop("name", "n_shingles")
        df = df.join(xf, on=["owner", "repo"], how="left")
    if t.get("x_repo2") is not None:
        df = df.join(t["x_repo2"].select("owner", "repo", "src_bytes", "age_days"), on=["owner", "repo"], how="left")
    if t.get("x_files2") is not None:
        df = df.join(t["x_files2"].filter(pl.col("name") == name).select("owner", "repo", "sig_init"), on=["owner", "repo"], how="left")
    rp = t["repos"].select("owner", "repo", "incl_prob_repo", "toolchain")
    df = df.join(rp, on=["owner", "repo"], how="left").join(t["ex"], on=["owner", "repo"], how="inner")
    return _weights(df.filter(pl.col("tokens").is_not_null()))


def load_repo(tag: str | None, t: dict | None = None) -> pl.DataFrame:
    """One row per repo (any primary file): ecosystem flags, stack, bridge, skills depth."""
    t = t or _tables(tag)
    base = t["a1_repo"].select("owner", "repo").unique()
    df = (base.join(t["a7"], on=["owner", "repo"], how="left").join(t["stacks"], on=["owner", "repo"], how="left")
          .join(t["repos"].select("owner", "repo", "incl_prob_repo", "toolchain"), on=["owner", "repo"], how="left"))
    sk = t["skills"]
    if sk is not None:
        per = sk.group_by(["owner", "repo"]).agg(pl.len().alias("n_skills"), (pl.col("n_script_files") > 0).sum().alias("n_skills_scripts"))
        df = df.join(per, on=["owner", "repo"], how="left")
    else:
        df = df.with_columns(n_skills=pl.lit(None, dtype=pl.UInt32), n_skills_scripts=pl.lit(None, dtype=pl.UInt32))
    df = df.with_columns(n_skills=pl.col("n_skills").fill_null(0), n_skills_scripts=pl.col("n_skills_scripts").fill_null(0))
    crow = t["a1_repo"].filter(pl.col("name") == "CLAUDE.md").select("owner", "repo").unique().with_columns(claude_file_row=pl.lit(True))
    df = df.join(crow, on=["owner", "repo"], how="left").join(t["repos"].select("owner", "repo", "has_claude"), on=["owner", "repo"], how="left")
    if t.get("x_repo") is not None:
        df = df.join(t["x_repo"], on=["owner", "repo"], how="left")
    if t.get("x_repo2") is not None:
        df = df.join(t["x_repo2"], on=["owner", "repo"], how="left")
    if t.get("x_repo3") is not None:
        df = df.join(t["x_repo3"], on=["owner", "repo"], how="left")
    df = df.join(t["ex"], on=["owner", "repo"], how="inner")
    return _weights(df)


def _nan_where(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return np.where(mask, np.nan, x)


def file_vectors(df: pl.DataFrame, name: str, cm: pl.DataFrame | None, thr: float | None = None, thr_lines: float | None = None) -> dict[str, tuple[str, np.ndarray]]:
    """metric key -> (kind, per-repo vector)."""
    f = lambda c: df[c].to_numpy().astype(float)
    nested_only = (df["structure"] == "nested-only").to_numpy()
    v: dict[str, tuple[str, np.ndarray]] = {}
    for c, k in (("tokens", "a1.tokens"), ("lines", "a1.lines"), ("headings", "a1.headings"), ("code_blocks", "a1.code_blocks"), ("links", "a1.links")):
        v[f"{k}.median"] = ("median", f(c))
    for q in ("p25", "median", "p75", "p90"):  # nested-only repos load nothing from the root: measured over repos WITH a root file
        v[f"a1.always_loaded_tokens.{q}"] = (q, _nan_where(f("always_loaded_tokens"), nested_only))
    od = f("on_demand_tokens")
    v["a1.on_demand.share_with_nested_files"] = ("share", (od > 0).astype(float))
    v["a1.on_demand_tokens.median_among_with_nested"] = ("median", np.where(od > 0, od, np.nan))
    for s in ("single", "imports", "nested", "both", "nested-only"):
        v[f"a1.structure.{s.replace('-', '_')}"] = ("share", (df["structure"] == s).cast(pl.Float64).to_numpy())
    v["a3.shout_per_1k.median"] = ("median", f("shout_per_1k"))
    v["a3.share_with_keyword_shouting"] = ("share", (f("shout_words") > 0).astype(float))
    v["a3.share_with_keyword_or_caps"] = ("share", ((f("shout_words") > 0) | (f("caps_lines") > 0)).astype(float))
    v["a3.share_caps_only"] = ("share", ((f("shout_words") == 0) & (f("caps_lines") > 0)).astype(float))
    v["a3.negation_share.median"] = ("median", f("negation_share"))
    v["a3.imperative_share.median"] = ("median", f("imperative_share"))
    if "sig_init" in df.columns:  # round 3: generator signature ('provides guidance to Claude Code' in the first 10 non-empty lines)
        sg = df["sig_init"].to_numpy()
        known = df["sig_init"].is_not_null().to_numpy()
        sgb = np.where(known, np.asarray([bool(x) for x in sg]), False)
        v["g.sig.share"] = ("share", np.where(known, sgb.astype(float), np.nan))
        for tag_, m in (("sig", sgb & known), ("nosig", ~sgb & known)):
            mk = lambda x: np.where(m, x, np.nan)
            v[f"g.{tag_}.overview.share_files"] = ("share", mk(df["overview"].cast(pl.Float64).to_numpy()))
            v[f"g.{tag_}.overview.token_share"] = ("ratio", np.where(m[:, None], np.column_stack([f("overview_tokens"), f("tokens")]), np.nan))
            v[f"g.{tag_}.tree_dump.share_files"] = ("share", mk(df["tree_dump"].cast(pl.Float64).to_numpy()))
            v[f"g.{tag_}.tokens.median"] = ("median", mk(f("tokens")))
            v[f"g.{tag_}.negation_share.median"] = ("median", mk(f("negation_share")))
            v[f"g.{tag_}.negation_any.share"] = ("share", mk((f("negation_share") > 0).astype(float)))
    v["a8.tree_dump.share_files"] = ("share", df["tree_dump"].cast(pl.Float64).to_numpy())
    v["a8.tree_dump.token_share"] = ("ratio", np.column_stack([f("tree_dump_tokens"), f("tokens")]))
    v["a8.overview.share_files"] = ("share", df["overview"].cast(pl.Float64).to_numpy())
    v["a8.overview.token_share"] = ("ratio", np.column_stack([f("overview_tokens"), f("tokens")]))
    if "cluster_size_exact" in df.columns:  # exact-verified near-duplicate edges only (Codex #8)
        cx = df["cluster_size_exact"].cast(pl.Float64).to_numpy()
        v["a5.share_in_clusters_of_5_plus"] = ("share", np.where(np.isnan(cx), np.nan, (cx >= 5).astype(float)))
    elif "cluster_size" in df.columns:
        v["a5.share_in_clusters_of_5_plus"] = ("share", np.where(np.isnan(f("cluster_size")), np.nan, (f("cluster_size") >= 5).astype(float)))
    if "exact_dup_size" in df.columns:
        v["a5.share_exact_duplicate"] = ("share", np.where(np.isnan(f("exact_dup_size")), np.nan, (f("exact_dup_size") > 1).astype(float)))
    if "overlap_share" in df.columns:  # README overlap (denominator: files in repos with a README and >= 1 five-word shingle)
        rp = df["readme_present"].fill_null(False).cast(pl.Float64).to_numpy()
        v["a9.readme.has_readme"] = ("share", rp)
        os_ = df["overlap_share"].cast(pl.Float64).to_numpy()
        v["a9.overlap.shingle_share.median"] = ("median", os_)
        v["a9.overlap.share_over_25"] = ("share", np.where(np.isnan(os_), np.nan, (os_ > 0.25).astype(float)))
        ot = df["overlap_tokens"].cast(pl.Float64).to_numpy()
        v["a9.overlap.tokens.median"] = ("median", ot)
        v["a9.overlap.token_share"] = ("ratio", np.where(np.isnan(ot)[:, None], np.nan, np.column_stack([ot, f("tokens")])))
    if "has_test" in df.columns and thr is not None:  # essentials scorecard: 6 deterministic checks
        b = lambda c: df[c].fill_null(False).cast(pl.Float64).to_numpy()
        al = np.where(nested_only, np.nan, f("always_loaded_tokens"))
        crit = {"has_test": b("has_test"), "has_build": b("has_build"), "has_dont": b("has_dont"),
                "no_tree": 1.0 - b("tree_dump"), "no_overview": 1.0 - b("overview"), "light": np.where(np.isnan(al), np.nan, (al <= thr).astype(float))}
        for k, x in crit.items():
            v[f"sc.pass.{k}"] = ("share", x)
        score = np.sum(np.column_stack(list(crit.values())), axis=1)  # NaN when always-loaded is undefined (nested-only)
        v["sc.score.mean"] = ("mean", score)
        v["sc.score.median"] = ("median", score)
        v["sc.share_5_plus"] = ("share", np.where(np.isnan(score), np.nan, (score >= 5).astype(float)))
        for k in range(7):
            v[f"sc.dist.{k}"] = ("share", np.where(np.isnan(score), np.nan, (score == k).astype(float)))
    if "lang" in df.columns:  # D4-D6, X1-X3 (deterministic text features)
        ff = lambda c: df[c].fill_null(False).cast(pl.Float64).to_numpy()
        lang = df["lang"].fill_null("unknown").to_numpy()
        det = lang != "unknown"
        v["d4.share_detected"] = ("share", det.astype(float))
        v["d4.share_non_english"] = ("share", np.where(det, (lang != "english").astype(float), np.nan))
        for lg in ["english", "tamil"] + [l for l, _ in sorted(((l, int((lang == l).sum())) for l in set(lang) - {"unknown", "english", "tamil"}), key=lambda x: -x[1])[:8]]:
            v[f"d4.lang.{lg}"] = ("share", np.where(det, (lang == lg).astype(float), np.nan))
        for c, k in (("persona", "d5.persona_line"), ("says_please", "d5.says_please"), ("says_thanks", "d5.says_thanks"), ("names_vendor", "d5.names_vendor_or_model"), ("names_claude", "d5.names_claude")):
            v[k] = ("share", ff(c))
        v["d5.tool_neutral"] = ("share", 1.0 - ff("names_vendor"))
        v["d6.mentions_security"] = ("share", ff("mentions_security"))
        nl = df["n_nonempty_lines"].cast(pl.Float64).to_numpy()
        for grp, mask in (("", nl >= 6), ("_long", (nl >= 6) & (nl > (thr_lines if thr_lines is not None else np.inf)))):
            for kind, nm in (("emph", "emphasis"), ("neg", "negation")):
                tot = sum(df[f"{kind}_{t}"].cast(pl.Float64).to_numpy() for t in ("first", "mid", "last"))
                for t in ("first", "mid", "last"):
                    x = df[f"{kind}_{t}"].cast(pl.Float64).to_numpy()
                    v[f"x1{grp}.{nm}.{t}_third"] = ("ratio", np.where((mask & (tot > 0))[:, None], np.column_stack([x, tot]), np.nan))
        for k in ("vague", "dup", "contra", "template", "any"):
            v[f"x2.smell_{k}"] = ("share", ff(f"smell_{k}"))
        ni = df["n_items"].cast(pl.Float64).to_numpy()
        for k in ("prohib", "oblig", "permit", "descr"):
            v[f"x3.{k}"] = ("ratio", np.where((ni > 0)[:, None], np.column_stack([df[f"deont_{k}"].cast(pl.Float64).to_numpy(), ni]), np.nan))
    if "src_bytes" in df.columns:  # D2: instructions vs code
        sb = df["src_bytes"].cast(pl.Float64).to_numpy()
        ab = df["chars"].cast(pl.Float64).to_numpy()
        hasr = (sb > 0)
        ratio = np.where(hasr, ab / np.where(hasr, sb, 1), np.nan)
        v["d2.share_with_source"] = ("share", np.where(np.isnan(sb), np.nan, hasr.astype(float)))
        for q in ("p25", "median", "p75", "p90"):
            v[f"d2.bytes_ratio.{q}"] = (q, ratio)
        v["d2.share_agent_file_larger_than_code"] = ("share", np.where(np.isnan(ratio), np.nan, (ratio > 1).astype(float)))
    if "age_days" in df.columns:
        ag = df["age_days"].cast(pl.Float64).to_numpy()
        for lo_, hi_, nm_ in AGE_BANDS:
            v[f"d3.age.{nm_}"] = ("share", np.where(np.isnan(ag), np.nan, ((ag >= lo_) & (ag < hi_)).astype(float)))
    if cm is not None and cm.height:
        per = cm.filter(pl.col("name") == name).group_by(["owner", "repo", "cmd"]).agg(pl.len())
        keys = list(zip(df["owner"], df["repo"]))
        top = per.group_by("cmd").agg(pl.len().alias("n")).sort("n", descending=True).head(10)["cmd"].to_list()
        for c in top:
            have = {tuple(k) for k in per.filter(pl.col("cmd") == c).select("owner", "repo").iter_rows()}
            v[f"cmd.{c}"] = ("share", np.array([1.0 if k in have else 0.0 for k in keys]))
        anyc = {tuple(k) for k in per.select("owner", "repo").unique().iter_rows()}
        v["cmd.any"] = ("share", np.array([1.0 if k in anyc else 0.0 for k in keys]))
    return v


def repo_vectors(df: pl.DataFrame) -> dict[str, tuple[str, np.ndarray]]:
    f = lambda c: df[c].to_numpy().astype(float)
    v: dict[str, tuple[str, np.ndarray]] = {}
    for c in ("skills", "commands", "agents", "settings_json", "cursor_rules", "copilot_instructions", "mcp_json"):
        v[f"a7.{c}"] = ("share", df[c].cast(pl.Float64).to_numpy())
    hc = np.array([1.0 if "CLAUDE.md" in (m or "") else 0.0 for m in df["members"]])
    for c in ("skills", "commands", "agents", "settings_json"):  # .claude/* features: denominator = repos that have a CLAUDE.md
        v[f"a7_with_claude.{c}"] = ("share", np.where(hc > 0, df[c].cast(pl.Float64).to_numpy(), np.nan))
    v["a7_with_claude.any_of_four"] = ("share", np.where(hc > 0, np.maximum.reduce([df[c].cast(pl.Float64).to_numpy() for c in ("skills", "commands", "agents", "settings_json")]), np.nan))
    v["pop.small_personal_share"] = ("share", (df["stratum"] == "long_tail").cast(pl.Float64).to_numpy())
    ss = f("stack_size")
    for k, lab in ((1, "1"), (2, "2"), (3, "3")):
        v[f"stack.size_{lab}"] = ("share", (ss == k).astype(float))
    v["stack.size_4_plus"] = ("share", (ss >= 4).astype(float))
    v["stack.multi_file"] = ("share", (ss >= 2).astype(float))
    v["stack.size.mean"] = ("mean", ss)
    v["stack.size.p90"] = ("p90", ss)
    hcl = df["has_claude"].fill_null(False).to_numpy()
    v["stack.claude_in_tree_without_file_row"] = ("share", np.where(hcl, (~df["claude_file_row"].fill_null(False).to_numpy()).astype(float), np.nan))
    v["stack.has_claude_md"] = ("share", np.array([1.0 if "CLAUDE.md" in (m or "") else 0.0 for m in df["members"]]))
    v["stack.has_agents_md"] = ("share", np.array([1.0 if "AGENTS.md" in (m or "") else 0.0 for m in df["members"]]))
    both = df["both_root_files"].fill_null(False).to_numpy()
    v["stack.both_root_files"] = ("share", both.astype(float))
    v["stack.bridge.share_of_repos"] = ("share", df["bridge"].fill_null(False).cast(pl.Float64).to_numpy())
    v["stack.bridge.share_of_repos_with_both_files"] = ("share", np.where(both, df["bridge"].fill_null(False).cast(pl.Float64).to_numpy(), np.nan))
    v["stack.bridge.symlink"] = ("share", (df["bridge_kind"].fill_null("") == "symlink").cast(pl.Float64).to_numpy())
    v["stack.bridge.import"] = ("share", (df["bridge_kind"].fill_null("") == "import").cast(pl.Float64).to_numpy())
    v["stack.reverse_symlink_agents_to_claude"] = ("share", df["agents_symlink_to_claude"].fill_null(False).cast(pl.Float64).to_numpy())
    if "link_class" in df.columns:  # A: pair classes among repos with both root files (denominator: repos with a classified pair)
        lc = df["link_class"].fill_null("").to_numpy()
        has = lc != ""
        for c in ("symlink", "import_bridge", "prose_pointer", "near_copy", "independent"):
            v[f"link.class.{c}"] = ("share", np.where(has, (lc == c).astype(float), np.nan))
        v["link.linked_any"] = ("share", np.where(has, np.isin(lc, ["symlink", "import_bridge", "prose_pointer"]).astype(float), np.nan))
        v["link.pairs_share_of_repos"] = ("share", has.astype(float))
    if "line_jaccard" in df.columns:
        el = df["drift_eligible"].fill_null(False).to_numpy()
        jac = np.where(el, df["line_jaccard"].cast(pl.Float64).to_numpy(), np.nan)
        v["drift.eligible_share_of_repos"] = ("share", el.astype(float))
        v["drift.line_jaccard.median"] = ("median", jac)
        v["drift.share_diverged"] = ("share", np.where(np.isnan(jac), np.nan, (jac < 0.5).astype(float)))
        for lo_, hi_, nm_ in ((0, 0.1, "lt10"), (0.1, 0.5, "10_50"), (0.5, 0.9, "50_90"), (0.9, 1.01, "ge90")):
            v[f"drift.jaccard_bin.{nm_}"] = ("share", np.where(np.isnan(jac), np.nan, ((jac >= lo_) & (jac < hi_)).astype(float)))
        bt = df["both_name_tb_cmds"].fill_null(False).to_numpy()
        dif = df["tb_cmds_differ"].cast(pl.Float64).to_numpy()
        v["drift.both_name_test_build_cmds"] = ("share", np.where(el, bt.astype(float), np.nan))
        v["drift.share_differ_test_build_cmds"] = ("share", np.where(el & bt, dif, np.nan))
    if "settings_parsed" in df.columns:  # D1: committed .claude/settings*.json
        sp = df["settings_parsed"].fill_null(False).to_numpy()
        v["d1.has_settings_file"] = ("share", df["has_settings_file"].fill_null(False).cast(pl.Float64).to_numpy())
        v["d1.settings_parsed_share_of_repos"] = ("share", sp.astype(float))
        for k in ("s_any_allow", "s_broad_bash", "s_network", "s_bypass", "s_accept_edits", "s_all_mcp", "s_has_deny", "s_hooks", "s_hook_format_lint", "s_hook_test", "s_hook_guard", "s_hook_notify", "s_hook_other"):
            v[f"d1.{k[2:]}"] = ("share", np.where(sp, df[k].fill_null(False).cast(pl.Float64).to_numpy(), np.nan))
    if "h_any" in df.columns:  # round 3: hooks (committed settings only) and tool co-presence
        sp3 = df["settings_parsed"].fill_null(False).to_numpy() if "settings_parsed" in df.columns else np.ones(df.height, bool)
        ha = df["h_any"].fill_null(False).to_numpy()
        v["h.any.share_of_repos"] = ("share", ha.astype(float))
        v["h.any.share_of_repos_with_parsed_settings"] = ("share", np.where(sp3, ha.astype(float), np.nan))
        for c in df.columns:
            if c.startswith("h_ev_") or c.startswith("h_cls_"):
                kind_ = "ev" if c.startswith("h_ev_") else "cls"
                v[f"h.{kind_}.{c.split('_', 2)[2]}.share_of_repos_with_hooks"] = ("share", np.where(ha, df[c].fill_null(False).cast(pl.Float64).to_numpy(), np.nan))
                v[f"h.{kind_}.{c.split('_', 2)[2]}.share_of_repos"] = ("share", df[c].fill_null(False).cast(pl.Float64).to_numpy())
        for c in df.columns:
            if c.startswith("c_"):
                ok_ = df[c].is_not_null().to_numpy()
                v[f"co.{c[2:]}"] = ("share", np.where(ok_, df[c].fill_null(False).cast(pl.Float64).to_numpy(), np.nan))
        cc = [c for c in df.columns if c.startswith("c_") and c != "c_claude_dir"]
        if cc:
            anyo = np.maximum.reduce([df[c].fill_null(False).cast(pl.Float64).to_numpy() for c in cc])
            v["co.any_other_tool"] = ("share", anyo)
    if "age_days" in df.columns:
        ag = f("age_days")
        for lo_, hi_, nm_ in AGE_BANDS:
            v[f"d3.age.{nm_}"] = ("share", np.where(np.isnan(ag), np.nan, ((ag >= lo_) & (ag < hi_)).astype(float)))
    ns, nss = f("n_skills"), f("n_skills_scripts")
    v["skills.share_of_repos_with_skill_files"] = ("share", (ns > 0).astype(float))
    v["skills.n_per_repo.median_among_with_skills"] = ("median", np.where(ns > 0, ns, np.nan))
    v["skills.n_per_repo.p90_among_with_skills"] = ("p90", np.where(ns > 0, ns, np.nan))
    v["skills.share_with_scripts"] = ("ratio", np.where((ns > 0)[:, None], np.column_stack([nss, ns]), np.nan))
    return v


def _entry(x, w, kind, rng, B, scope, ft, desc, cap) -> dict:
    e = {**estimate(x, w, kind, rng, B), "kind": kind, "scope": scope, "file": ft, "desc": desc, "status": "ok"}
    e["low_n"] = e["n"] < LOW_N
    e["sensitivity"] = sensitivity(x, w, kind, e, cap)
    return e


def build(tag: str | None, B: int | None = None) -> dict:
    cfg = config.load()
    B = B or cfg["stats"]["bootstrap"]
    rng = np.random.default_rng(cfg["snapshot"]["seed"])
    _require_strata(tag)
    t = _tables(tag)
    nums: dict = {}
    frames = {}
    for ft, name in FILES.items():
        df = load_file(tag, name, t)
        if df.height:
            frames[ft] = df
    frames["repo"] = load_repo(tag, t)
    wmeta, nested = {}, {}
    for ft, df in frames.items():
        scopes = {"established": df.filter(pl.col("stratum") == "established"), "long_tail": df.filter(pl.col("stratum") == "long_tail"), "overall": df}
        thr = None
        if ft != "repo" and "has_test" in df.columns:  # scorecard check 6: fixed budget of always-loaded tokens (config post.scorecard_token_budget; our choice)
            ok = (df["structure"] != "nested-only").to_numpy()
            thr = float(cfg["post"]["scorecard_token_budget"])
            nums[f"{ft}.overall.sc.threshold_tokens"] = {"value": thr, "n": int(ok.sum()), "kind": "median", "scope": "overall", "file": ft, "desc": "scorecard budget (fixed, our choice)", "status": "ok", "low_n": False}
            scopes["stars1k"] = df.filter((pl.col("stratum") == "established") & (pl.col("stars") >= 1000))
        thr_l = None
        if ft != "repo" and "n_nonempty_lines" in df.columns:
            thr_l = wquant(df["n_nonempty_lines"].to_numpy().astype(float), df["w"].to_numpy(), 0.5)
            nums[f"{ft}.overall.x1.threshold_lines"] = {"value": float(thr_l), "n": df.height, "kind": "median", "scope": "overall", "file": ft, "desc": "median non-empty lines (X1 long-file cut)", "status": "ok", "low_n": False}
        if "age_days" in df.columns:  # D3: age bands (subsets of overall; a limited set of metrics)
            for lo_, hi_, nm_ in AGE_BANDS:
                sub = df.filter((pl.col("age_days") >= lo_) & (pl.col("age_days") < hi_))
                if sub.height:
                    scopes[f"age_{nm_}"] = sub
        vec = {s: (file_vectors(sub, FILES[ft], t["commands"], thr, thr_l) if ft != "repo" else repo_vectors(sub)) for s, sub in scopes.items() if sub.height}
        for sc_ in [k for k in vec if k.startswith("age_")]:
            vec[sc_] = {k: x for k, x in vec[sc_].items() if k.startswith(AGE_KEEP)}
        for s, sub in scopes.items():
            if not sub.height:
                continue
            w = sub["w"].to_numpy()
            cap = float(np.quantile(w, TRIM_Q))
            wmeta[f"{ft}.{s}"] = {"n": sub.height, "w_min": float(w.min()), "w_median": float(np.median(w)), "w_p99": cap, "w_max": float(w.max()),
                                  "n_trimmed": int((w > cap).sum()), "kish_n_eff": float(w.sum() ** 2 / (w ** 2).sum())}
            for key, (kind, x) in vec[s].items():
                nums[f"{ft}.{s}.{key}"] = _entry(x, w, kind, rng, B, s, ft, key, cap)
            if ft != "repo":
                nested[f"{ft}.{s}"] = int((sub["structure"] == "nested-only").sum())
        if "established" in vec and "long_tail" in vec:  # the comparison is itself a finding
            e_w, l_w = scopes["established"]["w"].to_numpy(), scopes["long_tail"]["w"].to_numpy()
            for key, (kind, xe) in vec["established"].items():
                if key not in vec["long_tail"]:
                    continue
                xl = vec["long_tail"][key][1]
                d = estimate_diff(xe, e_w, xl, l_w, kind, rng, B)
                cap_e, cap_l = np.quantile(e_w, TRIM_Q), np.quantile(l_w, TRIM_Q)
                xe_c, we_c = _clean(xe, e_w)
                xl_c, wl_c = _clean(xl, l_w)
                if d["value"] is not None:
                    tv = _stat(kind, xl_c, np.minimum(wl_c, cap_l)) - _stat(kind, xe_c, np.minimum(we_c, cap_e))
                    d["sensitivity"] = {"trimmed_value": tv, "delta": tv - d["value"], "sensitive": bool(abs(tv - d["value"]) > (d["ci_hi"] - d["ci_lo"]) / 2)}
                d["low_n"] = min(d["n"]) < LOW_N
                nums[f"{ft}.diff.{key}"] = {**d, "kind": kind, "scope": "long_tail minus established", "file": ft, "desc": key, "status": "ok"}
    for ft, df in frames.items():
        if "lang" in df.columns:
            nums[f"{ft}.overall.d4.tamil_files_unweighted"] = {"value": float((df["lang"] == "tamil").sum()), "n": df.height, "kind": "count", "scope": "overall", "file": ft, "desc": "files detected as Tamil (unweighted count)", "status": "ok", "low_n": False}
    bl = [json.loads(x) for x in open(ROOT / "data/raw/buckets.jsonl")]
    for nm, k in (("AGENTS.md", "agents"), ("CLAUDE.md", "claude")):
        tot = sum(b["total"] for b in bl if b["name"] == nm)
        nums[f"enum.files.{k}"] = {"value": float(tot), "n": int(tot), "kind": "count", "scope": "enumeration", "file": k, "desc": "files found by GitHub code search (size buckets, before filters)", "status": "ok", "low_n": False}
    sens = [v for v in nums.values() if v.get("sensitivity")]
    meta = {"n_by_cell": {k: v["n"] for k, v in wmeta.items()}, "weights": wmeta, "n_nested_only": nested,
            "low_n_cells": sum(1 for v in nums.values() if v.get("low_n")), "cells": len(nums),
            "sensitivity_cells": len(sens), "sensitive_cells": sum(1 for v in sens if v["sensitivity"].get("sensitive")),
            "trimmed_changed_cells": sum(1 for v in sens if v["sensitivity"].get("n_trimmed", 0) > 0),
            "max_abs_trim_delta": max((abs(v["sensitivity"]["delta"]) for v in sens), default=0.0)}
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "checkpoint": tag or "full", "bootstrap": B, "definitions": DEFINITIONS,
           "strata": strata.table(tag), "meta": meta, "numbers": nums}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))
    return out


# ---------- raw figures ----------

def figures(tag: str | None) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    FIG.mkdir(parents=True, exist_ok=True)
    t = _tables(tag)
    cols = [("always_loaded_tokens", True), ("tokens", True), ("lines", True), ("headings", True), ("code_blocks", True),
            ("shout_per_1k", False), ("negation_share", False), ("imperative_share", False)]
    col = {"established": "#1f6feb", "long_tail": "#d4731f"}
    for ft, name in FILES.items():
        df = load_file(tag, name, t)
        if df.is_empty():
            continue
        fig, axs = plt.subplots(2, 4, figsize=(15, 6))
        for ax, (c, log) in zip(axs.ravel(), cols):
            vals = {s: df.filter(pl.col("stratum") == s)[c].drop_nulls().to_numpy().astype(float) for s in col}
            allv = np.concatenate([v for v in vals.values() if len(v)])
            tr = (lambda v: np.log10(v + 1)) if log else (lambda v: v)
            edges = np.histogram_bin_edges(tr(allv), bins=30)
            for s, v in vals.items():
                if len(v):
                    ax.hist(tr(v), bins=edges, density=True, alpha=.55, color=col[s], label=f"{s.replace('_', ' ')} (n={len(v)})")
            ax.set_title(c + (", log10(x+1)" if log else ""), fontsize=9)
        axs[0, 0].legend(fontsize=7)
        fig.suptitle(f"RAW distributions, {name}, non-dev repos, unweighted, density per stratum (checkpoint {tag or 'full'})", fontsize=11)
        fig.tight_layout(); fig.savefig(FIG / f"a1_a3_distributions_{ft}.png", dpi=130); plt.close(fig)
    rp = load_repo(tag, t)
    flags = ["skills", "commands", "agents", "settings_json", "cursor_rules", "copilot_instructions", "mcp_json"]
    fig, ax = plt.subplots(figsize=(8, 4))
    for i, s in enumerate(col):
        sub = rp.filter(pl.col("stratum") == s)
        ax.barh(np.arange(len(flags)) + i * 0.38, [float(sub[c].cast(pl.Float64).mean()) for c in flags], height=.36, color=col[s], label=f"{s.replace('_', ' ')} (n={sub.height})")
    ax.set_yticks(np.arange(len(flags)) + .19); ax.set_yticklabels(flags); ax.legend(fontsize=8)
    ax.set_title("A7 presence, unweighted share of repos (raw)", fontsize=10)
    fig.tight_layout(); fig.savefig(FIG / "a7_presence.png", dpi=130); plt.close(fig)
    fig, ax = plt.subplots(figsize=(6, 3.5))
    for i, s in enumerate(col):
        sub = rp.filter(pl.col("stratum") == s)
        cnt = sub.group_by("stack_size").agg(pl.len()).sort("stack_size")
        ax.bar(cnt["stack_size"].to_numpy() + i * 0.38, cnt["len"].to_numpy() / sub.height, width=.36, color=col[s], label=s.replace("_", " "))
    ax.set_xlabel("instruction files per repo (CLAUDE.md, AGENTS.md, Cursor, Copilot, GEMINI.md, .windsurfrules)"); ax.legend(fontsize=8)
    ax.set_title("Stack size, unweighted share of repos (raw)", fontsize=10)
    fig.tight_layout(); fig.savefig(FIG / "stack_size.png", dpi=130); plt.close(fig)
    return [str(p.relative_to(ROOT)) for p in sorted(FIG.glob("*.png"))]


# ---------- terminal summary ----------

HEADLINE = {
    "claude": ["a1.always_loaded_tokens.median", "a1.always_loaded_tokens.p90", "a1.lines.median", "a1.structure.single", "a1.structure.nested_only",
               "a3.shout_per_1k.median", "a3.share_with_any_shouting", "a3.imperative_share.median", "a8.tree_dump.share_files",
               "a8.tree_dump.token_share", "a8.overview.share_files", "a8.overview.token_share", "a5.share_in_clusters_of_5_plus", "cmd.any"],
    "repo": ["a7.skills", "a7.settings_json", "a7.mcp_json", "a7.cursor_rules", "stack.multi_file", "stack.size.mean", "stack.bridge.share_of_repos",
             "stack.reverse_symlink_agents_to_claude", "skills.n_per_repo.median_among_with_skills", "skills.share_with_scripts"],
}
HEADLINE["agents"] = HEADLINE["claude"]


def summary(out: dict) -> list[str]:
    m = out["meta"]
    st = out.get("strata") or {}
    lines = [f"report: checkpoint {out['checkpoint']}; bootstrap {out['bootstrap']}; cells {m['cells']} ({m['low_n_cells']} with n<{LOW_N}); "
             f"trim p99: changed {m['trimmed_changed_cells']} cells, sensitive {m['sensitive_cells']}/{m['sensitivity_cells']}, max |delta| {m['max_abs_trim_delta']:.3g}"]
    if st:
        lines.append(f"strata N_h (non-dev): established {st['established']['N_nondev']:,}, long tail {st['long_tail']['N_nondev']:,} (n={st['long_tail'].get('n_sampled', 0):,})")
    nums = out["numbers"]
    f = lambda v, kind: "-" if v is None else (f"{v:.0%}" if kind == "share" else f"{v:,.3g}")
    for ft in ("claude", "agents", "repo"):
        n_all = (m["n_by_cell"].get(f"{ft}.established", 0), m["n_by_cell"].get(f"{ft}.long_tail", 0))
        if not any(n_all):
            continue
        lines.append(f"[{ft}] repos: established {n_all[0]:,}, long tail {n_all[1]:,}")
        lines.append(f" {'metric':<46}{'estab.':>10}{'tail':>10}{'overall':>10}")
        for k in HEADLINE[ft]:
            row = [nums.get(f"{ft}.{s}.{k}") for s in ("established", "long_tail", "overall")]
            if not any(row):
                continue
            kind = next(r["kind"] for r in row if r)
            low = "*" if any(r and r.get("low_n") for r in row) else " "
            fk = "share" if kind in ("share", "ratio") else kind
            lines.append(f"{low}{k:<46}" + "".join(f"{f((r or {}).get('value'), fk):>10}" for r in row))
    lines.append("(* = a cell with n < 100)")
    return lines[:60]
