"""reports/widget_percentiles.json: weighted percentiles that feed the interactive "where does your file fall" piece.
By stratum x file type x toolchain: lines, always-loaded tokens, shouting rate; stack size by stratum x toolchain (repo level);
plus the top commands per toolchain. Non-dev, weighted like numbers.json. Cells with fewer than 30 repos are omitted."""
from __future__ import annotations

import json
import time

import numpy as np
import polars as pl

from . import config, report

ROOT = config.ROOT
OUT = ROOT / "reports/widget_percentiles.json"
PCTS = [1, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 98, 99]
MIN_N = 30


def _pcts(x: np.ndarray, w: np.ndarray) -> dict | None:
    ok = ~np.isnan(x)
    x, w = x[ok], w[ok]
    if len(x) < MIN_N:
        return None
    return {"n": int(len(x)), **{f"p{p}": report.wquant(x, w, p / 100) for p in PCTS}}


def _cells(df: pl.DataFrame, cols: list[str]) -> dict:
    out = {}
    for stratum, sub in (("established", df.filter(pl.col("stratum") == "established")), ("long_tail", df.filter(pl.col("stratum") == "long_tail")), ("overall", df)):
        by = {"all": sub}
        for tc, g in sub.group_by("toolchain"):
            by[tc[0]] = g
        out[stratum] = {}
        for tc, g in by.items():
            if g.height < MIN_N:
                continue
            w = g["w"].to_numpy()
            cell = {c: _pcts(g[c].to_numpy().astype(float), w) for c in cols}
            out[stratum][tc] = {k: v for k, v in cell.items() if v}
    return out


def write(tag: str | None) -> str:
    t = report._tables(tag)
    res = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "checkpoint": tag or "full", "definitions": report.DEFINITIONS,
           "percentiles_note": f"weighted percentiles; cells with n < {MIN_N} omitted; always_loaded_tokens excludes nested-only repos",
           "files": {}, "stack_size": {}, "top_commands": {}}
    for ft, name in report.FILES.items():
        df = report.load_file(tag, name, t)
        if df.is_empty():
            continue
        df = df.with_columns(always_loaded_tokens=pl.when(pl.col("structure") == "nested-only").then(None).otherwise(pl.col("always_loaded_tokens")))
        res["files"][ft] = _cells(df, ["lines", "always_loaded_tokens", "shout_per_1k"])
        cm = t["commands"].filter(pl.col("name") == name).group_by(["owner", "repo", "cmd"]).agg(pl.len())
        tops = {}
        for tc, g in [("all", df)] + [(k[0], v) for k, v in df.group_by("toolchain")]:
            if g.height < MIN_N:
                continue
            keys = g.select("owner", "repo", "w")
            w_all = float(g["w"].sum())
            j = cm.join(keys, on=["owner", "repo"], how="inner").group_by("cmd").agg(pl.col("w").sum().alias("ws"), pl.len().alias("n")).sort("ws", descending=True).head(10)
            tops[tc] = [{"cmd": c, "share_weighted": float(ws / w_all), "n_repos": int(n)} for c, ws, n in j.iter_rows()]
        res["top_commands"][ft] = tops
    rp = report.load_repo(tag, t)
    res["stack_size"] = _cells(rp, ["stack_size"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1))
    return f"wrote reports/widget_percentiles.json ({OUT.stat().st_size // 1024} KB; file types {list(res['files'])})"
