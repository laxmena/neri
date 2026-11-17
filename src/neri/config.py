"""Config + paths. All thresholds come from config.toml."""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIRS = [
    "data/raw", "data/sample", "data/blobs", "data/trees", "data/features",
    "data/log", "data/ref", "data/public", "reports", "figures/explore", "figures/post",
]


@lru_cache
def load() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def ensure_dirs() -> list[str]:
    created = []
    for d in DATA_DIRS:
        p = ROOT / d
        if not p.exists():
            p.mkdir(parents=True)
            created.append(d)
    return created
