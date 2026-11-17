"""Run the long-tail stratum after the checkpoint pipeline has finished: draw -> fetch -> truncated walks -> subdir manifests ->
skills -> features -> explorer -> view. Resumable (every stage skips cached work). Progress in data/log/longtail.log."""
from __future__ import annotations

import time

from . import config
from . import explorer as ex
from . import features as ft
from . import fetch as fe
from . import sample as sm
from . import view as vw

ROOT = config.ROOT
LOG = ROOT / "data/log/longtail.log"


def _log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def run(tag: str, poll: int = 120, wait: bool = True) -> None:
    marker = ROOT / "data/log" / f"pipeline_c{tag}.done"
    while wait and not marker.exists():
        time.sleep(poll)
    done = ROOT / "data/log" / f"longtail_c{tag}.done"
    if done.exists():
        return
    for line in sm.run_longtail(tag, log=lambda m: None if m[:2].isdigit() else _log("draw: " + m)):
        _log(line)
    fe.run(tag, None, log=lambda m: _log("fetch: " + m) if not m[:2].isdigit() else None)
    fe.resolve_truncated(tag, log=lambda m: _log("truncated: " + m))
    fe.run_subdirs(tag, log=lambda m: _log("subdirs: " + m))
    fe.run_skills(tag, log=lambda m: _log("skills: " + m))
    for line in ft.run(tag)[:4]:
        _log("features: " + line)
    _log("explorer: " + ex.write(tag))
    for line in vw.run(tag):
        _log("view: " + line)
    done.write_text("done")
    _log("long tail complete")
