"""Unattended chain for post 1 (docs/POST1-PLAN.md section 4). Every step is resumable and idempotent (marker files in data/log/);
progress goes to data/log/chain.log and STATUS.md is rewritten at each step. Modules are imported lazily inside each step so the
step runs whatever code is on disk when it starts.

  1 enumeration   AGENTS.md listing -> paging to the target (search quota); metadata is pre-fetched meanwhile (GraphQL quota)
  2 freeze        finalize candidates + freeze the pointer list (data/public/)
  3 sample        established + long-tail strata, non-dev, consistent N_h (both file types)
  4 fetch         trees/files (core + raw quota) and SKILL.md files. NOT run: truncated-tree walks, subdir manifests (A2 only)
  5 features      deterministic features (A2 is computed but is out of post 1 and never reported)
  6 report        numbers.json, raw figures, post figures, widget percentiles, command check sheet
  7 explorer + view
Final data = the full candidate pool (50K candidates per file name), i.e. tag None: data/raw/candidates.parquet, data/sample/,
data/features/. `neri report` with no --checkpoint reads it. c50000 stays as the CLAUDE.md-only snapshot.
"""
from __future__ import annotations

import subprocess
import time

from . import config

ROOT = config.ROOT
LOG = ROOT / "data/log/chain.log"
STEPS = ["enumeration", "freeze", "sample", "fetch", "features", "report", "explorer_view"]


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def marker(step: str):
    return ROOT / "data/log" / f"chain_{step}.done"


def write_status(state: str, step: str, detail: str, nxt: str) -> None:
    """Rewrite STATUS.md (kept under 30 lines, template preserved)."""
    done = [s for s in STEPS if marker(s).exists()]
    (ROOT / "STATUS.md").write_text(f"""# STATUS

state: {state}   <!-- RUNNING | WAITING_FOR_HUMAN | BLOCKED | DONE -->
milestone: post 1 chain (docs/POST1-PLAN.md), step: {step}
updated: {time.strftime('%Y-%m-%d %H:%M')}

## Last gate
Chain steps done: {', '.join(done) or 'none yet'}. {detail}
Verified earlier: non-dev N_h per stratum sums match exactly; nested-only is a structure type; dev set excluded; 71+ tests pass.

## Key numbers so far
Interim (CLAUDE.md snapshot c50000, non-dev): reports/numbers.json written by `neri report --checkpoint 50000`. Final numbers come from the full pool.

## Blockers / questions for human
None. Parked for post 3 (no work, no API quota): A2 wrongness, r4 + long-tail validation sheets, detector re-validation.
Parked for later posts: LLM topics, M7 history, M9 neri check.

## Next step
{nxt}
Monday: review reports/cmd_check.md (20 commands), reports/numbers.json, figures/explore + figures/post, then draft per POST1-PLAN section 6.
Check progress: tail data/log/chain.log. Resume if killed: `caffeinate -i nohup uv run neri chain >> data/log/chain.log 2>&1 &`.
""")


def enumeration_running() -> bool:
    return subprocess.run(["pgrep", "-f", "neri enumerate"], capture_output=True).returncode == 0


def enumeration_complete() -> bool:
    import json

    from . import enumerate as en
    cfg = config.load()
    paged = set(en.PAGED.read_text().split()) if en.PAGED.exists() else set()
    target = cfg["enumerate"]["candidate_target"] // len(cfg["filter"]["file_names"])
    import polars as pl
    if not en.CAND.exists():
        return False
    counts = dict(pl.read_ndjson(en.CAND).unique(subset=["owner", "repo", "path"]).group_by("file_name").agg(pl.len()).iter_rows())
    for name in cfg["filter"]["file_names"]:
        p = en._order_path(name)
        if not p.exists():
            return False
        order = json.loads(p.read_text())
        if counts.get(name, 0) < target and not all(b in paged for b in order):
            return False
    return True


def warm_metadata() -> None:
    """Pre-fetch GraphQL metadata for explore-half, non-dev candidate repos while enumeration still uses the search quota."""
    import polars as pl

    from . import devset
    from . import enumerate as en
    from . import sample as sm
    if not en.CAND.exists():
        return
    c = pl.read_ndjson(en.CAND).select("owner", "repo").unique()
    c = c.filter(pl.struct(["owner", "repo"]).map_elements(lambda r: sm.split_bit(r["owner"], r["repo"]) == 0, return_dtype=pl.Boolean))
    c = devset.nondev(c)
    sm.fetch_meta(list(c.iter_rows()), log=lambda m: None if m[:2].isdigit() else log("warm: " + m))
    log(f"warm: metadata pre-fetched for {c.height:,} explore-half repos")


def step_enumeration() -> None:
    from . import enumerate as en
    last_warm = 0.0
    while not enumeration_complete():
        if not enumeration_running():
            log("enumeration process not running and not complete: resuming it in-process (resumable)")
            write_status("RUNNING", "enumeration", "Enumeration resumed by the chain.", "Enumeration (AGENTS.md listing + paging) is running.")
            en.run(log=lambda m: None if m[:2].isdigit() else log("enum: " + m))
            continue
        if time.time() - last_warm > 1200:  # every 20 min; GraphQL quota is independent of search
            try:
                warm_metadata()
            except Exception as e:  # never let a warm-up failure stop the chain
                log(f"warm: skipped ({type(e).__name__})")
            last_warm = time.time()
        time.sleep(120)
    en.finalize()


def step_freeze() -> None:
    from . import pointers
    for line in pointers.freeze(force=True):
        log("freeze: " + line)


def step_sample() -> None:
    from . import sample as sm
    for line in sm.run(None, log=lambda m: None if m[:2].isdigit() else log("sample: " + m)):
        log("sample: " + line)
    for line in sm.run_longtail(None, log=lambda m: None if m[:2].isdigit() else log("longtail: " + m)):
        log("longtail: " + line)


def step_fetch() -> None:
    from . import fetch as fe
    fe.run(None, None, log=lambda m: None if m[:2].isdigit() else log("fetch: " + m))
    fe.run_skills(None, log=lambda m: log("skills: " + m))


def step_features() -> None:
    from . import features as ft
    for line in ft.run(None)[:6]:
        log("features: " + line)


def step_report() -> None:
    from . import report as rp
    out = rp.build(None)
    log("report: " + rp.summary(out)[0])
    for line in rp.summary(out)[1:3]:
        log("report: " + line)
    log("figures: " + ", ".join(rp.figures(None)))
    from . import postfigs, widget
    log("post figures: " + ", ".join(postfigs.make(None)))
    log("widget: " + widget.write(None))
    from . import widgethtml
    log("widget html: " + widgethtml.build())
    from . import cmdcheck, publicdata
    log("cmd check: " + cmdcheck.write(None))
    log("public data: " + publicdata.export(None))


def step_explorer_view() -> None:
    from . import explorer as ex
    from . import view as vw
    log("explorer: " + ex.write(None, full=True))
    for line in vw.run(None):
        log("view: " + line)


RUNNERS = {"enumeration": step_enumeration, "freeze": step_freeze, "sample": step_sample, "fetch": step_fetch,
           "features": step_features, "report": step_report, "explorer_view": step_explorer_view}
NEXT = {"enumeration": "AGENTS.md enumeration and paging (search quota); metadata is pre-fetched meanwhile.",
        "freeze": "Freeze the pointer list.", "sample": "Sample both strata (non-dev, consistent N_h).",
        "fetch": "Fetch trees and files for the sample (core + raw quota).", "features": "Compute features.",
        "report": "Report: numbers.json, figures, widget percentiles, cmd_check.", "explorer_view": "Explorer and view."}


def run() -> None:
    log("chain start")
    for step in STEPS:
        if marker(step).exists():
            continue
        write_status("RUNNING", step, f"Starting {step}.", NEXT[step])
        log(f"step {step}: start")
        t0 = time.time()
        for attempt in range(1, 4):  # transient failures (network, API hiccups) get 3 attempts; every step is resumable
            try:
                RUNNERS[step]()
                break
            except Exception as e:
                log(f"step {step}: attempt {attempt} FAILED {type(e).__name__}: {str(e)[:200]}")
                if attempt == 3:
                    write_status("BLOCKED", step, f"Step {step} failed 3 times: {type(e).__name__}: {str(e)[:120]}",
                                 "Fix the failure, then resume: rerun the chain (steps are resumable).")
                    raise
                time.sleep(300)
        marker(step).write_text("done")
        log(f"step {step}: done in {(time.time() - t0) / 60:.1f} min")
    write_status("WAITING_FOR_HUMAN", "complete", "All chain steps are done.", "Waiting for the human's review of the numbers and figures.")
    log("chain complete")
