"""Popular stratum census: fetch ALL eligible non-dev stars>=10 repos (not only the original stratified sample), then set
explore.parquet to the full population (sample_prob -> 1.0 after post-stratification). Resumable. Core API + raw only."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import polars as pl

from . import config, fetch as fe, sample as sm, boost

ROOT = config.ROOT
BAK = ROOT / "data/sample/explore_original.parquet"


def population() -> pl.DataFrame:
    cfg = config.load()
    m_df, _ = sm._nondev_meta(None, lambda m: None)
    base = sm._base_filter(m_df).filter(pl.col("stars") >= cfg["filter"]["min_stars"])
    edges = cfg["sample"]["star_strata"]
    return base.with_columns(star_stratum=pl.col("stars").map_elements(lambda x: sm._star_bucket(x, edges), return_dtype=pl.Utf8))


def run(workers: int = 12, chunk: int = 300) -> None:
    cfg = config.load()
    pop = population()
    done = boost._done_keys()
    todo = [r for r in pop.select("owner", "repo", "head_sha").iter_rows() if (r[0], r[1]) not in done]
    boost._log(f"popfull: population {pop.height:,}, already fetched {pop.height - len(todo):,}, to fetch {len(todo):,}")
    lo, hi = cfg["filter"]["min_bytes"], cfg["filter"]["max_bytes"]
    fx = fe.Fetcher()
    t0 = time.time()
    for i in range(0, len(todo), chunk):
        rows = todo[i:i + chunk]
        with ThreadPoolExecutor(workers) as ex, open(fe.INDEX, "a") as out:
            futs = {ex.submit(fe.process, fx, o, r, sha, lo, hi): (o, r) for o, r, sha in rows}
            for fu in as_completed(futs):
                try:
                    rec, fails = fu.result()
                except Exception as e:
                    o, r = futs[fu]
                    rec, fails = {"owner": o, "repo": r, "status": "failed"}, [{"owner": o, "repo": r, "stage": "exception", "reason": type(e).__name__}]
                out.write(json.dumps({**rec, "n_fail": len(fails)}) + "\n"); out.flush()
                if fails:
                    fe._append_fails(fails)
        boost._log(f"popfull chunk {min(i + chunk, len(todo)):,}/{len(todo):,} ({(i + chunk) / max(time.time() - t0, 1) * 3600:,.0f}/h)")


def finalize() -> str:
    """explore.parquet := original rows (incl. dev rows, unchanged) + every non-dev population repo not already in it."""
    if not BAK.exists():
        BAK.write_bytes((ROOT / "data/sample/explore.parquet").read_bytes())
    orig = pl.read_parquet(BAK)
    pop = population()
    have = set(orig.select(pl.concat_str(["owner", "repo"], separator="/")).to_series().to_list())
    done = boost._done_keys()
    new = (pop.with_columns(key=pl.concat_str(["owner", "repo"], separator="/"))
           .filter(~pl.col("key").is_in(list(have)))
           .filter(pl.struct(["owner", "repo"]).map_elements(lambda r: (r["owner"], r["repo"]) in done, return_dtype=pl.Boolean)))
    n_nd = pop.group_by(["star_stratum", "type_prelim"]).agg(pl.len().alias("N_h"))
    new = new.join(n_nd, on=["star_stratum", "type_prelim"]).with_columns(sample_prob=pl.lit(1.0))
    cols = orig.columns
    out = pl.concat([orig, new.select([c for c in cols if c in new.columns])], how="diagonal_relaxed")
    out.write_parquet(ROOT / "data/sample/explore.parquet")
    p = ROOT / "data/sample/strata.json"
    t = json.loads(p.read_text())
    t["established"].update({"n_sampled": int(pop.height), "note": f"full census of non-dev stars>=10: original {orig.height:,} + {new.height:,} added; post-stratified sample_prob = 1 when every non-dev repo is fetched"})
    p.write_text(json.dumps(t, indent=1))
    return f"popular census: population {pop.height:,}; explore rows {orig.height:,} -> {out.height:,} (+{new.height:,})"
