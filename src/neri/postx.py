"""Deterministic extras for post 1 (no LLM): README overlap, CLAUDE-vs-AGENTS drift, package-manager check, scorecard inputs.
Outputs data/features/x_files.parquet (one row per primary file), x_repo.parquet (one row per repo), reports/pm_check.md,
data/log/drift_examples.md (local only, anonymized). Explore, non-dev repos only."""
from __future__ import annotations

import hashlib
import json
import random
import re

import polars as pl

from . import config, devset, strata
from .census import extract_with_lines
from .core import iter_lines
from . import xfeat

ROOT = config.ROOT
BLOBS = ROOT / "data/blobs"
TEST_RE = re.compile(r"(?:^|:)(test|tests|pytest|jest|vitest|mocha|cypress|e2e|ctest|rspec|phpunit|check)(?::|$)")
BUILD_RE = re.compile(r"(?:^|:)(build|run|dev|start|serve|compile|package|make)(?::|$)|^make:\(default\)$")
DONT_RE = re.compile(r"\b(never|don'?t|do not|must not|should not|shouldn'?t|avoid|forbidden)\b", re.I)
JS_LOCK = {"package-lock.json": "npm", "yarn.lock": "yarn", "pnpm-lock.yaml": "pnpm", "bun.lock": "bun", "bun.lockb": "bun"}
PY_LOCK = {"uv.lock": "uv", "poetry.lock": "poetry", "Pipfile.lock": "pipenv"}
JS_PM, PY_PM = {"npm", "yarn", "pnpm", "bun"}, {"pip", "uv", "poetry", "pipenv", "conda"}
_enc = None


def _tok(s: str) -> int:
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding("o200k_base")
    return len(_enc.encode(s, disallowed_special=()))


def _blob(sha: str) -> str | None:
    p = BLOBS / sha[:2] / sha
    return p.read_text(errors="replace") if p.exists() else None


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def _shingles(w: list[str]) -> set[str]:
    return {" ".join(w[i:i + 5]) for i in range(len(w) - 4)}


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[#*>`_\-+|]", " ", s.lower())).strip()


def _overlap(text: str, rd_sh: set[str]) -> tuple[int, int, int, int]:
    """(n_shingles, n_shared, tokens_total_of_overlapping_lines, n_lines_overlapping). A line overlaps when >=50% of its 5-word shingles are in the README."""
    w = _words(text)
    sh = _shingles(w)
    shared = len(sh & rd_sh)
    otok = olines = 0
    for line in text.splitlines():
        lw = _words(line)
        ls = _shingles(lw)
        if ls and len(ls & rd_sh) / len(ls) >= 0.5:
            otok += _tok(line) + 1
            olines += 1
    return len(sh), shared, otok, olines


def _lines(text: str) -> set[str]:
    return {n for n in (_norm_line(l) for l in text.splitlines()) if n}


SRC_EXT = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
           ".cs", ".rb", ".php", ".scala", ".sh", ".bash", ".lua", ".r", ".jl", ".dart", ".ex", ".exs", ".erl", ".hs", ".ml", ".clj", ".vue", ".svelte",
           ".sql", ".html", ".css", ".scss", ".m", ".mm", ".zig", ".nim", ".pl"}
EXCL_DIR = ("node_modules/", "vendor/", "third_party/", "third-party/", "thirdparty/", "dist/", "build/", "target/", ".git/", "__pycache__/",
            "site-packages/", "venv/", ".venv/", "bower_components/", "Pods/", "generated/", "gen/", ".next/", "coverage/", "docs/", "doc/")
EXCL_NAME = re.compile(r"(\.min\.(js|css)$|\.pb\.go$|_pb2(_grpc)?\.py$|\.generated\.|\.g\.dart$|\.d\.ts$|-lock\.|\.lock$)")
_LD = None


def _lang(text: str) -> str:
    global _LD
    if _LD is None:
        from lingua import LanguageDetectorBuilder
        _LD = LanguageDetectorBuilder.from_all_languages().with_minimum_relative_distance(0.2).build()
    t = xfeat.prose_for_lang(text)
    if not t:
        return "unknown"
    lg = _LD.detect_language_of(t)
    return lg.name.lower() if lg else "unknown"


def _src_stats(tree: list[dict]) -> tuple[int, int]:
    b = n = 0
    for t in tree:
        if t["t"] != "blob" or not t["s"]:
            continue
        p = t["p"]
        ext = "." + p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
        if ext not in SRC_EXT:
            continue
        if any(("/" + p).find("/" + d) >= 0 for d in EXCL_DIR) or EXCL_NAME.search(p):
            continue
        b += t["s"]; n += 1
    return b, n


def repo_features(idx: dict, log=print) -> pl.DataFrame:
    """One row per repo: D2 source bytes, D3 age at snapshot, D1 settings flags."""
    import datetime as dt
    import gzip
    snap = dt.datetime.fromisoformat(config.load()["snapshot"]["date"])
    created = {}
    for line in open(ROOT / "data/raw/repo_meta.jsonl"):
        j = json.loads(line)
        c = (j.get("meta") or {}).get("createdAt")
        if c:
            created[j["key"].lower()] = dt.datetime.fromisoformat(c.replace("Z", "")).date()
    sett: dict[tuple, dict] = {}
    sp = ROOT / "data/raw/settings.jsonl"
    if sp.exists():
        for line in open(sp):
            j = json.loads(line)
            blobs = [t for f in j["files"] if f["status"] == "ok" and (t := _blob(f["blob_sha"])) is not None]
            if blobs:
                sett[(j["owner"], j["repo"])] = xfeat.parse_settings(blobs) or {}
            elif j["files"]:
                sett[(j["owner"], j["repo"])] = {}
    rows = []
    for (o, r), rec in idx.items():
        try:
            d = json.loads(gzip.decompress((ROOT / "data/trees" / f"{o}__{r}__{rec['sha'][:12]}.json.gz").read_bytes()))
        except Exception:
            continue
        sb, sn = _src_stats(d["tree"])
        c = created.get(f"{o}/{r}".lower())
        row = {"owner": o, "repo": r, "src_bytes": None if d.get("truncated") else sb, "src_files": None if d.get("truncated") else sn,
               "age_days": (snap.date() - c).days if c else None, "has_settings_file": (o, r) in sett}
        st = sett.get((o, r))
        row.update({k: (st or {}).get(k) for k in SETTING_KEYS})
        row["settings_parsed"] = bool(st)
        rows.append(row)
    return pl.DataFrame(rows, schema_overrides={"src_bytes": pl.Int64, "src_files": pl.Int64, "age_days": pl.Int64})


SETTING_KEYS = ["s_any_allow", "s_broad_bash", "s_network", "s_bypass", "s_accept_edits", "s_all_mcp", "s_has_deny", "s_hooks",
                "s_hook_format_lint", "s_hook_test", "s_hook_guard", "s_hook_notify", "s_hook_other"]


def run(log=print) -> list[str]:
    cfg = config.load()
    rng = random.Random(cfg["snapshot"]["seed"])
    keep = {(o, r) for o, r in devset.nondev(strata.load_explore(None)).select("owner", "repo").iter_rows()}
    idx = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        if (j["owner"], j["repo"]) in keep and j["status"] in ("ok", "partial"):
            idx[(j["owner"], j["repo"])] = j
    rdm = {}
    rp = ROOT / "data/raw/readmes.jsonl"
    if rp.exists():
        for line in open(rp):
            j = json.loads(line)
            rdm[(j["owner"], j["repo"])] = j
    a1f = pl.read_parquet(ROOT / "data/features/a1_files.parquet").filter(pl.col("primary") & pl.col("root") & ~pl.col("symlinked"))
    cm = pl.read_parquet(ROOT / "data/features/commands.parquet")
    cmd_by: dict[tuple, set] = {}
    tool_by: dict[tuple, set] = {}
    for o, r, nm, c, t in cm.select("owner", "repo", "name", "cmd", "tool").iter_rows():
        cmd_by.setdefault((o, r, nm), set()).add(c)
        tool_by.setdefault((o, r, nm), set()).add(t)
    stacks = {(o, r): (b, a) for o, r, b, a in pl.read_parquet(ROOT / "data/features/stacks.parquet").select("owner", "repo", "bridge", "agents_symlink_to_claude").iter_rows()}
    rows, texts, smell_rows = [], {}, []
    rd_cache: dict[tuple, tuple] = {}
    for o, r, nm, path in a1f.select("owner", "repo", "name", "path").iter_rows():
        rec = idx.get((o, r))
        if not rec:
            continue
        fe = next((f for f in rec["files"] if f["path"] == path), None)
        text = _blob(fe["blob_sha"]) if fe else None
        if text is None:
            continue
        texts[(o, r, nm)] = text
        rd = rdm.get((o, r))
        has_rd, rtext = False, None
        if rd and rd.get("status") == "ok" and rd.get("blob_sha"):
            rtext = _blob(rd["blob_sha"])
            has_rd = rtext is not None
        n_sh = shared = otok = olines = 0
        if has_rd:
            key = (o, r)
            if key not in rd_cache:
                rd_cache.clear()
                rd_cache[key] = _shingles(_words(rtext))
            n_sh, shared, otok, olines = _overlap(text, rd_cache[key])
        cmds = cmd_by.get((o, r, nm), set())
        nofence = "\n".join(l for l, fence in iter_lines(text) if not fence)
        rows.append({"owner": o, "repo": r, "name": nm, "readme_present": has_rd, "n_shingles": n_sh,
                     "overlap_share": (shared / n_sh) if (has_rd and n_sh) else None, "overlap_tokens": otok if has_rd and n_sh else None,
                     "has_test": any(TEST_RE.search(c) for c in cmds), "has_build": any(BUILD_RE.search(c) for c in cmds),
                     "has_dont": bool(DONT_RE.search(nofence)), "lang": _lang(text), **xfeat.text_features(text)})
        sm = xfeat.smell_examples(text) if rows and rows[-1].get("smell_any") else []
        if sm:
            smell_rows.append((o, r, nm, sm))
    xf = pl.DataFrame(rows)
    cl = _exact_clusters(texts, cfg, log)
    xf = xf.join(pl.DataFrame([{"owner": o, "repo": r, "name": n, "cluster_size_exact": c} for (o, r, n), c in cl.items()], schema={"owner": pl.Utf8, "repo": pl.Utf8, "name": pl.Utf8, "cluster_size_exact": pl.Int64}), on=["owner", "repo", "name"], how="left")
    _caps_check(texts, cfg, hid_=lambda o, r: hashlib.sha1(f"{o}/{r}".encode()).hexdigest()[:6])
    xf.write_parquet(ROOT / "data/features/x_files.parquet")
    log(f"x_files: {xf.height} rows; with README {xf['readme_present'].sum()}")

    xr2 = repo_features(idx, log)
    xr2.write_parquet(ROOT / "data/features/x_repo2.parquet")
    log(f"x_repo2: {xr2.height} repos; settings files {int(xr2['has_settings_file'].sum())}, parsed {int(xr2['settings_parsed'].sum())}")
    rng2 = random.Random(cfg["snapshot"]["seed"] + 1)
    rng2.shuffle(smell_rows)
    hid = lambda o, r: hashlib.sha1(f"{o}/{r}".encode()).hexdigest()[:6]
    L = ["# Instruction smells: 20 random flagged files (repos hashed; local only)", "",
         "Smells: vague (documented phrase list), dup (identical list item twice), contra (always/must X vs never/do-not X on the same two words), template (TODO/FIXME/<your ...>/placeholder). Review: is each flag a fair hit?", ""]
    for i, (o, r, nm, sm) in enumerate(smell_rows[:20], 1):
        L.append(f"## {i}. repo#{hid(o, r)} · {nm}")
        L += [f"- **{k}**: `{v}`" for k, v in sm]
        L.append("")
    (ROOT / "reports/smell_check.md").write_text("\n".join(L))
    ss = xr2.filter(pl.col("settings_parsed")).sample(n=min(15, int(xr2["settings_parsed"].sum())), seed=cfg["snapshot"]["seed"]) if xr2["settings_parsed"].sum() else None
    if ss is not None:
        L = ["# Settings parse check: 15 random parsed repos (hashed; local only)", "", "Fields are what the parser derived from committed `.claude/settings*.json`. Check against the raw file if in doubt.", ""]
        for row in ss.iter_rows(named=True):
            L.append(f"- repo#{hid(row['owner'], row['repo'])}: " + ", ".join(f"{k[2:]}={row[k]}" for k in SETTING_KEYS))
        L.append("")
        sp = ROOT / "data/raw/settings.jsonl"
        raw = {}
        for line in open(sp):
            j = json.loads(line); raw[(j["owner"], j["repo"])] = j
        L.append("## Raw permission/hook entries behind those rows (first entries only)")
        for row in ss.iter_rows(named=True):
            j = raw.get((row["owner"], row["repo"])); ent = []
            for f in (j or {}).get("files", []):
                t = _blob(f["blob_sha"]) if f["status"] == "ok" else None
                try:
                    d = json.loads(t) if t else {}
                except Exception:
                    d = {}
                ent.append({"file": f["path"], "allow": (d.get("permissions", {}) or {}).get("allow", [])[:4] if isinstance(d.get("permissions"), dict) else [], "mode": (d.get("permissions", {}) or {}).get("defaultMode") if isinstance(d.get("permissions"), dict) else None,
                            "hook_events": list(d.get("hooks", {}).keys())[:4] if isinstance(d.get("hooks"), dict) else []})
            L.append(f"- repo#{hid(row['owner'], row['repo'])}: {json.dumps(ent)[:300]}")
        (ROOT / "reports/settings_check.md").write_text("\n".join(L))

    # ---- link classes (A): every repo with both root files; drift only for the independent class
    stk = {(o, r): (bk, asl, both) for o, r, bk, asl, both in pl.read_parquet(ROOT / "data/features/stacks.parquet").select("owner", "repo", "bridge_kind", "agents_symlink_to_claude", "both_root_files").iter_rows()}
    drift, ex, pairs = [], [], []
    for (o, r), (bk, asl, both_root) in stk.items():
        if not both_root or (o, r) not in idx:
            continue
        ct, at = texts.get((o, r, "CLAUDE.md")), texts.get((o, r, "AGENTS.md"))
        cls, direction = xfeat.classify_pair(ct, at, c_sym=(bk == "symlink"), a_sym=bool(asl))
        if cls is None:
            continue
        jac = xfeat.line_jaccard(ct, at) if (ct is not None and at is not None) else None
        row = {"owner": o, "repo": r, "link_class": cls, "link_dir": direction, "pair_jaccard": jac, "drift_eligible": cls == "independent",
               "line_jaccard": jac if cls == "independent" else None, "diverged": (cls == "independent" and jac is not None and jac < 0.5),
               "both_name_tb_cmds": None, "tb_cmds_differ": None}
        if cls == "independent":
            ta = {c for c in cmd_by.get((o, r, "CLAUDE.md"), set()) if TEST_RE.search(c) or BUILD_RE.search(c)}
            tb = {c for c in cmd_by.get((o, r, "AGENTS.md"), set()) if TEST_RE.search(c) or BUILD_RE.search(c)}
            row["both_name_tb_cmds"] = bool(ta) and bool(tb)
            row["tb_cmds_differ"] = (ta != tb) if (ta and tb) else None
            if ta and tb and ta != tb and jac is not None and jac < 0.5:
                ex.append((o, r, jac, sorted(ta), sorted(tb)))
        drift.append(row)
        pairs.append((o, r, cls, direction, ct, at, bk == "symlink", bool(asl)))
    xr = pl.DataFrame(drift, schema={"owner": pl.Utf8, "repo": pl.Utf8, "link_class": pl.Utf8, "link_dir": pl.Utf8, "pair_jaccard": pl.Float64, "drift_eligible": pl.Boolean,
                                     "line_jaccard": pl.Float64, "diverged": pl.Boolean, "both_name_tb_cmds": pl.Boolean, "tb_cmds_differ": pl.Boolean})
    xr.write_parquet(ROOT / "data/features/x_repo.parquet")
    log("link classes: " + ", ".join(f"{k}={v}" for k, v in xr["link_class"].value_counts().iter_rows()))
    rng.shuffle(ex)
    with open(ROOT / "data/log/drift_examples.md", "w") as f:
        f.write("# Drift examples among INDEPENDENT pairs (local only; repo names hashed; commands only)\n\n")
        for o, r, jac, ta, tb in ex[:3]:
            f.write(f"- repo#{hashlib.sha1(f'{o}/{r}'.encode()).hexdigest()[:6]} line-Jaccard {jac:.2f}\n  - CLAUDE.md names: {', '.join(ta[:6])}\n  - AGENTS.md names: {', '.join(tb[:6])}\n")
    _sheets(pairs, texts, idx, cfg, hid, log)
    _heldout(pairs, hid, cfg, log)

    # ---- package-manager mismatch (held for human approval; not in numbers.json)
    flagged, named, ntotal = [], 0, 0
    for (o, r, nm), text in texts.items():
        rec = idx[(o, r)]
        locks = set(rec.get("locks") or [])
        tools = tool_by.get((o, r, nm), set())
        for eco, lockmap, pms in (("js", JS_LOCK, JS_PM), ("py", PY_LOCK, PY_PM)):
            lpm = {lockmap[l] for l in locks if l in lockmap}
            fpm = tools & pms
            if not fpm or len(lpm) != 1:
                continue
            named += 1
            if not (fpm & lpm):
                flagged.append((o, r, nm, eco, sorted(fpm), sorted(lpm), sorted(locks)))
    rng.shuffle(flagged)
    lines = ["# Package-manager mismatch check (human review; do NOT publish numbers until approved)", "",
             f"Files naming a JS or Python package manager in repos with exactly one lockfile of that ecosystem: {named}. Flagged mismatches: {len(flagged)} "
             f"({len(flagged) / max(named, 1):.1%}). Unweighted, non-dev explore repos. 20 random flagged rows below: file + line ±2 + lockfiles present.", ""]
    for i, (o, r, nm, eco, fpm, lpm, locks) in enumerate(flagged[:20], 1):
        text = texts[(o, r, nm)]
        L = text.splitlines()
        hit = next((ln for n_, ln, seg in extract_with_lines(text) if n_.split(":")[0] in set(fpm) | {"npx"} and n_.split(":")[0] in fpm), None)
        lo, hi = (max(0, hit - 3), min(len(L), hit + 2)) if hit else (0, 0)
        lines += [f"## {i}. {o}/{r} · {nm} · {eco}", f"File names: {fpm}; lockfiles say: {lpm}; root lockfiles: {locks}; line {hit}", "```"] + [x[:200] for x in L[lo:hi]] + ["```", ""]
    (ROOT / "reports/pm_check.md").write_text("\n".join(lines))
    log(f"pm_check: named {named}, flagged {len(flagged)}; reports/pm_check.md written")
    return [f"extras ok: files {xf.height}, drift repos {xr.height}, pm flagged {len(flagged)}/{named}"]


def _head(text: str | None, n: int, sym: bool = False, target: str = "") -> tuple[list[str], int]:
    if sym:
        return [f"[git symlink to {target}]"], 0
    if text is None:
        return ["[file text unavailable]"], 0
    L = xfeat.nonempty(text)
    return [l[:160] for l in L[:n]], len(L)


def _sheets(pairs, texts, idx, cfg, hid, log) -> None:
    """link_check (30 pairs stratified by class) and agentsmd_check (40 AGENTS.md + 20 CLAUDE.md control). Blind rows (no detector output) go to data/log/*_rows.json."""
    if (ROOT / "data/log/link_rows.json").exists():  # the 30 + 60 rows were labeled by two labelers: freeze them (detector v1 classes stay in the record)
        return
    rng = random.Random(cfg["snapshot"]["seed"] + 7)
    by: dict[str, list] = {}
    for p in pairs:
        by.setdefault(p[2], []).append(p)
    pick = []
    per = 6
    for c in xfeat.LINK_CLASSES:
        g = by.get(c, [])
        rng.shuffle(g)
        pick += g[:per]
    rng.shuffle(pick)
    blind, L = [], ["# Link check: 30 random CLAUDE.md + AGENTS.md pairs, 6 per detector class (repos hashed; local only)", "",
                    "Labels for labelers: symlink / import_bridge / prose_pointer / near_copy / independent / unclear. Detector class shown for the human only.", ""]
    for i, (o, r, cls, d, ct, at, cs, as_) in enumerate(pick, 1):
        ch, cn = _head(ct, 15, cs, "AGENTS.md")
        ah, an = _head(at, 15, as_, "CLAUDE.md")
        rid = f"L{i:02d}"
        blind.append({"id": rid, "claude_lines": cn, "agents_lines": an, "claude_head": ch, "agents_head": ah})
        L += [f"## {rid} · repo#{hid(o, r)} · detector class: **{cls}** {d}", f"CLAUDE.md ({cn} non-empty lines):", "```"] + ch + ["```", f"AGENTS.md ({an} non-empty lines):", "```"] + ah + ["```", ""]
    (ROOT / "reports/link_check.md").write_text("\n".join(L))
    json.dump(blind, open(ROOT / "data/log/link_rows.json", "w"))
    json.dump({b["id"]: p[2] for b, p in zip(blind, pick)}, open(ROOT / "data/log/link_detector.json", "w"))
    # agentsmd check
    st = {(o, r): s for o, r, s in strata.load_explore(None).select("owner", "repo", "stratum").iter_rows()}
    rows = []
    for nm, k in (("AGENTS.md", 20), ("CLAUDE.md", 10)):
        for sname in ("established", "long_tail"):
            keys = [(o, r) for (o, r, n_) in texts if n_ == nm and st.get((o, r)) == sname]
            keys.sort()
            rng.shuffle(keys)
            rows += [(o, r, nm, sname) for o, r in keys[:k]]
    rng.shuffle(rows)
    blind2, L = [], ["# AGENTS.md check: 40 random AGENTS.md (20 popular, 20 small) + 20 CLAUDE.md control (repos hashed; local only)", "",
                     "Labels: instruction_file (tells a coding agent how to work in this repo) / agent_catalog_or_other (documents a project's own agents, or unrelated content) / unclear.", ""]
    for i, (o, r, nm, sname) in enumerate(rows, 1):
        h, n_ = _head(texts[(o, r, nm)], 20)
        rid = f"A{i:02d}"
        blind2.append({"id": rid, "file_name": nm, "lines": n_, "head": h})
        L += [f"## {rid} · repo#{hid(o, r)} · {nm} · {sname} ({n_} non-empty lines)", "```"] + h + ["```", ""]
    (ROOT / "reports/agentsmd_check.md").write_text("\n".join(L))
    json.dump(blind2, open(ROOT / "data/log/agentsmd_rows.json", "w"))
    log(f"sheets: link_check {len(pick)} rows, agentsmd_check {len(rows)} rows")


def _heldout(pairs, hid, cfg, log) -> None:
    """Held-out validation of detector v2 (v2 was tuned after the first 30-pair check): 5 random pairs per v2 class, none from the first sheet."""
    if (ROOT / "data/log/link_rows_v2.json").exists():
        return
    used = set(re.findall(r"repo#([0-9a-f]{6})", (ROOT / "reports/link_check.md").read_text()))
    rng = random.Random(cfg["snapshot"]["seed"] + 99)
    by: dict[str, list] = {}
    for p in pairs:
        if hid(p[0], p[1]) not in used:
            by.setdefault(p[2], []).append(p)
    pick = []
    for c in xfeat.LINK_CLASSES:
        g = by.get(c, [])
        rng.shuffle(g)
        pick += g[:5]
    rng.shuffle(pick)
    blind = []
    L = ["# Link check, held-out set (detector v2; 5 per class; repos hashed; local only)", ""]
    for i, (o, r, cls, d, ct, at, cs, as_) in enumerate(pick, 1):
        ch, cn = _head(ct, 15, cs, "AGENTS.md")
        ah, an = _head(at, 15, as_, "CLAUDE.md")
        rid = f"V{i:02d}"
        blind.append({"id": rid, "claude_lines": cn, "agents_lines": an, "claude_head": ch, "agents_head": ah})
        L += [f"## {rid} · repo#{hid(o, r)} · detector v2: **{cls}** {d}", f"CLAUDE.md ({cn}):", "```"] + ch + ["```", f"AGENTS.md ({an}):", "```"] + ah + ["```", ""]
    (ROOT / "reports/link_check_heldout.md").write_text("\n".join(L))
    json.dump(blind, open(ROOT / "data/log/link_rows_v2.json", "w"))
    json.dump({b["id"]: p[2] for b, p in zip(blind, pick)}, open(ROOT / "data/log/link_detector_v2.json", "w"))
    log(f"held-out sheet: {len(pick)} rows")


def _exact_clusters(texts: dict, cfg: dict, log) -> dict:
    """Near-duplicate clusters with EXACT verification: MinHash-LSH proposes candidate pairs, a pair becomes an edge only when the exact Jaccard of the
    5-word shingle sets is >= the threshold; clusters are connected components of verified edges (members of a component need not all be near each other)."""
    from datasketch import MinHash, MinHashLSH
    perm, thr = cfg["dedup"]["minhash_perm"], cfg["dedup"]["near_dup_jaccard"]
    keys = sorted(texts)
    sh = {k: _shingles(_words(texts[k])) for k in keys}
    mh = {}
    for k in keys:
        m = MinHash(num_perm=perm)
        for x in sh[k]:
            m.update(x.encode())
        mh[k] = m
    lsh = MinHashLSH(threshold=thr, num_perm=perm)
    for k in keys:
        lsh.insert("|".join(k), mh[k])
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    cand = ver = 0
    for k in keys:
        for m in lsh.query(mh[k]):
            kk = tuple(m.split("|"))
            if kk == k:
                continue
            cand += 1
            a, b = sh[k], sh[kk]
            if a and b and len(a & b) / len(a | b) >= thr:
                ver += 1
                parent[find(kk)] = find(k)
    size: dict = {}
    for k in keys:
        size[find(k)] = size.get(find(k), 0) + 1
    log(f"near-dup edges: {cand} LSH candidate hits, {ver} verified by exact Jaccard >= {thr}")
    return {k: size[find(k)] for k in keys}


def _caps_check(texts: dict, cfg: dict, hid_) -> None:
    """20 random CLAUDE.md files whose only shouting is ALL-CAPS lines (no IMPORTANT/NEVER/... keywords): the caps lines, for a precision check."""
    from .core import SHOUT, iter_lines
    rng = random.Random(cfg["snapshot"]["seed"] + 21)
    hits = []
    for (o, r, nm), t in sorted(texts.items()):
        if nm != "CLAUDE.md":
            continue
        caps, kw = [], 0
        for line, fence in iter_lines(t):
            if fence:
                continue
            kw += len(SHOUT.findall(line))
            letters = re.sub(r"[^A-Za-z]", "", line)
            if len(letters) >= 10 and letters.isupper() and not line.lstrip().startswith(("#", "|")):
                caps.append(line.strip()[:110])
        if caps and kw == 0:
            hits.append((o, r, caps))
    rng.shuffle(hits)
    L = ["# ALL-CAPS-only shouting: 20 random CLAUDE.md (no keyword hits) with the ALL-CAPS lines that triggered (repos hashed; local only)", "",
         "Question per row: is at least one shown line emphasis aimed at the reader (a shouted instruction/warning)? Label y / n (n = acronym list, comment, label, ID, table).", ""]
    for i, (o, r, caps) in enumerate(hits[:20], 1):
        L += [f"## C{i:02d} · repo#{hid_(o, r)} ({len(caps)} caps line(s))"] + [f"- `{c}`" for c in caps[:3]] + [""]
    (ROOT / "reports/caps_check.md").write_text("\n".join(L))
    json.dump([{"id": f"C{i:02d}", "caps": c[:3]} for i, (o, r, c) in enumerate(hits[:20], 1)], open(ROOT / "data/log/caps_rows.json", "w"))
