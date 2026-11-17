"""Background driver: wait until the enumeration job has stored N candidates, then freeze a checkpoint and run
M2 (sample) -> M3 (fetch) -> M4 (features) on it. Resumable: finished checkpoints (marker files) are skipped.
No LLM, explore repos only. Progress goes to data/log/pipeline.log."""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import config
from . import enumerate as en
from . import explorer as ex
from . import validate as va
from . import view as vw
from . import features as ft
from . import fetch as fe
from . import sample as sm

ROOT = config.ROOT
LOG = ROOT / "data/log/pipeline.log"


def _log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def _sample_log(m: str) -> None:
    """Keep GraphQL retry/rate-limit/failure messages (progress lines are noise)."""
    if not re.match(r"^\d\d:\d\d:\d\d graphql", m):
        _log("sample: " + m)


def _count() -> int:
    if not en.CAND.exists():
        return 0
    with open(en.CAND, "rb") as f:
        return sum(1 for _ in f)


def run(checkpoints: list[int], poll: int = 120) -> None:
    for n in checkpoints:
        marker = ROOT / "data/log" / f"pipeline_c{n}.done"
        if marker.exists():
            continue
        while _count() < n:
            time.sleep(poll)
        _log(f"checkpoint {n}: {en.checkpoint(n)}")
        for line in sm.run(str(n), log=_sample_log):
            _log("sample: " + line)
        fe.run(str(n), None, log=lambda m: None)
        fe.resolve_truncated(str(n), log=lambda m: _log("truncated: " + m))  # before features: partial trees prove nothing
        fe.run_subdirs(str(n), log=lambda m: _log("subdirs: " + m))  # manifests in `cd` directories, before features
        for line in fe.summary()[:6]:
            _log("fetch: " + line)
        for line in ft.run(str(n)):
            _log("features: " + line)
        fe.run_skills(str(n), log=lambda m: None)  # SKILL.md files, then recompute features with skills
        for line in ft.run(str(n))[:3]:
            _log("features+skills: " + line)
        _log("explorer: " + ex.write(str(n)))
        for line in vw.run(str(n)):
            _log("view: " + line)
        if n == checkpoints[-1] and not (ROOT / "reports/validation_sheet_r4.md").exists():
            # fresh, non-dev sheet; the detector is frozen at the current commit (build refuses if src/ is dirty)
            _log("validation sheet r4: " + va.build(str(n), 4))
        marker.write_text("done")
        _log(f"checkpoint {n} complete")
