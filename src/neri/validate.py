"""M5: validation sheets and scoring for the A2 wrongness detector.

Protocol (docs/DECISIONS.md): the detector is FROZEN by a commit before a sheet is drawn (`build` refuses if src/ or tests/
is dirty and records the git head + code hashes); sheets are drawn from NON-DEV repos only; every row carries the source line,
+-3 lines of context, a non-English flag, the detector's evidence and the parent folder's contents; two independent labelers
(Claude Code and a subagent that has not seen the detector code or prior labels) assign one of the categories below; we report
agreement (Cohen's kappa) and precision (share labeled STALE) with Wilson intervals. Sheets contain third-party text: local only.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import subprocess
import time
from posixpath import dirname

import polars as pl

from . import config, core, devset
from .features import _blob, _tree_paths

ROOT = config.ROOT
REP = ROOT / "reports"
LABELS = ("STALE", "EXPECTED_CREATED", "NOT_A_REFERENCE", "UNKNOWN", "ALIVE")

RUBRIC = """# Validation rubric (shared by every labeler)

An automated checker FLAGGED the reference below as dead in an agent-instruction file (CLAUDE.md / AGENTS.md).
Decide what it really is. Use ONLY the sheet row: the reference, its source line and +-3 lines of context, the checker's
evidence, the parent folder's contents, and the link. If the context is not English, translate it first. Do not open
repositories and do not read any source code of the checker. Judge each row independently.

Assign exactly one category:

- **STALE**: the reference is expected to exist in THIS repository now, and it is missing. Includes targets that were moved
  or renamed (a different folder, extension or letter case) and links that point to a missing file. A reader following the
  instruction would hit something that does not exist as written.
- **EXPECTED_CREATED**: it is not meant to be committed: per-user or setup files in a template repo, files the reader or the
  tool creates ("copy X to Y", generated output), build or runtime outputs, caches, state, logs.
- **NOT_A_REFERENCE**: it is not a claim about this repo's files: an example or placeholder (`foo`, `specific`, `my-app`,
  `vX.Y.Z`), a wildcard or pattern (`just test-*`), a conditional ("when present", "if it exists"), a naming convention, a
  deliberate historical mention, something in ANOTHER repository, the user's project that a tool inspects, a system file,
  a URL fragment or a code identifier.
- **UNKNOWN**: you cannot verify it from the sheet (for example the file listing may be incomplete, or the target could be a
  symlink or submodule). When in doubt between STALE and anything else, choose the other category.
- **ALIVE**: the target does exist (the checker was simply wrong).

A command that is run from a different directory (after `cd`) must be judged against that directory, not the repo root.

Answer with a JSON object mapping each row id to one category name.
"""


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def _hash(*names: str) -> dict:
    return {n: hashlib.sha256((ROOT / "src/neri" / n).read_bytes()).hexdigest() for n in names}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def _stratified(df: pl.DataFrame, n: int, rng: random.Random) -> pl.DataFrame:
    """Proportional allocation across `kind`, at least 1 per kind when possible, at most 2 rows per repo."""
    if df.is_empty():
        return df
    kinds = df.group_by("kind").agg(pl.len().alias("n")).sort("kind")
    total = kinds["n"].sum()
    alloc = {k: max(1, round(n * c / total)) for k, c in kinds.iter_rows()}
    while sum(alloc.values()) > n:
        alloc[max(alloc, key=alloc.get)] -= 1
    parts = []
    for k, a in alloc.items():
        rows = df.filter(pl.col("kind") == k).sort(["owner", "repo", "ref"])
        idx = list(range(rows.height)); rng.shuffle(idx)
        per_repo: dict[tuple, int] = {}
        pick = []
        for i in idx:
            key = (rows["owner"][i], rows["repo"][i])
            if per_repo.get(key, 0) < 2:
                per_repo[key] = per_repo.get(key, 0) + 1
                pick.append(i)
            if len(pick) == a:
                break
        parts.append(rows[sorted(pick)])
    return pl.concat(parts)


def _non_english(text: str) -> bool:
    letters = re.findall(r"[^\W\d_]", text)
    return bool(letters) and sum(ord(c) > 127 for c in letters) / len(letters) > 0.2


def build(tag: str | None, rnd: int, log=print) -> str:
    cfg = config.load()
    if _git("status", "--porcelain", "--", "src", "tests"):  # the detector is FROZEN when the sheet is drawn
        return "REFUSED: uncommitted changes in src/ or tests/; commit first so the detector is frozen before the sheet is drawn"
    d = ROOT / "data/features" / (f"c{tag}" if tag else "")
    det = devset.nondev(pl.read_parquet(d / "a2_detail.parquet")).filter(pl.col("dead") & ~pl.col("unknown"))  # fresh, non-dev
    assert devset.nondev(det).height == det.height
    rng = random.Random(f"{cfg['snapshot']['seed']}|validate|{rnd}")
    paths = _stratified(det.filter(pl.col("type") == "path"), 50, rng)
    cmds = _stratified(det.filter(pl.col("type") == "cmd"), 25, rng)
    excl = devset.nondev(pl.read_parquet(d / "a2_detail.parquet")).filter(
        (pl.col("type") == "path") & pl.col("category").is_in(["EXPECTED_CREATED", "NOT_A_REFERENCE"]) & pl.col("line").is_not_null())
    audit = pl.concat([_stratified(excl.filter(pl.col("category") == c).with_columns(kind=pl.col("reason")), 10, rng)
                       for c in ("EXPECTED_CREATED", "NOT_A_REFERENCE") if excl.filter(pl.col("category") == c).height],
                      how="diagonal") if excl.height else excl
    flagged_df = pl.concat([paths.with_columns(flagged=pl.lit(True)), cmds.with_columns(flagged=pl.lit(True)),
                            audit.with_columns(flagged=pl.lit(False))], how="diagonal")
    order = list(range(flagged_df.height)); rng.shuffle(order)  # blind: flagged and audit rows are interleaved
    flagged_df = flagged_df[order]
    idx = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        idx[(j["owner"], j["repo"])] = j
    rows, md = [], [f"# Validation sheet, round {rnd} (local only; contains third-party text)\n",
                    "Read `validation_rubric.md` first. Answer with a JSON object {row id: category}.\n"]
    for i, r in enumerate(flagged_df.iter_rows(named=True), 1):
        rec = idx[(r["owner"], r["repo"])]
        f = next(f for f in rec["files"] if f["root"] and f["name"] == r["name"])
        lines = (_blob(f["blob_sha"]) or "").splitlines()
        ln = int(r["line"])
        ctx = "\n".join(f"{k + 1:>4}{'>' if k + 1 == ln else ' '} {lines[k][:200]}" for k in range(max(0, ln - 4), min(len(lines), ln + 3)))
        ev = json.loads(r["evidence"] or "{}")
        tree = _tree_paths(r["owner"], r["repo"], rec["sha"])
        ref = r["ref"].split(" ", 1)[-1] if r["type"] == "cmd" else r["ref"]
        par = dirname(ref)
        sib = sorted({p[len(par) + 1:].split("/")[0] for p in tree if (p.startswith(par + "/") if par else "/" not in p)})[:8]
        evidence = {"moved_to": r["moved_to"] or None, "working_dir": r["cwd"] or "repo root",
                    "tree_truncated": False, **{k: v for k, v in ev.items() if v}}
        row = {"id": f"r{rnd}-{i:02d}", "type": r["type"], "kind": r["kind"], "owner": r["owner"], "repo": r["repo"], "sha": rec["sha"][:10],
               "flagged": bool(r["flagged"]), "detector_category": r["category"],
               "ref": r["ref"], "file": r["name"], "line": ln, "moved": bool(r["moved"]), "non_english": _non_english(ctx),
               "context": ctx, "evidence": json.dumps(evidence), "siblings": json.dumps(sib), "parent_exists": bool(par == "" or any(p.startswith(par + "/") for p in tree))}
        rows.append(row)
        md += [f"### {row['id']}  [{row['type']}/{row['kind']}]  `{row['ref']}`",
               f"- source: {row['file']} line {ln} - https://github.com/{r['owner']}/{r['repo']}/blob/{rec['sha']}/{r['name']}#L{ln}",
               f"- non-English context: {'YES, translate before labeling' if row['non_english'] else 'no'}",
               "- context (+-3 lines; `>` marks the line):", "```", ctx, "```",
               f"- checker evidence: {json.dumps(evidence, ensure_ascii=False)}",
               f"- parent folder `{par or '.'}` exists in the tree: {'yes' if row['parent_exists'] else 'NO'}; entries there: {sib}", ""]
    REP.mkdir(exist_ok=True)
    pl.DataFrame(rows).write_parquet(REP / f"validation_rows_r{rnd}.parquet")
    (REP / f"validation_sheet_r{rnd}.md").write_text("\n".join(md))
    (REP / "validation_rubric.md").write_text(RUBRIC)
    (REP / f"validation_freeze_r{rnd}.json").write_text(json.dumps({
        "round": rnd, "checkpoint": tag, "tier_version": core.TIER_VERSION, "drawn_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "git_head": _git("rev-parse", "HEAD"), "detector_sha256": _hash("core.py", "census.py", "classify.py"),
        "n_paths": paths.height, "n_cmds": cmds.height, "n_audit": audit.height, "dev_excluded": True}, indent=1))
    note = "" if cmds.height >= 25 else f" (only {cmds.height} STALE commands available; target 25)"
    return f"wrote reports/validation_sheet_r{rnd}.md ({len(rows)} rows: {paths.height} flagged paths, {cmds.height} flagged cmds, {audit.height} blind exclusion-audit rows){note}; tier {core.TIER_VERSION} frozen at {_git('rev-parse', '--short', 'HEAD')}"


def _freeze_warning(rnd: int) -> list[str]:
    p = REP / f"validation_freeze_r{rnd}.json"
    if not p.exists():
        return []
    fz = json.loads(p.read_text())
    if fz["detector_sha256"] != _hash(*fz["detector_sha256"].keys()):
        return ["WARNING: detector code changed after this sheet was drawn; these labels no longer validate the current detector"]
    return [f"freeze ok: detector unchanged since the sheet was drawn ({fz['tier_version']} at {fz['git_head'][:8]})"]


def _stale(label: str) -> bool:
    return label in ("STALE", "TP")  # TP/FP labels from rounds 1-3 map to STALE/not


def cohen_kappa(a: dict, b: dict, ids: list) -> tuple[float, float]:
    """Cohen's kappa for two labelers over `ids` (any number of categories); returns (kappa, raw agreement)."""
    n = len(ids)
    agree = sum(a[i] == b[i] for i in ids) / n
    cats = {a[i] for i in ids} | {b[i] for i in ids}
    pe = sum((sum(a[i] == c for i in ids) / n) * (sum(b[i] == c for i in ids) / n) for c in cats)
    return ((agree - pe) / (1 - pe) if pe < 1 else float("nan")), agree


def _precision_lines(rnd: int, labels: dict, name: str) -> list[str]:
    rows = pl.read_parquet(REP / f"validation_rows_r{rnd}.parquet")
    out = []
    aud = [i for i in rows.filter(~pl.col("flagged"))["id"] if i in labels]
    if aud:
        k = sum(_stale(labels[i]) for i in aud)
        lo, hi = _wilson(k, len(aud))
        out.append(f"  exclusion audit [{name}]: {k}/{len(aud)} excluded rows are really STALE = {k / len(aud):.1%} (CI {lo:.1%}-{hi:.1%}); "
                   "this is the share of real stale refs the exclusion rules hide")
    rows = rows.filter(pl.col("flagged"))
    for t in ("path", "cmd"):
        sub = rows.filter(pl.col("type") == t)
        ids = [i for i in sub["id"] if i in labels]
        tp = sum(_stale(labels[i]) for i in ids)
        lo, hi = _wilson(tp, len(ids))
        mix = {c: sum(labels[i] == c for i in ids) for c in LABELS if any(labels[i] == c for i in ids)}
        out.append(f"  {t} precision [{name}]: {tp}/{len(ids)} = {tp / max(1, len(ids)):.1%} (Wilson 95% CI {lo:.1%}-{hi:.1%}); labeled as: {mix}")
        if t == "path":
            for tier, m in (("moved-v3", True), ("missing-only", False)):
                s2 = [i for i in sub.filter(pl.col("moved") == m)["id"] if i in labels]
                k2 = sum(_stale(labels[i]) for i in s2)
                lo2, hi2 = _wilson(k2, len(s2))
                out.append(f"    tier {tier}: {k2}/{len(s2)} = {k2 / max(1, len(s2)):.1%} (CI {lo2:.1%}-{hi2:.1%})")
    return out


def score(rnd: int, suffix: str = "") -> list[str]:
    labels = json.loads((REP / f"validation_labels_r{rnd}{suffix}.json").read_text())
    return _freeze_warning(rnd) + [f"round {rnd} labeler{suffix or ' (mine)'}:"] + _precision_lines(rnd, labels, suffix.strip("_") or "mine")


def kappa(rnd: int) -> list[str]:
    a = json.loads((REP / f"validation_labels_r{rnd}.json").read_text())
    b = json.loads((REP / f"validation_labels_r{rnd}_indep.json").read_text())
    ids = sorted(set(a) & set(b))
    k, agree = cohen_kappa(a, b, ids)
    kb, ab = cohen_kappa({i: "S" if _stale(a[i]) else "N" for i in ids}, {i: "S" if _stale(b[i]) else "N" for i in ids}, ids)
    out = [f"labelers: n={len(ids)} rows; {len(LABELS)}-category agreement {agree:.1%}, Cohen's kappa {k:.2f}; "
           f"binary STALE-vs-not agreement {ab:.1%}, kappa {kb:.2f}",
           "disagreements: " + ", ".join(f"{i}(me={a[i]},indep={b[i]})" for i in ids if a[i] != b[i])]
    both = {i: ("STALE" if _stale(a[i]) and _stale(b[i]) else "NOT") for i in ids}
    out += _precision_lines(rnd, a, "mine") + _precision_lines(rnd, b, "indep") + _precision_lines(rnd, both, "both-STALE")
    return out + _freeze_warning(rnd)
