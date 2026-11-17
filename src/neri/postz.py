"""Round-3 deterministic features (no LLM, no network): generator signature, hooks (events + purpose classes), tool co-presence.
Outputs data/features/x_files2.parquet (per primary file), x_repo3.parquet (per repo), reports/signature_lines.md,
reports/hooks_check.md, reports/copresence.md. Explore, non-dev repos only."""
from __future__ import annotations

import gzip
import json
import random
import re
from collections import Counter

import polars as pl

from . import config, devset, strata

ROOT = config.ROOT
BLOBS, TREES = ROOT / "data/blobs", ROOT / "data/trees"

# /init header: NOT published in Anthropic's docs (see docs/RESEARCH-NOTES.md); this is the community-documented line.
SIG_RE = re.compile(r"provides guidance to claude code", re.I)
SIG_WINDOW = 10  # non-empty lines
BOILER_RE = re.compile(r"^(claude\.md|agents\.md|project instructions?|agent instructions?|repository guidelines|guidelines|"
                       r"this file provides guidance to .*|.*instructions for (ai|claude|coding).*)$")

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop", "SubagentStop", "Notification", "PreCompact")
CONTEXT_EVENTS = {"SessionStart", "UserPromptSubmit", "PreCompact"}
# one primary class per command: first matching rule wins
HOOK_CLASSES = ("guard_block", "context_injection", "format", "lint", "typecheck", "test", "git", "log_audit", "notify", "other")
_RULES = (
    ("notify", re.compile(r"notif|osascript|afplay|\bsay\b|terminal-notifier|ntfy|\bbeep|display notification|notify-send|slack|discord|\bplay\b|paplay", re.I)),
    ("guard_block", re.compile(r"exit 2|exit\(2\)|\bblock|\bdeny|permissionDecision|\bguard|\bprevent|\bprotect|forbid|reject|\bvalidate|secret|dangerous|\bcheck[-_ ]?(?:command|bash|file|path|safe)", re.I)),
    ("format", re.compile(r"prettier|\bblack\b|gofmt|rustfmt|cargo fmt|\bfmt\b|format|isort|biome format|swiftformat|clang-format|\bruff format|dprint|stylua|shfmt", re.I)),
    ("typecheck", re.compile(r"\btsc\b|mypy|pyright|typecheck|type-check|\bpyre\b|cargo check|go vet|\bflow\b", re.I)),
    ("lint", re.compile(r"eslint|\blint|ruff|flake8|pylint|clippy|oxlint|biome|stylelint|rubocop|golangci|swiftlint|shellcheck|markdownlint", re.I)),
    ("test", re.compile(r"pytest|jest|vitest|\btests?\b|mocha|cargo test|go test|rspec|phpunit|unittest|\bnpm (?:run )?test|yarn test|pnpm test", re.I)),
    ("git", re.compile(r"\bgit\b|\bgh\b|commit|\bbranch\b", re.I)),
    ("log_audit", re.compile(r">>|\btee\b|\blog\b|logger|audit|transcript|telemetry|\.jsonl|analytics|history|record", re.I)),
)
_CTX = re.compile(r"\becho\b|\bcat\b|\bprintf\b|additionalContext|git status|git log|git diff|\bls\b|\btree\b|context|inject|load|date\b|\bjq\b", re.I)


def classify_hook(event: str, cmd: str) -> str:
    for name, rx in _RULES:
        if rx.search(cmd):
            if name == "notify" and re.fullmatch(r".*(slack|discord).*", cmd, re.I) is None and not rx.search(cmd):
                continue
            return name
    if event in CONTEXT_EVENTS and _CTX.search(cmd):
        return "context_injection"
    return "other"


def hook_commands(blob: str) -> list[tuple[str, str, str]]:
    """[(event, matcher, command)] from one settings JSON (command-type hooks only)."""
    try:
        j = json.loads(blob)
    except Exception:
        return []
    out = []
    h = j.get("hooks") if isinstance(j, dict) else None
    if isinstance(h, dict):
        for ev, arr in h.items():
            for ent in (arr if isinstance(arr, list) else []):
                if not isinstance(ent, dict):
                    continue
                for hk in ent.get("hooks", []) if isinstance(ent.get("hooks"), list) else []:
                    if isinstance(hk, dict) and isinstance(hk.get("command"), str):
                        out.append((str(ev), str(ent.get("matcher") or ""), hk["command"]))
    return out


# tool config presence from the repo tree (root-anchored dirs/files; GEMINI.md at any depth). Truncated trees give lower bounds.
TOOLS = {
    "cursor": ("prefix", (".cursor/", ".cursorrules")),
    "copilot": ("prefix", (".github/copilot-instructions.md", ".github/instructions/")),
    "gemini": ("any", ("GEMINI.md", ".gemini/")),
    "windsurf": ("prefix", (".windsurfrules", ".windsurf/")),
    "cline": ("prefix", (".clinerules",)),
    "roo": ("prefix", (".roo/", ".roorules", ".roomodes")),
    "kiro": ("prefix", (".kiro/",)),
    "codex": ("prefix", (".codex/",)),
    "claude_dir": ("prefix", (".claude/",)),
}


def copresence(paths: list[str]) -> dict[str, bool]:
    base = {p.rsplit("/", 1)[-1] for p in paths}
    out = {}
    for tool, (mode, keys) in TOOLS.items():
        hit = any(p.startswith(keys) for p in paths)
        if mode == "any":
            hit = hit or "GEMINI.md" in base or any(p.startswith(".gemini/") for p in paths)
        out[tool] = hit
    return out


def _norm_first(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[#*>`_]", " ", s.lower())).strip()[:120]


_TIDX: dict | None = None


def _tree(owner: str, repo: str, sha: str | None):
    global _TIDX
    if _TIDX is None:
        _TIDX = {}
        for f in TREES.iterdir():
            parts = f.name.rsplit("__", 1)[0]
            _TIDX[parts] = f
    f = _TIDX.get(f"{owner}__{repo}")
    if f is None:
        return None
    j = json.load(gzip.open(f))
    return [t["p"] for t in j["tree"]] if isinstance(j, dict) else None


def run(log=print) -> list[str]:
    cfg = config.load()
    keep = {(o, r) for o, r in devset.nondev(strata.load_explore(None)).select("owner", "repo").iter_rows()}
    idx = {}
    for line in open(ROOT / "data/raw/fetch_index.jsonl"):
        j = json.loads(line)
        if (j["owner"], j["repo"]) in keep and j["status"] in ("ok", "partial"):
            idx[(j["owner"], j["repo"])] = j
    a1f = pl.read_parquet(ROOT / "data/features/a1_files.parquet").filter(pl.col("primary") & ~pl.col("symlinked"))
    rows = []
    first_cnt: dict[str, Counter] = {"CLAUDE.md": Counter(), "AGENTS.md": Counter()}
    prose_cnt: dict[str, Counter] = {"CLAUDE.md": Counter(), "AGENTS.md": Counter()}
    for o, r, nm, path in a1f.select("owner", "repo", "name", "path").iter_rows():
        rec = idx.get((o, r))
        fe = next((f for f in rec["files"] if f["path"] == path), None) if rec else None
        if not fe or not fe.get("blob_sha"):
            continue
        p = BLOBS / fe["blob_sha"][:2] / fe["blob_sha"]
        if not p.exists():
            continue
        lines = [l.strip() for l in p.read_text(errors="replace").splitlines() if l.strip()]
        head = lines[:SIG_WINDOW]
        sig = any(SIG_RE.search(l) for l in head)
        first = _norm_first(lines[0]) if lines else ""
        prose = next((_norm_first(l) for l in lines if not l.startswith("#")), "")
        rows.append({"owner": o, "repo": r, "name": nm, "sig_init": sig, "first_line": first, "first_prose": prose})
        if nm in first_cnt:
            first_cnt[nm][first] += 1
            prose_cnt[nm][prose] += 1
    xf = pl.DataFrame(rows)
    xf.write_parquet(ROOT / "data/features/x_files2.parquet")
    # signature lines sheet (aggregate counts; lines seen >= 15 times only, so no repo-specific text)
    md = ["# First lines of CLAUDE.md / AGENTS.md (normalized), counts >= 15 only. Local; gitignored.", "",
          "Official /init header: not found in Anthropic docs (docs/RESEARCH-NOTES.md). Signature used = a line containing 'provides guidance to Claude Code' in the first 10 non-empty lines.", ""]
    def flag(line, n):
        if SIG_RE.search(line) or BOILER_RE.match(line):
            return "likely generator/boilerplate"
        if line.startswith("<") or "begin:" in line or ":start" in line:
            return "tool-injected block marker (likely generated)"
        return "repeated >=100x: likely template" if n >= 100 else ""
    for title, src in (("first non-empty line", first_cnt), ("first non-heading line (first prose line)", prose_cnt)):
        for nm, c in src.items():
            tot = sum(c.values())
            md += [f"## {nm}, {title}: {tot:,} files, {len(c):,} distinct", "", "| # | count | share | normalized line | flag |", "|--|--|--|--|--|"]
            k = 0
            for line, n in c.most_common(60):
                if n < 15 or k >= 20:
                    break
                k += 1
                md.append(f"| {k} | {n:,} | {n / tot:.1%} | `{line}` | {flag(line, n)} |")
            md.append("")
    (ROOT / "reports/signature_lines.md").write_text("\n".join(md))
    # hooks + co-presence
    rng = random.Random(cfg["snapshot"]["seed"])
    st = {}
    for line in open(ROOT / "data/raw/settings.jsonl"):
        j = json.loads(line)
        if (j["owner"], j["repo"]) in keep:
            st[(j["owner"], j["repo"])] = j
    allcmds = []
    rrows = []
    repos = pl.read_parquet(ROOT / "data/features/repos.parquet").filter(pl.col("dev").not_()) if "dev" in pl.read_parquet(ROOT / "data/features/repos.parquet").columns else pl.read_parquet(ROOT / "data/features/repos.parquet")
    for o, r in repos.select("owner", "repo").iter_rows():
        if (o, r) not in keep:
            continue
        rec = {"owner": o, "repo": r}
        paths = _tree(o, r, None)
        if paths is not None:
            rec.update({f"c_{k}": v for k, v in copresence(paths).items()})
        s = st.get((o, r))
        evs, cls = set(), set()
        n_cmd = 0
        if s:
            for f in s["files"]:
                if f.get("status") != "ok":
                    continue
                p = BLOBS / f["blob_sha"][:2] / f["blob_sha"]
                if not p.exists():
                    continue
                for ev, m, c in hook_commands(p.read_text(errors="replace")):
                    n_cmd += 1
                    evs.add(ev)
                    k = classify_hook(ev, c)
                    cls.add(k)
                    allcmds.append((o, r, ev, m, c, k))
        rec["h_any"] = n_cmd > 0
        rec["h_n_cmds"] = n_cmd
        for e in HOOK_EVENTS:
            rec[f"h_ev_{e}"] = e in evs
        for c in HOOK_CLASSES:
            rec[f"h_cls_{c}"] = c in cls
        rrows.append(rec)
    xr = pl.DataFrame(rrows, infer_schema_length=None)
    xr.write_parquet(ROOT / "data/features/x_repo3.parquet")
    # hooks_check: 20 random hook commands (seeded), anonymized: no owner/repo, command truncated to 200 chars
    pick = rng.sample(allcmds, min(20, len(allcmds)))
    hm = ["# Hooks check: 20 random hook commands with my class (review; correct me). Local; gitignored.", "",
          f"Pool: {len(allcmds):,} command hooks in committed settings of non-dev repos. Rules: first match of notify > guard_block > format > typecheck > lint > test > git > log_audit; "
          "SessionStart/UserPromptSubmit/PreCompact with echo/cat/git status... = context_injection; else other.", "",
          "| # | event | matcher | command (<=200 chars) | my class | your class |", "|--|--|--|--|--|--|"]
    for i, (o, r, ev, m, c, k) in enumerate(pick, 1):
        cc = c.replace("|", "\\|").replace("\n", " ")[:200]
        hm.append(f"| {i} | {ev} | `{m[:30]}` | `{cc}` | {k} | |")
    (ROOT / "reports/hooks_check.md").write_text("\n".join(hm) + "\n")
    return [f"x_files2: {xf.height:,} files; sig_init {int(xf['sig_init'].sum()):,}", f"x_repo3: {xr.height:,} repos; hook commands {len(allcmds):,} in {int(xr['h_any'].sum()):,} repos"]


def matrix(log=print) -> str:
    """reports/copresence.md: weighted co-occurrence of tool configs among repos with an agent file. Point estimates (weights as in numbers.json)."""
    import numpy as np
    from . import report
    df = report.load_repo(None)
    cols = [c for c in df.columns if c.startswith("c_")]
    df = df.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cols]))
    w = df["w"].to_numpy()
    X = {c[2:]: df[c].to_numpy().astype(float) for c in cols}
    X["CLAUDE.md"] = np.array([1.0 if "CLAUDE.md" in (m or "") else 0.0 for m in df["members"]])
    X["AGENTS.md"] = np.array([1.0 if "AGENTS.md" in (m or "") else 0.0 for m in df["members"]])
    names = ["CLAUDE.md", "AGENTS.md"] + [c[2:] for c in cols]
    W = w.sum()
    md = ["# Tool co-presence among repos with an agent file (weighted point estimates)", "",
          f"n = {df.height:,} repos; weights as in numbers.json (population ~ {W:,.0f} repos). 'Also configure' means the config path exists in the committed tree (root-anchored; GEMINI.md at any depth); "
          "trees truncated by the API give lower bounds. Not market share.", "",
          "Diagonal = share of repos with the tool. Cell (row, col) = share of repos with ROW that also have COL.", "",
          "| row \\ col | " + " | ".join(names) + " |", "|--|" + "--|" * len(names)]
    for a in names:
        wa = (w * X[a]).sum()
        md.append(f"| **{a}** ({wa / W:.1%}) | " + " | ".join(f"{(w * X[a] * X[b]).sum() / wa:.0%}" if wa else "" for b in names) + " |")
    (ROOT / "reports/copresence.md").write_text("\n".join(md) + "\n")
    return "reports/copresence.md written"


def languages() -> str:
    """reports/languages.md: weighted top-10 detected languages per file type (approximate; from numbers.json)."""
    n = json.load(open(ROOT / "reports/numbers.json"))["numbers"]
    md = ["# Detected languages of CLAUDE.md / AGENTS.md prose (weighted, approximate; offline detector, files with detectable prose)", ""]
    for ft in ("claude", "agents"):
        rows = sorted(((k.rsplit(".", 1)[1], v) for k, v in n.items() if k.startswith(f"{ft}.overall.d4.lang.")), key=lambda x: -(x[1]["value"] or 0))[:10]
        md += [f"## {ft}.md (n = {rows[0][1]['n']:,})", "", "| language | share | 95% CI |", "|--|--|--|"]
        md += [f"| {l} | {v['value']:.1%} | {v['ci_lo']:.1%}–{v['ci_hi']:.1%} |" for l, v in rows]
        md.append("")
    (ROOT / "reports/languages.md").write_text("\n".join(md))
    return "reports/languages.md written"
