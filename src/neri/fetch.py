"""M3: fetch (explore sample only). Trees via the API (cached gz), contents via raw.githubusercontent.com at pinned shas.

Per repo: recursive tree at the pinned default-branch sha (from M2), root + nested agent files, recursive @imports,
root manifests, lockfile presence, .gitignore. Blobs are content-addressed by git blob sha under data/blobs/.
Resumable: repos already in data/raw/fetch_index.jsonl are skipped; cached trees/blobs are never refetched.
Token is never logged. Never run on confirm repos: input is explore.parquet only.
"""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import polars as pl

from . import config, gh, strata

ROOT = config.ROOT
INDEX = ROOT / "data/raw/fetch_index.jsonl"
TREES = ROOT / "data/trees"
BLOBS = ROOT / "data/blobs"
FAILS = ROOT / "data/log/fetch_failures.parquet"
REQLOG = ROOT / "data/log/requests.jsonl"
AGENT = ("CLAUDE.md", "AGENTS.md")
MANIFESTS = {"package.json", "Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", "pyproject.toml", ".gitignore"}
LOCKS = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "uv.lock", "poetry.lock",
         "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"}
IMPORT_RE = re.compile(r"(?<![\w/@.])@([\w][\w./+-]*\.\w+|[\w][\w.+-]*/[\w./+-]+)")
MAX_NESTED, MAX_IMPORT_DEPTH, MAX_IMPORTS = 40, 5, 30


class Gate:
    """Minimum interval between calls, shared across threads; also a shared back-off pause."""
    def __init__(self, per_sec: float):
        self.gap, self.next, self.lock = 1.0 / per_sec, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)

    def pause(self, secs: float):
        with self.lock:
            self.next = max(self.next, time.monotonic() + secs)


def _log(kind, key, status):
    with open(REQLOG, "a") as f:
        f.write(json.dumps({"ts": round(time.time(), 1), "stage": "fetch", "kind": kind, "key": key, "status": status}) + "\n")


class Fetcher:
    def __init__(self):
        cfg = config.load()["fetch"]
        self.api = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {gh.token()}",
                                                     "Accept": "application/vnd.github+json"})
        self.raw = httpx.Client(timeout=60, limits=httpx.Limits(max_connections=cfg["workers"]))
        self.api_gate, self.raw_gate = Gate(cfg["api_per_sec"]), Gate(cfg["raw_per_sec"])

    def tree(self, o: str, r: str, sha: str):
        """Returns (entries|None, truncated, error|None)."""
        p = TREES / f"{o}__{r}__{sha[:12]}.json.gz"
        if p.exists():
            d = json.loads(gzip.decompress(p.read_bytes()))
            return d["tree"], d["truncated"], None
        for attempt in range(6):
            self.api_gate.wait()
            try:
                resp = self.api.get(f"https://api.github.com/repos/{o}/{r}/git/trees/{sha}", params={"recursive": 1})
            except httpx.HTTPError as e:
                time.sleep(2 ** attempt); err = type(e).__name__; continue
            _log("tree", f"{o}/{r}", resp.status_code)
            if resp.status_code == 200:
                j = resp.json()
                tree = [{"p": t["path"], "m": t["mode"], "t": t["type"], "s": t.get("size"), "h": t["sha"]} for t in j["tree"]]
                p.write_bytes(gzip.compress(json.dumps({"tree": tree, "truncated": j.get("truncated", False)}).encode()))
                return tree, j.get("truncated", False), None
            if resp.status_code in (403, 429):
                ra = resp.headers.get("retry-after")
                reset = resp.headers.get("x-ratelimit-reset")
                delay = float(ra) if ra else (max(1.0, float(reset) - time.time()) if resp.headers.get("x-ratelimit-remaining") == "0" else 60.0)
                self.api_gate.pause(delay + 1); err = "rate_limited"; continue
            if resp.status_code in (404, 409):
                return None, False, f"tree_http_{resp.status_code}"
            time.sleep(2 ** attempt); err = f"tree_http_{resp.status_code}"
        return None, False, err

    def dir_tree(self, o: str, r: str, tree_sha: str):
        """Non-recursive listing of ONE directory (tree or commit sha). Cached by sha. Returns entries or None on failure."""
        p = TREES / "dirs" / f"{tree_sha}.json.gz"
        if p.exists():
            return json.loads(gzip.decompress(p.read_bytes()))
        for attempt in range(6):
            self.api_gate.wait()
            try:
                resp = self.api.get(f"https://api.github.com/repos/{o}/{r}/git/trees/{tree_sha}")
            except httpx.HTTPError:
                time.sleep(2 ** attempt); continue
            _log("dir_tree", f"{o}/{r}", resp.status_code)
            if resp.status_code == 200:
                ents = [{"n": e["path"], "t": e["type"], "m": e["mode"], "h": e["sha"]} for e in resp.json()["tree"]]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(gzip.compress(json.dumps(ents).encode()))
                return ents
            if resp.status_code in (403, 429):
                ra = resp.headers.get("retry-after")
                reset = resp.headers.get("x-ratelimit-reset")
                delay = float(ra) if ra else (max(1.0, float(reset) - time.time()) if resp.headers.get("x-ratelimit-remaining") == "0" else 60.0)
                self.api_gate.pause(delay + 1); continue
            if resp.status_code in (404, 409):
                return None
            time.sleep(2 ** attempt)
        return None

    def blob(self, o: str, r: str, sha: str, path: str, blob_sha: str):
        """Returns (text|None, error|None); cached by git blob sha."""
        p = BLOBS / blob_sha[:2] / blob_sha
        if p.exists():
            return p.read_text(errors="replace"), None
        err = "unknown"
        for attempt in range(5):
            self.raw_gate.wait()
            try:
                resp = self.raw.get(f"https://raw.githubusercontent.com/{o}/{r}/{sha}/{quote(path)}")
            except httpx.HTTPError as e:
                time.sleep(2 ** attempt); err = type(e).__name__; continue
            _log("raw", f"{o}/{r}/{path}", resp.status_code)
            if resp.status_code == 200:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(resp.content)
                return resp.content.decode(errors="replace"), None
            if resp.status_code in (403, 429):
                self.raw_gate.pause(float(resp.headers.get("retry-after", 30)) + 1); err = "rate_limited"; continue
            if resp.status_code == 404:
                return None, "blob_404"
            time.sleep(2 ** attempt); err = f"blob_http_{resp.status_code}"
        return None, err


def choose_primary(nested_ok: list[dict]) -> dict:
    """Primary file of a NESTED-ONLY repo: the SHALLOWEST in-range nested file named CLAUDE.md (the name the stratum was
    sampled through; any agent file if none), ties broken by the LARGEST size, then by path."""
    pref = [f for f in nested_ok if f["name"] == "CLAUDE.md"] or nested_ok
    return min(pref, key=lambda f: (f["path"].count("/"), -(f["size"] or 0), f["path"]))


def _imports(text: str) -> list[str]:
    out, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if fence:
            continue
        line = re.sub(r"`[^`]*`", "", line)  # imports are not evaluated inside code spans
        out += IMPORT_RE.findall(line)
    return out


def process(fx: Fetcher, o: str, r: str, sha: str, lo: int, hi: int) -> tuple[dict, list[dict]]:
    fails, rec = [], {"owner": o, "repo": r, "sha": sha}
    tree, trunc, err = fx.tree(o, r, sha)
    if tree is None:
        return {**rec, "status": "failed"}, [{"owner": o, "repo": r, "stage": "tree", "reason": err}]
    by_path = {t["p"]: t for t in tree if t["t"] == "blob"}
    files, nested = [], []
    for path, t in by_path.items():
        base = PurePosixPath(path).name
        if base in AGENT:
            f = {"path": path, "root": "/" not in path, "name": base, "blob_sha": t["h"], "size": t["s"], "symlink": t["m"] == "120000"}
            (files if f["root"] else nested).append(f)
    nested_seen = len(nested)
    nested.sort(key=lambda f: (f["path"].count("/"), f["path"]))  # keep the SHALLOWEST nested files when capping
    files += nested[:MAX_NESTED]
    names = {PurePosixPath(p).name for p in by_path if "/" not in p}
    rec.update(tree_truncated=trunc, n_tree=len(tree), nested_total=nested_seen,
               locks=sorted(n for n in names if n in LOCKS),
               dirs=sorted({d for d in (".claude/skills", ".claude/commands", ".claude/agents", ".cursor/rules", ".github")
                            if any(p.startswith(d + "/") for p in by_path)}),
               files_present=sorted(n for n in (".claude/settings.json", ".mcp.json", ".github/copilot-instructions.md",
                                                ".cursorrules", "CLAUDE.local.md") if n in by_path))
    root_ok = [f for f in files if f["root"] and (f["symlink"] or lo <= (f["size"] or 0) <= hi)]
    if not root_ok:
        # NESTED-ONLY repo: no usable root agent file. Primary = the SHALLOWEST in-range nested file named CLAUDE.md (the name
        # the stratum was sampled through; any agent file if none), ties broken by LARGEST size, then path.
        nested_ok = [f for f in files if not f["root"] and not f["symlink"] and lo <= (f["size"] or 0) <= hi]
        if not nested_ok:
            return {**rec, "files": files, "status": "no_agent_file_in_range"}, []
        primary = choose_primary(nested_ok)
        rec.update(nested_only=True, primary_path=primary["path"])
    # fetch agent files + imports
    queue = [(f["path"], 0) for f in files]
    done, imports = set(), []
    while queue:
        path, depth = queue.pop(0)
        if path in done:
            continue
        done.add(path)
        t = by_path[path]
        text, e = fx.blob(o, r, sha, path, t["h"])
        if text is None:
            fails.append({"owner": o, "repo": r, "stage": "agent_file" if depth == 0 else "import", "reason": e or "?"})
            continue
        for f in files:
            if f["path"] == path and f["symlink"]:
                f["symlink_target"] = text.strip()[:300]
        if depth < MAX_IMPORT_DEPTH and len(imports) < MAX_IMPORTS:
            for imp in _imports(text):
                target = str(PurePosixPath(PurePosixPath(path).parent / imp))
                target = re.sub(r"^\./", "", target)
                if target in by_path and target not in done:
                    imports.append({"from": path, "to": target, "depth": depth + 1, "blob_sha": by_path[target]["h"]})
                    queue.append((target, depth + 1))
    # manifests (root only)
    manifests = {}
    for n in MANIFESTS:
        if n in by_path:
            text, e = fx.blob(o, r, sha, n, by_path[n]["h"])
            if text is None:
                fails.append({"owner": o, "repo": r, "stage": "manifest", "reason": e or "?"})
            else:
                manifests[n] = by_path[n]["h"]
    rec.update(files=files, imports=imports, manifests=manifests,
               status="ok" if not fails else "partial")
    return rec, fails


def run(tag: str | None, limit: int | None, log=print) -> None:
    cfg = config.load()
    src = ROOT / "data/sample" / (f"c{tag}" if tag else "") / "explore.parquet"
    df = strata.load_explore(tag).sort(["owner", "repo"])  # explore only; confirm ids are never read here
    for d in (TREES, BLOBS):
        d.mkdir(parents=True, exist_ok=True)
    done = set()
    if INDEX.exists():
        with open(INDEX) as f:
            for line in f:
                j = json.loads(line)
                if j.get("status") != "no_root_file_in_range":  # old rows: re-fetch, they may be nested-only repos
                    done.add((j["owner"], j["repo"]))
    todo = [r for r in df.select("owner", "repo", "head_sha").iter_rows() if (r[0], r[1]) not in done]
    if limit:
        todo = todo[:limit]
    log(f"fetch: {len(done):,} done, {len(todo):,} to do")
    fx = Fetcher()
    lo, hi = cfg["filter"]["min_bytes"], cfg["filter"]["max_bytes"]
    n = 0
    with ThreadPoolExecutor(cfg["fetch"]["workers"]) as ex, open(INDEX, "a") as out:
        futs = {ex.submit(process, fx, o, r, sha, lo, hi): (o, r) for o, r, sha in todo}
        for fu in as_completed(futs):
            try:
                rec, fails = fu.result()
            except Exception as e:  # keep the stage alive; record and move on
                o, r = futs[fu]
                rec, fails = {"owner": o, "repo": r, "status": "failed"}, [{"owner": o, "repo": r, "stage": "exception", "reason": type(e).__name__}]
            out.write(json.dumps({**rec, "n_fail": len(fails)}) + "\n"); out.flush()
            if fails:
                _append_fails(fails)
            n += 1
            if n % 50 == 0:
                log(f"{time.strftime('%H:%M:%S')} fetched {n:,}/{len(todo):,}")
    log("fetch done")


def _append_fails(fails: list[dict]) -> None:
    new = pl.DataFrame(fails)
    if FAILS.exists():
        new = pl.concat([pl.read_parquet(FAILS), new], how="diagonal")
    new.write_parquet(FAILS)


def summary() -> list[str]:
    if not INDEX.exists():
        return ["nothing fetched yet"]
    recs = [json.loads(l) for l in open(INDEX)]
    st = pl.DataFrame({"status": [r["status"] for r in recs]}).group_by("status").agg(pl.len().alias("n")).sort("n", descending=True)
    n = len(recs)
    ok = sum(1 for r in recs if r["status"] in ("ok", "partial", "no_root_file_in_range"))
    out = [f"repos processed: {n:,}; fetch success (tree+files; excludes tree/exception failures): {ok / n:.1%}"]
    out += [f"  status {s}: {c:,}" for s, c in st.iter_rows()]
    out.append(f"root agent files (post-filter): {sum(1 for r in recs for f in r.get('files', []) if f['root']):,}; "
               f"nested: {sum(r.get('nested_total', 0) for r in recs):,}; imports resolved: {sum(len(r.get('imports', [])) for r in recs):,}")
    out.append(f"truncated trees: {sum(1 for r in recs if r.get('tree_truncated')):,}; symlinked agent files: "
               f"{sum(1 for r in recs for f in r.get('files', []) if f.get('symlink')):,}")
    if FAILS.exists():
        fl = pl.read_parquet(FAILS).group_by(["stage", "reason"]).agg(pl.len().alias("n")).sort("n", descending=True).head(10)
        out += [f"  fail {a}/{b}: {c:,}" for a, b, c in fl.iter_rows()]
    return out[:50]


# ---------- skills (explore repos with .claude/skills/*/SKILL.md) ----------

SKILLS_INDEX = ROOT / "data/raw/skills_index.jsonl"
MAX_SKILLS = 60


def _skills_for_repo(fx: Fetcher, o: str, r: str, sha: str) -> dict | None:
    from . import census
    tree, _, err = fx.tree(o, r, sha)
    if tree is None:
        return None
    if not any(t["p"].startswith(".claude/skills/") for t in tree):
        return None
    by = {t["p"]: t for t in tree}
    found = {s["name"]: s for s in census.resolve_skill_entries(tree)}
    for ln in census.symlinked_skill_dirs(tree):  # skill dir is a symlink: resolve through the link text
        if ln["name"] in found:
            continue
        text, e = fx.blob(o, r, sha, ln["link_path"], ln["link_sha"])
        if text is None:
            continue
        target = census.resolve_link(ln["link_path"], text)
        md = by.get(f"{target}/SKILL.md")
        if md and md["t"] == "blob":
            found[ln["name"]] = {"name": ln["name"], "path": md["p"], "blob_sha": md["h"], "size": md["s"], "linked": True,
                                 "dir": target}
    out = []
    for name, s in sorted(found.items())[:MAX_SKILLS]:
        d = s.get("dir") or f".claude/skills/{name}"
        under = [p for p in by if p.startswith(d + "/")]
        text, e = fx.blob(o, r, sha, s["path"], s["blob_sha"])
        out.append({**s, "dir": d, "n_files": len(under), "n_script_files": sum(1 for p in under if "/scripts/" in p),
                    "fetched": text is not None, "error": e})
    return {"owner": o, "repo": r, "sha": sha, "n_skills_total": len(found), "skills": out}


def run_skills(tag: str | None, log=print) -> None:
    src = ROOT / "data/sample" / (f"c{tag}" if tag else "") / "explore.parquet"
    df = strata.load_explore(tag).sort(["owner", "repo"])
    done = set()
    if SKILLS_INDEX.exists():
        with open(SKILLS_INDEX) as f:
            for line in f:
                j = json.loads(line)
                done.add((j["owner"], j["repo"]))
    todo = [r for r in df.select("owner", "repo", "head_sha").iter_rows() if (r[0], r[1]) not in done]
    fx = Fetcher()
    n = 0
    log(f"skills: {len(done):,} repos with skills already indexed; scanning {len(todo):,} repos")
    with ThreadPoolExecutor(config.load()["fetch"]["workers"]) as ex, open(SKILLS_INDEX, "a") as out:
        futs = {ex.submit(_skills_for_repo, fx, o, r, sha): (o, r) for o, r, sha in todo
                if (TREES / f"{o}__{r}__{sha[:12]}.json.gz").exists()}  # only repos already fetched by M3
        for fu in as_completed(futs):
            try:
                rec = fu.result()
            except Exception as e:  # keep going; record nothing for this repo
                log(f"skills error {type(e).__name__}"); continue
            if rec:
                out.write(json.dumps(rec) + "\n"); out.flush()
                n += 1
    log(f"skills: indexed {n:,} new repos with skills")



# ---------- truncated trees: resolve flagged paths with per-directory walks ----------

RESOLVED = ROOT / "data/raw/truncated_resolved.jsonl"


def walk_path(ref: str, root_sha: str, get_entries) -> bool | None:
    """Does `ref` exist? Walk it one directory at a time with non-recursive listings (get_entries(tree_sha) -> list|None).
    True = found; False = a segment is definitively absent; None = the API could not tell. A segment that is a
    symlink or submodule (not a tree) counts as found, because the path resolves on disk."""
    cur, parts = root_sha, ref.split("/")
    for i, part in enumerate(parts):
        ents = get_entries(cur)
        if ents is None:
            return None
        e = next((x for x in ents if x["n"] == part), None)
        if e is None:
            return False
        if i == len(parts) - 1 or e["t"] != "tree":
            return True
        cur = e["h"]
    return True


def resolve_truncated(tag: str | None, log=print) -> None:
    """For explore repos whose recursive tree was truncated: walk each path the partial listing calls dead.
    Output: data/raw/truncated_resolved.jsonl, one line per (repo, ref) plus a marker line per repo (idempotent)."""
    from . import classify, core
    src = ROOT / "data/sample" / (f"c{tag}" if tag else "") / "explore.parquet"
    keep = {(o, r) for o, r in strata.load_explore(tag).select("owner", "repo").iter_rows()}
    done = set()
    if RESOLVED.exists():
        for line in open(RESOLVED):
            j = json.loads(line)
            if j.get("ref") is None:
                done.add((j["owner"], j["repo"]))
    fx = Fetcher()
    cfg = config.load()
    n = 0
    for line in open(INDEX):
        rec = json.loads(line)
        k = (rec["owner"], rec["repo"])
        if k not in keep or k in done or not rec.get("tree_truncated") or rec.get("status") not in ("ok", "partial"):
            continue
        o, r, sha = k[0], k[1], rec["sha"]
        tree, _, err = fx.tree(o, r, sha)
        if tree is None:
            continue
        paths = [t["p"] for t in tree if t["t"] in ("blob", "commit")]
        gi = None
        if ".gitignore" in rec.get("manifests", {}):
            p = BLOBS / rec["manifests"][".gitignore"][:2] / rec["manifests"][".gitignore"]
            gi = p.read_text(errors="replace") if p.exists() else None
        refs = set()
        for f in rec.get("files", []):
            if f["root"] and not f.get("symlink") and cfg["filter"]["min_bytes"] <= (f["size"] or 0) <= cfg["filter"]["max_bytes"]:
                bp = BLOBS / f["blob_sha"][:2] / f["blob_sha"]
                if bp.exists():
                    refs |= {(x["ref"], x.get("full") or x["ref"]) for x in classify.classify_paths(bp.read_text(errors="replace"), paths, gi)
                             if x["category"] == "STALE"}
        out = []
        for ref, full in sorted(refs):
            out.append({"owner": o, "repo": r, "ref": ref, "exists": walk_path(full, sha, lambda s: fx.dir_tree(o, r, s))})
        out.append({"owner": o, "repo": r, "ref": None, "exists": None})  # marker: repo done
        with open(RESOLVED, "a") as w:
            for x in out:
                w.write(json.dumps(x) + "\n")
        n += 1
        log(f"resolved truncated repo {o}/{r}: {len(refs)} flagged refs walked")
    log(f"truncated: {n} repos resolved this run")



# ---------- manifests in directories that commands `cd` into ----------

SUBDIRS = ROOT / "data/raw/subdir_manifests.jsonl"
SUB_MANIFESTS = {"package.json", "Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", "pyproject.toml"}
MAX_SUBDIRS = 10


def run_subdirs(tag: str | None, log=print) -> None:
    """For each explore repo: the directories its root agent files `cd` into, and the manifests found there (fetched from raw
    at the pinned sha). Resumable: repos already in data/raw/subdir_manifests.jsonl are skipped."""
    from . import classify
    src = ROOT / "data/sample" / (f"c{tag}" if tag else "") / "explore.parquet"
    keep = {(o, r) for o, r in strata.load_explore(tag).select("owner", "repo").iter_rows()}
    done = set()
    if SUBDIRS.exists():
        for line in open(SUBDIRS):
            j = json.loads(line)
            done.add((j["owner"], j["repo"]))
    fx = Fetcher()
    cfg = config.load()
    n = 0
    with open(SUBDIRS, "a") as out:
        for line in open(INDEX):
            rec = json.loads(line)
            k = (rec["owner"], rec["repo"])
            if k not in keep or k in done or rec.get("status") not in ("ok", "partial"):
                continue
            tree, _, err = fx.tree(k[0], k[1], rec["sha"])
            if tree is None:
                continue
            by = {t["p"]: t for t in tree if t["t"] == "blob"}
            dirs: set[str] = set()
            for f in rec.get("files", []):
                if f["root"] and not f.get("symlink") and cfg["filter"]["min_bytes"] <= (f["size"] or 0) <= cfg["filter"]["max_bytes"]:
                    bp = BLOBS / f["blob_sha"][:2] / f["blob_sha"]
                    if bp.exists():
                        dirs |= classify.cd_dirs(bp.read_text(errors="replace"))
            found: dict[str, dict[str, str]] = {}
            for d in sorted(dirs)[:MAX_SUBDIRS]:
                for name in SUB_MANIFESTS:
                    t = by.get(f"{d}/{name}")
                    if t:
                        found.setdefault(d, {})[name] = t["h"]
                        fx.blob(k[0], k[1], rec["sha"], t["p"], t["h"])
            out.write(json.dumps({"owner": k[0], "repo": k[1], "dirs": found, "cd_dirs": sorted(dirs)[:MAX_SUBDIRS]}) + "\n")
            out.flush()
            n += 1
    log(f"subdirs: {n} repos scanned")
