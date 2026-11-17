"""Population strata. 'established' = stars >= 10 (the original sample, unchanged). 'long_tail' = stars 0-9, not a fork, not
archived, pushed within the window, size in range; a seeded simple random sample drawn from the NON-DEV explore candidates
of the checkpoint. N_h (population size) and sample_prob are stored per row so every estimate can be weighted back to the
whole population and reported per stratum."""
from __future__ import annotations

import json
import random
from datetime import date, timedelta

import polars as pl

from . import config

ROOT = config.ROOT


def sdir(tag: str | None):
    return ROOT / "data/sample" / (f"c{tag}" if tag else "")


def apply_nondev_probs(est: pl.DataFrame, dev: pl.DataFrame | None, n_nd: dict) -> pl.DataFrame:
    """Post-stratify the established sample to the NON-DEV population: within each star x type stratum the non-dev sampled repos
    stand for the non-dev population, so sample_prob = n_nondev_sampled / N_nondev and sum(1 / sample_prob) over the non-dev rows
    equals N_nondev. (The sample was drawn before the dev split; conditional on non-dev membership it is still a simple random
    sample inside the stratum.)"""
    key = pl.concat_str(["star_stratum", "type_prelim"], separator="|")
    nd = est if dev is None else est.join(dev, on=["owner", "repo"], how="anti")
    cnt = dict(nd.with_columns(_k=key).group_by("_k").agg(pl.len()).iter_rows())
    return (est.with_columns(_k=key, sample_prob_orig=pl.col("sample_prob"))
            .with_columns(sample_prob=pl.struct(["_k", "sample_prob_orig"]).map_elements(
                lambda r: min(1.0, cnt.get(r["_k"], 0) / n_nd[r["_k"]]) if n_nd.get(r["_k"]) and cnt.get(r["_k"]) else r["sample_prob_orig"],
                return_dtype=pl.Float64))
            .drop("_k"))


def load_explore(tag: str | None) -> pl.DataFrame:
    """Established sample (explore.parquet) plus the long-tail sample (longtail.parquet) when it exists, with a `stratum` column.
    When strata.json holds the non-dev established populations, established sample_prob is the post-stratified non-dev one."""
    from . import devset
    d = sdir(tag)
    est = pl.read_parquet(d / "explore.parquet").with_columns(stratum=pl.lit("established"))
    t = table(tag)
    n_nd = (t.get("established") or {}).get("N_nondev_by_stratum")
    if n_nd:
        est = apply_nondev_probs(est, devset.load(), n_nd)
    frames = [est]
    if (d / "longtail.parquet").exists():
        frames.append(pl.read_parquet(d / "longtail.parquet"))
    return pl.concat(frames, how="diagonal_relaxed")


def bucket_lookup(buckets: dict):
    """(file name, size in bytes) -> bucket id, from the enumeration's leaf buckets."""
    import bisect
    by: dict[str, tuple] = {}
    for b in buckets.values():
        by.setdefault(b["name"], []).append((b["lo"], b["hi"], b["id"]))
    for k in by:
        by[k].sort()
    los = {k: [x[0] for x in v] for k, v in by.items()}

    def look(name: str, size: int):
        v = by.get(name)
        if not v:
            return None
        i = bisect.bisect_right(los[name], size) - 1
        return v[i][2] if i >= 0 and v[i][0] <= size <= v[i][1] else None
    return look


def repo_incl_prob(files: list[dict], p_by_name: dict, lookup, extra: int = 0, lo: int = 200, hi: int = 102400) -> float:
    """P(the repo is in the candidate pool) = 1 - prod over agent files of (1 - p_name) for each DISTINCT size bucket the
    repo's files fall in (a repo with k CLAUDE.md files in k buckets had k chances). `extra` = files beyond the fetch cap
    (sizes unknown, assumed to be in distinct buckets of the CLAUDE.md name: an upper bound on k)."""
    ids: dict[str, set] = {}
    for f in files:
        size = f.get("size")
        if size is None or not lo <= size <= hi or f.get("symlink"):
            continue
        b = lookup(f["name"], size)
        if b:
            ids.setdefault(f["name"], set()).add(b)
    keep_log = 1.0
    for name, s in ids.items():
        k = len(s) + (extra if name == "CLAUDE.md" else 0)
        keep_log *= (1 - (p_by_name.get(name) or 0.0)) ** k
    if extra and "CLAUDE.md" not in ids:
        keep_log *= (1 - (p_by_name.get("CLAUDE.md") or 0.0)) ** extra
    return 1 - keep_log


def draw_longtail(pop: pl.DataFrame, n: int, seed) -> pl.DataFrame:
    """Seeded simple random sample of `n` repos from the long-tail population; adds N_h, sample_prob, stratum."""
    ids = sorted(pop.select(pl.concat_str(["owner", "repo"], separator="/")).to_series().to_list())
    rng = random.Random(f"{seed}|longtail")
    rng.shuffle(ids)
    take = set(ids[:n])
    N = len(ids)
    out = pop.with_columns(key=pl.concat_str(["owner", "repo"], separator="/")).filter(pl.col("key").is_in(list(take))).drop("key")
    return out.with_columns(stratum=pl.lit("long_tail"), N_h=pl.lit(N), sample_prob=pl.lit(min(1.0, n / N) if N else 1.0))


def cutoff() -> str:
    cfg = config.load()
    return (date.fromisoformat(cfg["snapshot"]["date"]) - timedelta(days=cfg["filter"]["pushed_within_days"])).isoformat()


def table(tag: str | None) -> dict:
    """Stratum sizes for the report: N_h (population), n (sampled), sampling fraction."""
    f = sdir(tag) / "strata.json"
    return json.loads(f.read_text()) if f.exists() else {}
