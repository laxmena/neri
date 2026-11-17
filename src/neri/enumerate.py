"""M1: code-search enumeration as a seeded random sample of size buckets. Resumable at every step.

Phase A (list): recursively split size ranges using result counts only until every bucket returns < cap (1,000)
  results. Page 1 of each leaf is cached, so paging a bucket later needs one request fewer.
Phase B (page), per file name (stratum): shuffle that name's leaf buckets once with snapshot.seed|name (order saved
  in bucket_order_<name>.json) and page buckets in that order until candidate_target/len(names) files are stored. Raise the target and rerun to continue down
  the same order. Each candidate records incl_prob = buckets_paged / buckets_total within the name (equal for its buckets
  because the order is a random permutation; approximate because the stopping point depends on counts).

Limits (source: https://docs.github.com/en/rest/search/search): code search 10 req/min authenticated,
per_page <= 100, <= 1,000 results per query. Search items carry no file size; size_lo/size_hi record the bucket
and exact sizes come from trees in M3.
"""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import config, gh

ROOT = config.ROOT
RAW = ROOT / "data/raw"
STATE = RAW / "enum_state.json"          # frontier per file name + counters
BUCKETS = RAW / "buckets.jsonl"          # one line per leaf bucket: count + cached page 1 (deduped by id on load)
ORDER_FMT = "bucket_order_{}.json"       # per file name: shuffled bucket ids, written once the name is fully listed
PAGED = RAW / "paged_buckets.txt"        # ids of buckets fully paged
CAND = RAW / "candidates.jsonl"
PARQUET = RAW / "candidates.parquet"
REQLOG = ROOT / "data/log/requests.jsonl"
PROGRESS = ROOT / "data/log/enumerate_progress.json"
URL = "https://api.github.com/search/code"


class Client:
    def __init__(self, per_min: float):
        self.gap = 60.0 / per_min
        self.last = 0.0
        self.http = httpx.Client(timeout=60, headers={
            "Authorization": f"Bearer {gh.token()}", "Accept": "application/vnd.github+json"})

    def get(self, q: str, page: int) -> dict:
        per_page = config.load()["enumerate"]["per_page"]
        for attempt in range(8):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                r = self.http.get(URL, params={"q": q, "per_page": per_page, "page": page})
            except httpx.HTTPError as e:
                self._log(q, page, type(e).__name__, None)
                time.sleep(min(300, 2 ** attempt * 5) + random.random() * 3)
                continue
            self._log(q, page, r.status_code, r.headers.get("x-ratelimit-remaining"))
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                ra = r.headers.get("retry-after")
                reset = r.headers.get("x-ratelimit-reset")
                if ra:
                    delay = float(ra)
                elif reset and r.headers.get("x-ratelimit-remaining") == "0":
                    delay = max(1.0, float(reset) - time.time())
                else:
                    delay = 60.0
                time.sleep(delay + 1 + random.random() * 3)
                continue
            if r.status_code >= 500:
                time.sleep(min(300, 2 ** attempt * 5) + random.random() * 3)
                continue
            if r.status_code == 422:  # page beyond the 1,000 cap
                return {"total_count": 0, "items": [], "incomplete_results": False}
            r.raise_for_status()
        raise RuntimeError(f"search failed after retries: {q} p{page}")

    @staticmethod
    def _log(q, page, status, remaining):
        REQLOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REQLOG, "a") as f:
            f.write(json.dumps({"ts": round(time.time(), 1), "stage": "enumerate", "q": q, "page": page,
                                "status": status, "remaining": remaining}) + "\n")


# ---------- helpers ----------

def _split(lo: int, hi: int, total: int, cap: int, fill: float) -> list[tuple[int, int]]:
    """Count-guided multiway split into geometric sub-ranges; falls back to bisection."""
    m = min(max(2, math.ceil(total / (fill * cap))), hi - lo + 1)
    pts = {lo, hi + 1}
    for i in range(1, m):
        pts.add(round(math.exp(math.log(lo) + (math.log(hi + 1) - math.log(lo)) * i / m)))
    pts = sorted(p for p in pts if lo <= p <= hi + 1)
    if len(pts) < 3:
        mid = (lo + hi) // 2
        return [(lo, mid), (mid + 1, hi)]
    return [(a, b - 1) for a, b in zip(pts, pts[1:])]


def _compact(items: list[dict]) -> list[list]:
    return [[it["repository"]["full_name"], it["path"], it["sha"], bool(it["repository"].get("fork")),
             bool(it["repository"].get("private")), it["name"]] for it in items]


def _load_state(cfg: dict) -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    f = cfg["filter"]
    return {"frontier": {n: [[f["min_bytes"], f["max_bytes"]]] for n in f["file_names"]},
            "probes": 0, "pages": 0, "incomplete": 0, "listing_done": False, "started": time.time()}


def _save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def _load_buckets() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if BUCKETS.exists():
        with open(BUCKETS) as f:
            for line in f:
                b = json.loads(line)
                out[b["id"]] = b
    return out


def _progress(phase: str, st: dict, extra: dict, remaining_requests: float, gap: float) -> None:
    eta = datetime.now() + timedelta(seconds=remaining_requests * gap)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps({
        "phase": phase, "updated": datetime.now().isoformat(timespec="seconds"),
        "probes": st["probes"], "pages": st["pages"], "est_remaining_requests": int(remaining_requests),
        "eta_local": eta.isoformat(timespec="minutes"), **extra}, indent=1))


# ---------- phase A ----------

def _order_path(name: str) -> Path:
    return RAW / ORDER_FMT.format(name.replace(".", "_"))


def list_buckets(cl: Client, st: dict, cfg: dict, log, deadline: float | None, name: str) -> bool:
    e = cfg["enumerate"]
    cap, fill = e["cap"], e["split_fill"]
    n_leaf = len(_load_buckets())
    fr = st["frontier"][name]
    while fr:
        if deadline and time.monotonic() > deadline:
            return False
        lo, hi = fr[-1]
        res = cl.get(f"filename:{name} size:{lo}..{hi}", 1)
        total = res["total_count"]
        st["probes"] += 1
        st["incomplete"] += bool(res.get("incomplete_results"))
        fr.pop()
        if total >= cap and lo < hi:
            fr.extend(reversed(_split(lo, hi, total, cap, fill)))  # smallest range ends up on top
        elif total > 0:
            with open(BUCKETS, "a") as f:
                f.write(json.dumps({"id": f"{name}|{lo}|{hi}", "name": name, "lo": lo, "hi": hi,
                                    "total": total, "capped": total >= cap, "p1": _compact(res["items"])}) + "\n")
            n_leaf += 1
        _save_state(st)
        if st["probes"] % 20 == 0:
            pending = sum(len(v) for v in st["frontier"].values())
            _progress(f"A:list-buckets:{name}", st, {"leaf_buckets": n_leaf, "frontier_nodes": pending},
                      pending * 1.3 + e["candidate_target"] / e["per_page"] * 0.5, cl.gap)
            log(f"{time.strftime('%H:%M:%S')} A {name} probes={st['probes']} leaves={n_leaf} frontier={pending}")
    return True


# ---------- phase B ----------

def _fix_order(buckets: dict, cfg: dict, name: str) -> list[str]:
    path = _order_path(name)
    if path.exists():
        return json.loads(path.read_text())
    ids = sorted(i for i, b in buckets.items() if b["name"] == name)
    random.Random(f"{cfg['snapshot']['seed']}|{name}").shuffle(ids)
    path.write_text(json.dumps(ids))
    return ids


def page_buckets(cl: Client, st: dict, cfg: dict, log, deadline: float | None, name: str) -> bool:
    e = cfg["enumerate"]
    cap, per_page = e["cap"], e["per_page"]
    target = e["candidate_target"] // len(cfg["filter"]["file_names"])
    buckets = _load_buckets()
    order = _fix_order(buckets, cfg, name)
    paged = set(PAGED.read_text().split()) if PAGED.exists() else set()
    seen: set[tuple] = set()
    if CAND.exists():
        with open(CAND) as f:
            for line in f:
                d = json.loads(line)
                if d["file_name"] == name:
                    seen.add((d["owner"], d["repo"], d["path"]))
    log(f"B {name} start: {len(seen)} candidates, target {target}, buckets {len(order)}, paged {sum(1 for b in order if b in paged)}")
    for bid in order:
        if len(seen) >= target:
            break
        if bid in paged:
            continue
        if deadline and time.monotonic() > deadline:
            return False
        b = buckets[bid]
        q = f"filename:{b['name']} size:{b['lo']}..{b['hi']}"
        rows = list(b["p1"])
        for p in range(2, min(math.ceil(b["total"] / per_page), cap // per_page) + 1):
            res = cl.get(q, p)
            st["pages"] += 1
            st["incomplete"] += bool(res.get("incomplete_results"))
            rows += _compact(res["items"])
        with open(CAND, "a") as f:
            for full, path, sha, fork, private, nm in rows:
                if nm != b["name"] or fork or private:
                    continue
                owner, repo = full.split("/", 1)
                if (owner, repo, path) in seen:
                    continue
                seen.add((owner, repo, path))
                f.write(json.dumps({"owner": owner, "repo": repo, "path": path, "blob_sha": sha,
                                    "file_name": b["name"], "root": "/" not in path,
                                    "size_lo": b["lo"], "size_hi": b["hi"], "bucket_id": bid,
                                    "bucket_total": b["total"]}) + "\n")
        with open(PAGED, "a") as f:
            f.write(bid + "\n")
        paged.add(bid)
        _save_state(st)
        _progress(f"B:page-buckets:{name}", st, {"candidates_this_name": len(seen),
                                                  "buckets_paged": sum(1 for x in order if x in paged)},
                  max(0, target - len(seen)) / per_page * 0.7, cl.gap)
        log(f"{time.strftime('%H:%M:%S')} B {name} candidates={len(seen)} buckets_paged={sum(1 for x in order if x in paged)}/{len(order)}")
    return True


# ---------- entry points ----------

def run(max_minutes: float | None = None, log=print) -> None:
    cfg = config.load()
    RAW.mkdir(parents=True, exist_ok=True)
    st = _load_state(cfg)
    cl = Client(cfg["github"]["search_per_min"])
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    for name in cfg["filter"]["file_names"]:  # config order: CLAUDE.md is listed, so it is paged first
        if st["frontier"][name]:
            if not list_buckets(cl, st, cfg, log, deadline, name):
                log("time box reached during listing"); _save_state(st); return
            log(f"phase A complete for {name}")
        if not page_buckets(cl, st, cfg, log, deadline, name):
            log("time box reached during paging"); break
        finalize()
    _save_state(st)
    finalize()
    log("done (or time-boxed); run `neri enumerate --summary`")


def _probs() -> dict[str, float]:
    paged = set(PAGED.read_text().split()) if PAGED.exists() else set()
    out = {}
    for name in config.load()["filter"]["file_names"]:
        p = _order_path(name)
        if p.exists():
            order = json.loads(p.read_text())
            out[name] = sum(1 for b in order if b in paged) / len(order)
    return out


def finalize() -> None:
    import polars as pl
    if not CAND.exists():
        return
    probs = _probs()
    df = (pl.read_ndjson(CAND).unique(subset=["owner", "repo", "path"], keep="first").sort(["owner", "repo", "path"])
          .with_columns(incl_prob=pl.col("file_name").replace_strict(probs, return_dtype=pl.Float64, default=None)))
    df.write_parquet(PARQUET)


def checkpoint(n: int) -> str:
    """Freeze a prefix of WHOLE buckets in paging order: >= n/(#names with data) rows per name.
    incl_prob = buckets_in_prefix / buckets_total for that name (a valid random-prefix sample)."""
    import polars as pl
    rows: dict[str, list[dict]] = {}
    with open(CAND) as f:
        for line in f:
            d = json.loads(line)
            rows.setdefault(d["file_name"], []).append(d)
    per = n // max(1, len(rows))
    out = []
    for name, rs in rows.items():
        total_b = len(json.loads(_order_path(name).read_text()))
        keep, buckets_seen, last = [], set(), None
        for d in rs:
            if len(keep) >= per and d["bucket_id"] != last:
                break
            keep.append(d); buckets_seen.add(d["bucket_id"]); last = d["bucket_id"]
        for d in keep:
            d["incl_prob"] = len(buckets_seen) / total_b
        out += keep
    df = pl.DataFrame(out).unique(subset=["owner", "repo", "path"], keep="first")
    dst = RAW / "checkpoints"
    dst.mkdir(exist_ok=True)
    df.write_parquet(dst / f"c{n}.parquet")
    return f"checkpoint c{n}: {df.height:,} candidates; " + ", ".join(
        f"{k}: p={v:.4f}" for k, v in df.group_by('file_name').agg(pl.col('incl_prob').first()).iter_rows())


def summary() -> list[str]:
    import polars as pl
    if not STATE.exists():
        return ["no enumeration state yet"]
    st = json.loads(STATE.read_text())
    out = []
    if PROGRESS.exists():
        p = json.loads(PROGRESS.read_text())
        out.append(f"phase {p['phase']}, updated {p['updated']}, est. remaining requests {p['est_remaining_requests']}, ETA {p['eta_local']}")
    out.append(f"probes={st['probes']} pages={st['pages']} incomplete_flags={st['incomplete']}")
    b = _load_buckets()
    probs = _probs()
    for n in config.load()["filter"]["file_names"]:
        bb = [x for x in b.values() if x["name"] == n]
        out.append(f"{n}: buckets {len(bb):,}, at-cap buckets {sum(x['capped'] for x in bb)}, pool in buckets "
                   f"{sum(x['total'] for x in bb):,}, frontier left {len(st['frontier'].get(n, []))}, incl_prob {probs.get(n, 'n/a')}")
    if CAND.exists():
        finalize()
        df = pl.read_parquet(PARQUET)
        out.append(f"unique candidates: {df.height:,}; repos {df.select(pl.concat_str(['owner','repo'], separator='/').n_unique()).item():,}")
        for r in df.group_by("file_name").agg(pl.len().alias("n"), pl.col("root").sum().alias("root")).sort("file_name").iter_rows():
            out.append(f"  {r[0]}: {r[1]:,} files, {r[2]:,} at repo root")
    out.append("design est. totals: CLAUDE.md ~1.09M, AGENTS.md ~1.13M (code search de-duplicates, so lower is expected)")
    return out[:50]
