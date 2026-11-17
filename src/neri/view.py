"""`neri view`: readable, browsable tree of the fetched files for the explore sample (LOCAL ONLY; data/ is gitignored).
data/view/<owner>__<repo>/<path> is a symlink to the content-addressed blob, so editors show real file names/extensions."""
from __future__ import annotations

import json
import re

import polars as pl

from . import config, devset, strata

ROOT = config.ROOT
VIEW = ROOT / "data/view"
BLOBS = ROOT / "data/blobs"


def safe_parts(path: str) -> list[str] | None:
    """Reject anything that could escape the repo dir (third-party paths are untrusted)."""
    parts = path.split("/")
    if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\x00" in path:
        return None
    return parts


def _slug(owner: str, repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{owner}__{repo}")


def run(tag: str | None, include_dev: bool = True, log=print) -> list[str]:
    sdir = ROOT / "data/sample" / (f"c{tag}" if tag else "")
    keep = strata.load_explore(tag).select("owner", "repo")
    keep = devset.nondev(keep, include_dev)
    keep_set = set(keep.iter_rows())
    made = skipped = missing = unsafe = repos = 0
    entries: dict[tuple, list[tuple[str, str]]] = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        k = (j["owner"], j["repo"])
        if k not in keep_set or j.get("status") not in ("ok", "partial"):
            continue
        e = entries.setdefault(k, [])
        e += [(f["path"], f["blob_sha"]) for f in j.get("files", [])]
        e += [(i["to"], i["blob_sha"]) for i in j.get("imports", [])]
        e += [(n, h) for n, h in j.get("manifests", {}).items()]
    sk = ROOT / "data/raw/skills_index.jsonl"
    if sk.exists():
        for line in open(sk):
            j = json.loads(line)
            k = (j["owner"], j["repo"])
            if k in keep_set:
                entries.setdefault(k, []).extend((f".claude/skills/{s['name']}/SKILL.md", s["blob_sha"]) for s in j["skills"] if s.get("fetched"))
    for (o, r), items in entries.items():
        repos += 1
        for path, sha in items:
            parts = safe_parts(path)
            blob = BLOBS / sha[:2] / sha
            if parts is None:
                unsafe += 1; continue
            if not blob.exists():
                missing += 1; continue
            link = VIEW / _slug(o, r) / "/".join(parts)
            if link.is_symlink() or link.exists():
                skipped += 1; continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(blob.resolve())
            made += 1
    (VIEW / "README.txt").write_text("LOCAL ONLY. Symlinks into data/blobs (third-party file text). Gitignored. Never publish.\n")
    return [f"view: {repos:,} repos; {made:,} symlinks created, {skipped:,} already present, {missing:,} blobs missing, {unsafe:,} unsafe paths skipped",
            f"browse: open {VIEW.relative_to(ROOT)}/<owner>__<repo>/ (local only, gitignored)"]
