"""Post-1 deterministic features (POST1-PLAN section 3). Pure functions, no I/O, no LLM.

1. tree_dump_spans(): directory-tree dumps = fenced code blocks with box-drawing tree characters (├── └── │) on 2+ lines, OR 8+
   consecutive path-like lines anywhere. Spans are merged; the caller counts files and tokens.
2. overview_sections(): sections whose heading matches overview|introduction|about|project structure|architecture|tech stack
   (a section runs to the next heading of the same or a higher level; nested matches are merged).
3. stack_members() / bridge_kind(): the instruction-file stack of a repo and 'bridge' files that only point at AGENTS.md.
"""
from __future__ import annotations

import re

TREE_CHARS = re.compile("[├└│]")  # ├ └ │
PATH_TOKEN = re.compile(r"^`?[\w@.+~-]+(?:/[\w@.+~-]*)*/?`?$")
KNOWN_EXT = {"py", "js", "ts", "tsx", "jsx", "md", "json", "toml", "yaml", "yml", "sh", "rs", "go", "java", "kt", "rb", "c", "cc",
             "cpp", "h", "hpp", "cs", "swift", "php", "css", "scss", "html", "sql", "txt", "cfg", "ini", "lock", "xml", "mdx", "vue", "svelte"}
COMMENT_START = re.compile(r"^(?:#|//|--|—|–|-|:|→|<-|->)\s")
OVERVIEW_RE = re.compile(r"\b(overview|introduction|about|project structure|architecture|tech(?:nology)?[ -]stack)\b", re.I)
MIN_RUN = 8


def _pathlike_line(line: str) -> bool:
    s = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?", "", line).strip()
    if not s or s.startswith(("```", "~~~", "#")):
        return False
    first, _, rest = s.partition(" ")
    if not PATH_TOKEN.match(first):
        return False
    core = first.strip("`")
    has_slash = "/" in core
    ext = core.rsplit(".", 1)[-1].lower() if "." in core.rsplit("/", 1)[-1] else ""
    if not (has_slash or ext in KNOWN_EXT):
        return False
    return not rest.strip() or bool(COMMENT_START.match(rest.strip() + " "))


def _fences(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end_exclusive) of fenced code blocks, fence lines included."""
    out, start = [], None
    for i, l in enumerate(lines):
        if l.lstrip().startswith(("```", "~~~")):
            if start is None:
                start = i
            else:
                out.append((start, i + 1))
                start = None
    if start is not None:
        out.append((start, len(lines)))
    return out


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def tree_dump_spans(text: str) -> list[tuple[int, int]]:
    """Line spans (start, end_exclusive, 0-based) of directory-tree dumps."""
    lines = text.splitlines()
    spans = []
    for a, b in _fences(lines):
        if sum(1 for l in lines[a:b] if TREE_CHARS.search(l)) >= 2:
            spans.append((a, b))
    run = 0
    for i, l in enumerate(lines + [""]):
        if i < len(lines) and _pathlike_line(l):
            run += 1
        else:
            if run >= MIN_RUN:
                spans.append((i - run, i))
            run = 0
    return _merge(spans)


def overview_sections(text: str) -> list[tuple[int, int, str]]:
    """(start, end_exclusive, heading) of sections whose heading matches the overview pattern; merged."""
    lines = text.splitlines()
    fenced = set()
    for a, b in _fences(lines):
        fenced.update(range(a, b))
    heads = []
    for i, l in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", l)
        if m and i not in fenced:
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for k, (i, lvl, title) in enumerate(heads):
        if not OVERVIEW_RE.search(title):
            continue
        end = len(lines)
        for j, l2, _ in heads[k + 1:]:
            if l2 <= lvl:
                end = j
                break
        out.append((i, end, title))
    merged = _merge([(a, b) for a, b, _ in out])
    titles = {}
    for a, b in merged:
        titles[(a, b)] = next(t for x, y, t in out if x == a)
    return [(a, b, titles[(a, b)]) for a, b in merged]


def span_text(text: str, spans) -> str:
    lines = text.splitlines()
    return "\n".join(l for a, b, *_ in spans for l in lines[a:b])


STACK_ORDER = ["CLAUDE.md", "AGENTS.md", "cursor rules", "copilot instructions", "GEMINI.md", ".windsurfrules"]


def stack_members(tree_paths) -> list[str]:
    """Distinct instruction files a repo has (presence anywhere for CLAUDE/AGENTS/GEMINI/.windsurfrules; canonical path for the rest)."""
    names, paths = set(), set(tree_paths)
    base = {p.rsplit("/", 1)[-1] for p in paths}
    out = []
    if "CLAUDE.md" in base:
        out.append("CLAUDE.md")
    if "AGENTS.md" in base:
        out.append("AGENTS.md")
    if any(p.startswith(".cursor/rules/") for p in paths) or ".cursorrules" in paths:
        out.append("cursor rules")
    if ".github/copilot-instructions.md" in paths:
        out.append("copilot instructions")
    if "GEMINI.md" in base:
        out.append("GEMINI.md")
    if ".windsurfrules" in base:
        out.append(".windsurfrules")
    return out


def bridge_kind(is_symlink: bool, symlink_target: str | None, text: str | None, target_name: str = "AGENTS.md"):
    """'symlink' if the file is a symlink to `target_name`; 'import' if it is only/mostly an `@AGENTS.md` import (everything else
    in the file, headings excluded, is at most 200 characters); else None."""
    if is_symlink:
        return "symlink" if symlink_target and target_name in symlink_target else None
    if text is None:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    imp = [l for l in lines if re.fullmatch(rf"@(?:\./)?{re.escape(target_name)}", l)]
    if not imp:
        return None
    rest = " ".join(l for l in lines if l not in imp and not l.startswith("#"))
    return "import" if len(rest) <= 200 else None
