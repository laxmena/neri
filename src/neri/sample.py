"""M2: split, repo metadata (GraphQL), filters, stratified sample.

Order: hash split FIRST (sha256(owner/repo) % 2), so metadata is fetched for explore repos only; confirm repos are
stored as ids only. Metadata is cached in data/raw/repo_meta.jsonl (resumable, idempotent).
Repo type here is PRELIMINARY (from the candidate files seen); M3 corrects it from the tree.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import date, timedelta

import httpx
import polars as pl

from . import config, devset, gh, strata

ROOT = config.ROOT
META = ROOT / "data/raw/repo_meta.jsonl"
FIELDS = ("nameWithOwner stargazerCount isFork isArchived isPrivate pushedAt createdAt "
          "licenseInfo { spdxId } defaultBranchRef { target { oid } }")
SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


def split_bit(owner: str, repo: str) -> int:
    return int(hashlib.sha256(f"{owner}/{repo}".encode()).hexdigest(), 16) % 2  # 0 = explore, 1 = confirm


def _load_meta() -> dict:
    out = {}
    if META.exists():
        with open(META) as f:
            for line in f:
                d = json.loads(line)
                out[d["key"]] = d["meta"]
    return out


REQLOG = ROOT / "data/log/requests.jsonl"
GQL = "https://api.github.com/graphql"


def _req_log(status, ms, note=""):
    REQLOG.parent.mkdir(parents=True, exist_ok=True)  # a fresh clone has no data/log yet
    with open(REQLOG, "a") as f:
        f.write(json.dumps({"ts": round(time.time(), 1), "stage": "graphql", "status": status, "ms": ms, "note": note}) + "\n")


def _query(valid: list[tuple[str, str]]) -> str:
    return "query {" + " ".join(f'r{j}: repository(owner: "{o}", name: "{r}") {{ {FIELDS} }}'
                                for j, (o, r) in enumerate(valid)) + "}"


def _post(http: httpx.Client, query: str, log, max_attempts: int = 8):
    """POST one GraphQL query. Returns the payload dict, or None after repeated failure (caller may split the batch).
    Handles: network errors, non-200, empty/non-JSON bodies, primary and secondary rate limits (Retry-After,
    x-ratelimit-reset), GraphQL RATE_LIMITED errors. Logs status and the first 200 chars of any bad body."""
    for attempt in range(max_attempts):
        backoff = min(120, 2 ** attempt * 3) + random.random() * 3
        t0 = time.monotonic()
        try:
            resp = http.post(GQL, json={"query": query})
        except httpx.HTTPError as e:
            _req_log(type(e).__name__, int((time.monotonic() - t0) * 1000))
            log(f"graphql network error {type(e).__name__}; retry in {backoff:.0f}s")
            time.sleep(backoff); continue
        ms = int((time.monotonic() - t0) * 1000)
        body = resp.text or ""
        _req_log(resp.status_code, ms)
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                log(f"graphql 200 but non-JSON/empty body (len={len(body)}): {body[:200]!r}; retry in {backoff:.0f}s")
                time.sleep(backoff); continue
            if not isinstance(payload, dict):
                log(f"graphql 200 unexpected JSON type: {body[:200]!r}"); time.sleep(backoff); continue
            errs = payload.get("errors") or []
            limited = any((e or {}).get("type") == "RATE_LIMITED" or "rate limit" in str((e or {}).get("message", "")).lower() for e in errs) \
                or "rate limit" in str(payload.get("message", "")).lower()
            if limited:
                wait = max(60.0, float(resp.headers.get("retry-after", 0) or 0)) + random.random() * 5
                log(f"graphql rate limited (200): {body[:200]!r}; waiting {wait:.0f}s"); time.sleep(wait); continue
            if not payload.get("data"):
                log(f"graphql 200 without data: {body[:200]!r}; retry in {backoff:.0f}s"); time.sleep(backoff); continue
            return payload
        if resp.status_code in (403, 429):
            ra = resp.headers.get("retry-after")
            reset = resp.headers.get("x-ratelimit-reset")
            if ra:
                wait = float(ra) + 1  # honor Retry-After (secondary limit)
            elif resp.headers.get("x-ratelimit-remaining") == "0" and reset:
                wait = max(1.0, float(reset) - time.time()) + 1  # primary limit: wait for reset
            else:
                wait = 60.0  # secondary limit without a header: GitHub says wait at least a minute
            wait += random.random() * 5
            log(f"graphql HTTP {resp.status_code} (rate limit): {body[:200]!r}; waiting {wait:.0f}s")
            time.sleep(wait); continue
        log(f"graphql HTTP {resp.status_code}: {body[:200]!r}; retry in {backoff:.0f}s")
        time.sleep(backoff)
    return None


def _fetch_batch(http, chunk: list[tuple[str, str]], log) -> dict[tuple[str, str], dict | None]:
    """Fetch metadata for a chunk; if the batch keeps failing, split it so one bad repo cannot block the rest.
    Repos that still fail alone are omitted (not cached), so a later run retries them."""
    payload = _post(http, _query(chunk), log, max_attempts=8 if len(chunk) == 1 else 4)
    if payload is None:
        if len(chunk) == 1:
            log(f"graphql giving up on {chunk[0][0]}/{chunk[0][1]} (will retry on next run)")
            return {}
        mid = len(chunk) // 2
        log(f"graphql splitting batch of {len(chunk)}")
        return {**_fetch_batch(http, chunk[:mid], log), **_fetch_batch(http, chunk[mid:], log)}
    data = payload.get("data") or {}
    return {rc: data.get(f"r{j}") for j, rc in enumerate(chunk)}


def fetch_meta(repos: list[tuple[str, str]], log=print) -> None:
    cfg = config.load()
    batch = cfg["github"]["graphql_batch"]
    have = _load_meta()
    todo = [r for r in repos if f"{r[0]}/{r[1]}" not in have]
    log(f"metadata: {len(have):,} cached, {len(todo):,} to fetch")
    http = httpx.Client(timeout=90, headers={"Authorization": f"Bearer {gh.token()}"})
    failed = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        valid = [(o, r) for o, r in chunk if SAFE.match(o) and SAFE.match(r)]
        got = _fetch_batch(http, valid, log) if valid else {}
        with open(META, "a") as f:
            for (o, r), m in got.items():
                f.write(json.dumps({"key": f"{o}/{r}", "meta": m}) + "\n")
            for o, r in chunk:
                if (o, r) not in set(valid):
                    f.write(json.dumps({"key": f"{o}/{r}", "meta": None}) + "\n")
        failed += len(valid) - len(got)
        log(f"{time.strftime('%H:%M:%S')} graphql {min(i + batch, len(todo)):,}/{len(todo):,} (failed so far {failed})")
        time.sleep(0.5 + random.random() * 0.5)


def run(tag: str | None, log=print) -> list[str]:
    cfg = config.load()
    f, s = cfg["filter"], cfg["sample"]
    src = ROOT / ("data/raw/checkpoints/" + f"c{tag}.parquet" if tag else "data/raw/candidates.parquet")
    outdir = ROOT / "data/sample" / (f"c{tag}" if tag else "")
    outdir.mkdir(parents=True, exist_ok=True)
    cand = pl.read_parquet(src)
    repos = (cand.group_by(["owner", "repo"]).agg(
        pl.col("file_name").unique().alias("names"), pl.col("incl_prob").min().alias("incl_prob_min"))
        .with_columns(split=pl.struct(["owner", "repo"]).map_elements(
            lambda r: split_bit(r["owner"], r["repo"]), return_dtype=pl.Int64)))
    confirm = repos.filter(pl.col("split") == 1).select("owner", "repo")
    confirm.write_parquet(outdir / "confirm_ids.parquet")  # ids only, nothing else
    explore = repos.filter(pl.col("split") == 0)
    fetch_meta(list(explore.select("owner", "repo").iter_rows()), log)
    meta = _load_meta()
    rows = []
    for o, r, names, p in explore.select("owner", "repo", "names", "incl_prob_min").iter_rows():
        m = meta.get(f"{o}/{r}")
        if not m:
            continue
        rows.append({"owner": o, "repo": r, "stars": m["stargazerCount"], "is_fork": m["isFork"],
                     "archived": m["isArchived"], "pushed_at": m["pushedAt"][:10], "created_at": m["createdAt"][:10],
                     "license": (m["licenseInfo"] or {}).get("spdxId"),
                     "head_sha": ((m["defaultBranchRef"] or {}).get("target") or {}).get("oid"),
                     "names": sorted(names), "incl_prob_min": p})
    m_df = pl.DataFrame(rows)
    cutoff = (date.fromisoformat(cfg["snapshot"]["date"]) - timedelta(days=f["pushed_within_days"])).isoformat()
    funnel = [("explore repos (candidates)", explore.height), ("metadata found", m_df.height)]
    d = m_df.filter(pl.col("stars") >= f["min_stars"]); funnel.append((f"stars >= {f['min_stars']}", d.height))
    d = d.filter(~pl.col("is_fork")) if f["exclude_forks"] else d; funnel.append(("not fork", d.height))
    d = d.filter(~pl.col("archived")) if f["exclude_archived"] else d; funnel.append(("not archived", d.height))
    d = d.filter(pl.col("pushed_at") >= cutoff); funnel.append((f"pushed >= {cutoff}", d.height))
    d = d.filter(pl.col("head_sha").is_not_null()); funnel.append(("has default-branch sha", d.height))
    edges = s["star_strata"]
    def stratum(st):
        for i, (lo, hi) in enumerate(edges):
            if lo <= st <= hi:
                return f"{lo}-{hi}"
    d = d.with_columns(
        star_stratum=pl.col("stars").map_elements(stratum, return_dtype=pl.Utf8),
        type_prelim=pl.col("names").map_elements(
            lambda n: "both" if len(n) == 2 else ("CLAUDE only" if n[0] == "CLAUDE.md" else "AGENTS only"), return_dtype=pl.Utf8))
    # stratified sample: cap per stratum = target / n_strata (take all when a stratum is smaller), seeded
    strata = d.group_by(["star_stratum", "type_prelim"]).agg(pl.len().alias("N")).sort(["star_stratum", "type_prelim"])
    cap = max(1, s["target_repos"] // (2 * 9))  # explore half of the target, spread over 9 strata
    rng = random.Random(cfg["snapshot"]["seed"])
    keep = []
    for (ss, tt), grp in d.group_by(["star_stratum", "type_prelim"], maintain_order=True):
        ids = sorted(grp.select(pl.concat_str(["owner", "repo"], separator="/")).to_series().to_list())
        rng.shuffle(ids)
        keep += ids[:cap]
    keep_set = set(keep)
    d = d.with_columns(key=pl.concat_str(["owner", "repo"], separator="/")).with_columns(
        sampled=pl.col("key").is_in(list(keep_set)))
    n_h = strata.rename({"N": "N_h"})
    d = d.join(n_h, on=["star_stratum", "type_prelim"]).with_columns(
        sample_prob=pl.when(pl.col("N_h") <= cap).then(1.0).otherwise(cap / pl.col("N_h")))
    d.filter(pl.col("sampled")).drop("key", "sampled").write_parquet(outdir / "explore.parquet")
    out = ["funnel: " + "; ".join(f"{k}={v:,}" for k, v in funnel)]
    out.append(f"split: explore {explore.height:,} / confirm {confirm.height:,} "
               f"({explore.height / max(1, explore.height + confirm.height):.1%} explore)")
    out.append(f"strata (stars x prelim type): N -> sampled (cap {cap})")
    for ss, tt, n in strata.iter_rows():
        out.append(f"  {ss:>11} {tt:<11} N={n:,} sampled={min(n, cap):,}")
    lic = d.group_by("license").agg(pl.len().alias("n")).sort("n", descending=True).head(8)
    out.append("license: " + ", ".join(f"{(a or 'none')}={b:,}" for a, b in lic.iter_rows()))
    out.append(f"sampled explore repos: {len(keep_set):,} -> {outdir / 'explore.parquet'}")
    return out[:50]


def _nondev_meta(tag: str | None, log=print):
    """Non-dev explore candidate repos of a checkpoint with their cached metadata. Returns (frame, n_explore)."""
    src = ROOT / ("data/raw/checkpoints/" + f"c{tag}.parquet" if tag else "data/raw/candidates.parquet")
    cand = pl.read_parquet(src)
    repos = (cand.group_by(["owner", "repo"]).agg(pl.col("file_name").unique().alias("names"), pl.col("incl_prob").min().alias("incl_prob_min"))
             .with_columns(split=pl.struct(["owner", "repo"]).map_elements(lambda r: split_bit(r["owner"], r["repo"]), return_dtype=pl.Int64))
             .filter(pl.col("split") == 0))
    n_explore = repos.height
    repos = devset.nondev(repos)  # the dev set never enters a reported population or sample
    fetch_meta(list(repos.select("owner", "repo").iter_rows()), log)
    meta = _load_meta()
    rows = []
    for o, r, names, p in repos.select("owner", "repo", "names", "incl_prob_min").iter_rows():
        m = meta.get(f"{o}/{r}")
        if m:
            rows.append({"owner": o, "repo": r, "stars": m["stargazerCount"], "is_fork": m["isFork"], "archived": m["isArchived"],
                         "pushed_at": m["pushedAt"][:10], "created_at": m["createdAt"][:10], "license": (m["licenseInfo"] or {}).get("spdxId"),
                         "head_sha": ((m["defaultBranchRef"] or {}).get("target") or {}).get("oid"), "names": sorted(names), "incl_prob_min": p})
    return pl.DataFrame(rows), n_explore


def _base_filter(m_df: pl.DataFrame) -> pl.DataFrame:
    f = config.load()["filter"]
    d = m_df.filter((pl.col("pushed_at") >= strata.cutoff()) & pl.col("head_sha").is_not_null())
    if f["exclude_forks"]:
        d = d.filter(~pl.col("is_fork"))
    if f["exclude_archived"]:
        d = d.filter(~pl.col("archived"))
    return d.with_columns(type_prelim=pl.col("names").map_elements(
        lambda n: "both" if len(n) == 2 else ("CLAUDE only" if n[0] == "CLAUDE.md" else "AGENTS only"), return_dtype=pl.Utf8))


def _star_bucket(stars: int, edges) -> str | None:
    for lo, hi in edges:
        if lo <= stars <= hi:
            return f"{lo}-{hi}"
    return None


def build_strata(tag: str | None, log=print) -> dict:
    """Write strata.json with the NON-DEV population sizes of the established strata (star x type) and of the long tail."""
    cfg = config.load()
    m_df, n_explore = _nondev_meta(tag, log)
    base = _base_filter(m_df)
    edges = cfg["sample"]["star_strata"]
    est = (base.filter(pl.col("stars") >= cfg["filter"]["min_stars"])
           .with_columns(star_stratum=pl.col("stars").map_elements(lambda x: _star_bucket(x, edges), return_dtype=pl.Utf8)))
    nd = {f"{a}|{b}": c for a, b, c in est.group_by(["star_stratum", "type_prelim"]).agg(pl.len()).iter_rows()}
    lt = base.filter((pl.col("stars") >= cfg["sample"]["longtail_min_stars"]) & (pl.col("stars") <= cfg["sample"]["longtail_max_stars"]))
    d = strata.sdir(tag)
    d.mkdir(parents=True, exist_ok=True)
    t = strata.table(tag)
    t.update({"snapshot_date": cfg["snapshot"]["date"], "seed": cfg["snapshot"]["seed"], "pushed_cutoff": strata.cutoff(),
              "explore_candidates": n_explore, "nondev_explore_candidates": m_df.height})
    t["established"] = {**t.get("established", {}), "definition": f"stars >= {cfg['filter']['min_stars']}; star x type strata, original sample",
                        "N_nondev_by_stratum": nd, "N_nondev": sum(nd.values())}
    t["long_tail"] = {**t.get("long_tail", {}), "N_nondev": lt.height}
    (d / "strata.json").write_text(json.dumps(t, indent=1))
    return t


def run_longtail(tag: str | None, log=print) -> list[str]:
    """Draw the long-tail stratum: stars 0-9, not fork, not archived, pushed within the window, has a default-branch sha;
    seeded simple random sample of `longtail_target` repos from the NON-DEV explore candidates of this checkpoint."""
    cfg = config.load()
    s_cfg = cfg["sample"]
    t0 = build_strata(tag, log)  # N_h of every stratum, non-dev
    m_df, n_explore = _nondev_meta(tag, log)
    base = _base_filter(m_df)
    pop = (base.filter((pl.col("stars") >= s_cfg["longtail_min_stars"]) & (pl.col("stars") <= s_cfg["longtail_max_stars"]))
           .with_columns(star_stratum=pl.lit(f"{s_cfg['longtail_min_stars']}-{s_cfg['longtail_max_stars']}")))
    cut = strata.cutoff()
    repos_height = m_df.height
    smp = strata.draw_longtail(pop, s_cfg["longtail_target"], cfg["snapshot"]["seed"])
    d = strata.sdir(tag)
    d.mkdir(parents=True, exist_ok=True)
    smp.write_parquet(d / "longtail.parquet")
    est = pl.read_parquet(d / "explore.parquet")
    tab = strata.table(tag)
    tab["established"].update({"N_h_incl_dev": int(est.group_by(["star_stratum", "type_prelim"]).agg(pl.col("N_h").first())["N_h"].sum()),
                               "n_sampled": est.height,
                               "note": "N_nondev (per star x type stratum) is the population used for weights; the sample itself was drawn before the dev split"})
    tab["long_tail"].update({"definition": f"stars {s_cfg['longtail_min_stars']}-{s_cfg['longtail_max_stars']}, not fork, not archived, pushed >= {cut}, has sha; non-dev; simple random sample",
                             "N_h": pop.height, "n_sampled": smp.height, "sample_prob": float(smp["sample_prob"][0]) if smp.height else None,
                             "star_distribution": {int(k): int(v) for k, v in pop.group_by("stars").agg(pl.len()).sort("stars").iter_rows()}})
    (d / "strata.json").write_text(json.dumps(tab, indent=1))
    return [f"long tail: population N_h={pop.height:,} (of {n_explore:,} explore candidates; dev excluded {n_explore - repos_height:,}); "
            f"sampled {smp.height:,} (p={tab['long_tail']['sample_prob']:.3f}); established non-dev N_h={tab['established']['N_nondev']:,}, sampled {est.height:,}",
            f"wrote {(d / 'longtail.parquet').relative_to(ROOT)} and strata.json"]
