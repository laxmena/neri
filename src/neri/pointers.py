"""Freeze the candidate pointer list: the reproducible starting point (everything from here on is deterministic).
Pointers only (owner, repo, path, blob sha, bucket, inclusion probability): NO file text, no features. Written to
data/public/ (not gitignored). Refuses to overwrite an existing freeze unless force=True."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time

import polars as pl

from . import config
from . import enumerate as en
from .sample import META, split_bit

ROOT = config.ROOT
PUB = ROOT / "data/public"


def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def freeze(force: bool = False) -> list[str]:
    cfg = config.load()
    dst = PUB / "FROZEN.json"
    if dst.exists() and not force:
        return [f"REFUSED: {dst.relative_to(ROOT)} exists (frozen at {json.loads(dst.read_text())['frozen_at']}); use --force to overwrite"]
    PUB.mkdir(parents=True, exist_ok=True)
    en.finalize()
    cand = pl.read_parquet(en.PARQUET).with_columns(
        split=pl.struct(["owner", "repo"]).map_elements(lambda r: split_bit(r["owner"], r["repo"]), return_dtype=pl.Int64))
    cand = cand.select("owner", "repo", "path", "blob_sha", "file_name", "root", "size_lo", "size_hi",
                       "bucket_id", "bucket_total", "incl_prob", "split").sort(["owner", "repo", "path"])
    cand.write_parquet(PUB / "candidates-frozen.parquet")
    b = en._load_buckets()
    paged = set(en.PAGED.read_text().split()) if en.PAGED.exists() else set()
    rows, complete = [], True
    for name in cfg["filter"]["file_names"]:
        p = en._order_path(name)
        order = json.loads(p.read_text()) if p.exists() else []
        complete &= bool(order)
        for i, bid in enumerate(order):
            x = b[bid]
            rows.append({"bucket_id": bid, "file_name": name, "size_lo": x["lo"], "size_hi": x["hi"], "total": x["total"],
                         "capped": x["capped"], "order_index": i, "paged": bid in paged})
    pl.DataFrame(rows).write_parquet(PUB / "buckets-frozen.parquet")
    files = {"candidates-frozen.parquet": _sha(PUB / "candidates-frozen.parquet"),
             "buckets-frozen.parquet": _sha(PUB / "buckets-frozen.parquet")}
    if META.exists():  # repo metadata snapshot (numeric/categorical only): cannot be re-queried at the same values later
        seen, recs = set(), []
        for line in open(META):
            d = json.loads(line)
            if d["key"] in seen or not d["meta"]:
                continue
            seen.add(d["key"]); m = d["meta"]
            recs.append({"repo": d["key"], "stars": m["stargazerCount"], "is_fork": m["isFork"], "archived": m["isArchived"],
                         "pushed_at": m["pushedAt"], "created_at": m["createdAt"], "license": (m["licenseInfo"] or {}).get("spdxId"),
                         "head_sha": ((m["defaultBranchRef"] or {}).get("target") or {}).get("oid")})
        pl.DataFrame(recs).write_parquet(PUB / "repo-meta-snapshot.parquet")
        files["repo-meta-snapshot.parquet"] = _sha(PUB / "repo-meta-snapshot.parquet")
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    manifest = {
        "frozen_at": time.strftime("%Y-%m-%d %H:%M:%S"), "snapshot_date": cfg["snapshot"]["date"], "seed": cfg["snapshot"]["seed"],
        "enumeration_complete": complete and all((PUB / f).exists() for f in files),
        "n_candidates": cand.height, "n_repos": cand.select(pl.concat_str(["owner", "repo"], separator="/").n_unique()).item(),
        "per_file_name": {k: v for k, v in cand.group_by("file_name").agg(pl.len()).iter_rows()},
        "filter": cfg["filter"], "enumerate": cfg["enumerate"], "code_git_head": git("rev-parse", "HEAD"),
        "python": sys.version.split()[0], "polars": pl.__version__, "sha256": files,
        "reproducible": "everything from these pointers on: split (sha256(owner/repo)%2), fetch at pinned shas (blob_sha verifies "
                        "content), features, statistics (seeded), figures",
        "not_reproducible": "the code-search enumeration itself (index drift, no deterministic order, rate limits); repo metadata "
                            "values at snapshot time (hence the snapshot file); repos deleted or force-pushed since",
        "note": "pointers only; no third-party file text is included"}
    dst.write_text(json.dumps(manifest, indent=1))
    return [f"froze {cand.height:,} candidate pointers ({manifest['n_repos']:,} repos), {len(rows):,} buckets -> data/public/",
            f"enumeration_complete={manifest['enumeration_complete']} (freeze again with --force once enumeration is finished)",
            "sha256: " + ", ".join(f"{k[:-8]} {v[:10]}" for k, v in files.items())]
