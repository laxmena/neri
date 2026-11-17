"""Fetch each explore repo's root README* at the pinned sha (raw.githubusercontent.com only; no API quota). Resumable via the blob cache.
Writes data/raw/readmes.jsonl: {owner, repo, path, blob_sha, status}."""
from __future__ import annotations

import gzip
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, devset, strata
from .fetch import Fetcher
ROOT = config.ROOT

OUT = ROOT / "data/raw/readmes.jsonl"
RE = re.compile(r"^readme(\.(md|markdown|rst|txt|mdx))?$", re.I)
MAX = 300_000


def pick(tree: list[dict]) -> dict | None:
    c = [t for t in tree if t["t"] == "blob" and "/" not in t["p"] and RE.match(t["p"]) and t["m"] != "120000" and t["s"] <= MAX]
    c.sort(key=lambda t: (not t["p"].lower().endswith(".md"), t["p"].lower() != "readme.md", t["p"]))
    return c[0] if c else None


def run(workers: int = 12, log=print) -> None:
    meta = devset.nondev(strata.load_explore(None)).select("owner", "repo")
    keep = {(o, r) for o, r in meta.iter_rows()}
    idx = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        if (j["owner"], j["repo"]) in keep and j["status"] in ("ok", "partial"):
            idx[(j["owner"], j["repo"])] = j["sha"]
    done = set()
    if OUT.exists():
        done = {(j["owner"], j["repo"]) for j in map(json.loads, open(OUT))}
    todo = [(o, r, s) for (o, r), s in idx.items() if (o, r) not in done]
    log(f"readmes: {len(idx)} repos, {len(todo)} to do")
    fx = Fetcher()
    t0, n = time.time(), 0

    def one(a):
        o, r, sha = a
        p = ROOT / "data/trees" / f"{o}__{r}__{sha[:12]}.json.gz"
        try:
            tree = json.loads(gzip.decompress(p.read_bytes()))["tree"]
        except Exception:
            return {"owner": o, "repo": r, "path": None, "blob_sha": None, "status": "no_tree"}
        t = pick(tree)
        if t is None:
            return {"owner": o, "repo": r, "path": None, "blob_sha": None, "status": "none"}
        txt, err = fx.blob(o, r, sha, t["p"], t["h"])
        return {"owner": o, "repo": r, "path": t["p"], "blob_sha": t["h"], "status": "ok" if txt is not None else (err or "err")}

    with ThreadPoolExecutor(workers) as ex, open(OUT, "a") as f:
        for rec in ex.map(one, todo, chunksize=1):
            f.write(json.dumps(rec) + "\n")
            n += 1
            if n % 1000 == 0:
                f.flush()
                log(f"readmes {n}/{len(todo)} ({n / (time.time() - t0) * 3600:,.0f}/h)")
    log("readmes done")


SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
OUT_S = ROOT / "data/raw/settings.jsonl"


def run_settings(workers: int = 12, log=print) -> None:
    """D1: fetch committed .claude/settings(.local).json at the pinned sha (raw only; resumable)."""
    meta = devset.nondev(strata.load_explore(None)).select("owner", "repo")
    keep = {(o, r) for o, r in meta.iter_rows()}
    idx = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        if (j["owner"], j["repo"]) in keep and j["status"] in ("ok", "partial"):
            idx[(j["owner"], j["repo"])] = j["sha"]
    done = set()
    if OUT_S.exists():
        done = {(j["owner"], j["repo"]) for j in map(json.loads, open(OUT_S))}
    todo = []
    for (o, r), sha in idx.items():
        if (o, r) in done:
            continue
        p = ROOT / "data/trees" / f"{o}__{r}__{sha[:12]}.json.gz"
        try:
            tree = json.loads(gzip.decompress(p.read_bytes()))["tree"]
        except Exception:
            continue
        hit = [t for t in tree if t["t"] == "blob" and t["p"] in SETTINGS and t["m"] != "120000" and (t["s"] or 0) <= MAX]
        if hit:
            todo.append((o, r, sha, hit))
    log(f"settings: {len(todo)} repos with a committed settings file")
    fx = Fetcher()

    def one(a):
        o, r, sha, hit = a
        out = []
        for t in hit:
            txt, err = fx.blob(o, r, sha, t["p"], t["h"])
            out.append({"path": t["p"], "blob_sha": t["h"], "status": "ok" if txt is not None else (err or "err")})
        return {"owner": o, "repo": r, "files": out}

    n = 0
    with ThreadPoolExecutor(workers) as ex, open(OUT_S, "a") as f:
        for rec in ex.map(one, todo, chunksize=1):
            f.write(json.dumps(rec) + "\n")
            n += 1
            if n % 500 == 0:
                f.flush(); log(f"settings {n}/{len(todo)}")
    log("settings done")
