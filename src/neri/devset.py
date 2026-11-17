"""DEV set = the explore-half repos of checkpoint c5000. They were used to tune detectors and to write the fixtures,
so they are EXCLUDED from every reported number (report on c50000 minus c5000). Frozen once; never overwritten."""
from __future__ import annotations

import hashlib

import polars as pl

from . import config

ROOT = config.ROOT
DEV = ROOT / "data/sample/dev_ids.parquet"


def freeze() -> str:
    from .sample import split_bit  # lazy: sample imports this module
    if DEV.exists():
        return f"dev set already frozen: {pl.read_parquet(DEV).height:,} repos (sha256 {hashlib.sha256(DEV.read_bytes()).hexdigest()[:16]})"
    c = pl.read_parquet(ROOT / "data/raw/checkpoints/c5000.parquet")
    ids = (c.select("owner", "repo").unique()
           .filter(pl.struct(["owner", "repo"]).map_elements(lambda r: split_bit(r["owner"], r["repo"]) == 0, return_dtype=pl.Boolean))
           .sort(["owner", "repo"]))
    DEV.parent.mkdir(parents=True, exist_ok=True)
    ids.write_parquet(DEV)
    (DEV.with_suffix(".sha256")).write_text(hashlib.sha256(DEV.read_bytes()).hexdigest() + "\n")
    return f"froze dev set: {ids.height:,} explore repos from c5000 (all explore repos in the checkpoint, not only those passing filters)"


def load() -> pl.DataFrame | None:
    return pl.read_parquet(DEV) if DEV.exists() else None


def nondev(df: pl.DataFrame | None, include_dev: bool = False):
    dev = None if include_dev else load()
    if df is None or dev is None:
        return df
    return df.join(dev, on=["owner", "repo"], how="anti")
