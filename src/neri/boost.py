"""Long-tail boost: fetch ALL eligible non-dev long-tail repos in the seeded shuffle order of `draw_longtail`, in chunks, so any
completed prefix is a valid simple random sample. No search queries; core API + raw only. `finalize` rewrites longtail.parquet
to the completed prefix and records sample_prob = n_done / N_h."""
from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import polars as pl

from . import config, fetch as fe, sample as sm

ROOT = config.ROOT

ORDER = ROOT / "data/sample/longtail_order.parquet"
LOG = ROOT / "data/log/longtail_boost.log"


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def build_order() -> pl.DataFrame:
    """Full non-dev long-tail population in the same seeded order used by draw_longtail (first 2,500 = the original sample)."""
    if ORDER.exists():
        return pl.read_parquet(ORDER)
    cfg = config.load()
    s = cfg["sample"]
    m_df, _ = sm._nondev_meta(None, lambda m: None)
    base = sm._base_filter(m_df)
    pop = (base.filter((pl.col("stars") >= s["longtail_min_stars"]) & (pl.col("stars") <= s["longtail_max_stars"]))
           .with_columns(star_stratum=pl.lit(f"{s['longtail_min_stars']}-{s['longtail_max_stars']}"),
                         key=pl.concat_str(["owner", "repo"], separator="/")))
    ids = sorted(pop["key"].to_list())
    random.Random(f"{cfg['snapshot']['seed']}|longtail").shuffle(ids)
    rank = {k: i for i, k in enumerate(ids)}
    out = pop.with_columns(rank=pl.col("key").replace_strict(rank, return_dtype=pl.Int64)).sort("rank")
    out.write_parquet(ORDER)
    return out


def _done_keys() -> set[tuple[str, str]]:
    done = set()
    if fe.INDEX.exists():
        for line in open(fe.INDEX):
            j = json.loads(line)
            if j.get("status") != "no_root_file_in_range":
                done.add((j["owner"], j["repo"]))
    return done


def _prefix_done(order: pl.DataFrame, done: set) -> int:
    n = 0
    for o, r in order.select("owner", "repo").iter_rows():
        if (o, r) not in done:
            break
        n += 1
    return n


def _eta(n_done: int, n_total: int, t0: float, n0: int, deadline: float) -> str:
    rate = (n_done - n0) / max(time.time() - t0, 1)
    left = (n_total - n_done) / rate if rate > 0 else float("inf")
    fin = time.strftime("%H:%M", time.localtime(time.time() + left)) if left != float("inf") else "?"
    return f"{n_done:,}/{n_total:,} ({rate*3600:,.0f}/h) ETA {fin}, cutoff {time.strftime('%H:%M', time.localtime(deadline))}"


def _status(msg: str) -> None:
    p = ROOT / "STATUS.md"
    t = p.read_text()
    marker = "## Long-tail boost"
    t = t.split(marker)[0].rstrip() + f"\n\n{marker}\n{msg}\n"
    p.write_text(t)


def run(cutoff: str = "17:00", chunk: int = 300, workers: int = 12) -> None:
    cfg = config.load()
    order = build_order()
    N = order.height
    hh, mm = map(int, cutoff.split(":"))
    deadline = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    lo, hi = cfg["filter"]["min_bytes"], cfg["filter"]["max_bytes"]
    done = _done_keys()
    n_prefix = _prefix_done(order, done)
    _log(f"boost start: N_h={N:,}, prefix done {n_prefix:,}, cutoff {cutoff}, workers {workers}")
    fx = fe.Fetcher()
    t0, n0, pos = time.time(), n_prefix, n_prefix
    while pos < N and time.time() < deadline:
        rows = [r for r in order[pos:pos + chunk].select("owner", "repo", "head_sha").iter_rows() if (r[0], r[1]) not in done]
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
        pos = min(pos + chunk, N)
        msg = _eta(pos, N, t0, n0, deadline)
        _log("chunk done " + msg)
        _status(f"RUNNING {msg}. Log: data/log/longtail_boost.log. Resume: `caffeinate -i nohup uv run neri longtail-boost >> data/log/longtail_boost.log 2>&1 &`")
    _log("boost loop ended: " + ("complete" if pos >= N else "cutoff reached"))


def finalize() -> list[str]:
    """longtail.parquet := completed prefix of the shuffled order; sample_prob = n_done / N_h (all rows share it)."""
    order = build_order()
    N = order.height
    n_done = _prefix_done(order, _done_keys())
    d = ROOT / "data/sample"
    smp = (order[:n_done].drop("key", "rank")
           .with_columns(stratum=pl.lit("long_tail"), N_h=pl.lit(N), sample_prob=pl.lit(n_done / N if N else 1.0)))
    smp.write_parquet(d / "longtail.parquet")
    tab = json.loads((d / "strata.json").read_text())
    tab["long_tail"].update({"N_h": N, "n_sampled": n_done, "sample_prob": n_done / N,
                             "note": f"boost: completed prefix of the seeded order, n={n_done:,} of N_h={N:,}"})
    (d / "strata.json").write_text(json.dumps(tab, indent=1))
    msg = f"long tail finalized: n={n_done:,} of N_h={N:,} (sample_prob={n_done / N:.4f})"
    _log(msg)
    return [msg]
