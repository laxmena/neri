"""A2 reference classification. Every reference-like string in a root agent file gets exactly ONE category:

  ALIVE             the target exists in the git tree at the pinned sha
  STALE             expected to exist in THIS repo now, and missing (includes moved/renamed targets and case mismatches)
  EXPECTED_CREATED  per-user/setup files, build or runtime outputs, "copy X to Y" targets, ignored/generated paths
  NOT_A_REFERENCE   examples, placeholders, patterns/wildcards, conditionals, negated or historical mentions, other projects
  UNKNOWN           cannot be verified (truncated tree, symlink or submodule on the path, `cd` into an unindexed folder, ambiguity)

Only STALE counts toward the wrongness headline; every other category is reported separately. Pure functions, no I/O.
Context (`cd`) is honoured: a `cd <dir>` earlier in the same code block or line changes the working directory for the
paths and commands that follow, and commands are resolved against the manifests of THAT directory.
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections import Counter
from posixpath import basename, join, normpath

from . import core

ALIVE, STALE, CREATED, NOTREF, UNKNOWN = "ALIVE", "STALE", "EXPECTED_CREATED", "NOT_A_REFERENCE", "UNKNOWN"
CATEGORIES = (STALE, CREATED, NOTREF, UNKNOWN)

CTX = [  # (reason, regex, category): the first match on a line decides
    ("create", re.compile(
        r"\b(create[sd]?|creating|mkdir|touch|generat\w*|will be (?:created|generated)|new file|copy|copied|cp|mv|"
        r"rename[sd]?|move[sd]? to|download\w*|clone[sd]?|installed? (?:at|to|into)|"
        r"outputs?\s+(?:to|goes|files?|dir\w*|folder|path)|"
        r"(?:writ(?:e|es|ing)|sav(?:e|es|ing)|stor(?:e|es|ing)|append\w*|dump\w*)\b[^`]{0,40}\b(?:to|into|in))\b"
        r"|\u2192|->|=>|\u2500\u2500\u25ba", re.I), CREATED),
    ("runtime", re.compile(r"\b(persist\w*|per-user|user-specific|at runtime|runtime (?:file|state|artifact)s?)\b", re.I), CREATED),
    ("example", re.compile(r"\b(e\.g\.?|i\.e\.|for example|for instance|example|such as|single test|one test|specific test)\b", re.I), NOTREF),
    ("conditional", re.compile(r"\b(optional|if present|if it exists|if exists|if any|when present|when it exists|"
                               r"if available|when available|if you have)\b", re.I), NOTREF),
    ("other_project", re.compile(
        r"\b(upstream|external|third-party|vendored|submodule|sibling|other repo)\b|https?://github\.com/|\w's\s+`|"
        r"\b(?:user'?s?|your|the target|target)\s+(?:project|repo|repository|codebase)\b", re.I), NOTREF),
    ("negated", core.NEG_LINE, NOTREF),
]
DIAGRAM = re.compile("[├└│─┬]")
CD_RE = re.compile(r"^\s*(?:\$\s*)?(?:cd|pushd)\s+(\S+)")
SCOPE = re.compile(r"(--prefix|--workspace|--filter|--cwd|--dir|\s-C\s|\s-w\s|\s-F\s|--project|--directory)")
STAND_IN = {"specific", "something", "anything", "whatever", "placeholder", "dummy", "demo", "sample", "example", "name"}
GLOB = re.compile(r"[<>{}*?\[\]|=()!,\"'`\\ ]|\.\.\.|%|\$")
OTHER_TOOL_DIR = re.compile(r"^\.(cline|opencode|cursor|windsurf|agents|gemini|codex|aider|roo|kiro|trae|continue|zed|vscode)\b")
SUFFIX_LENIENT = True  # a path that exists under another prefix (monorepo package) counts as alive


def line_ctx(line: str):
    if DIAGRAM.search(line):
        return ("diagram", NOTREF)
    for name, rx, cat in CTX:
        if rx.search(line):
            return (name, cat)
    return None


def apply_cd(cwd, d: str):
    """New working directory after `cd d` (relative to the repo root), or None when it cannot be resolved."""
    if cwd is None:
        return None
    d = d.strip("\"'")
    if not d or d in ("~", "-") or d.startswith(("/", "~", "$")) or "$" in d or "`" in d:
        return None
    new = normpath(join(cwd or ".", d))
    if new.startswith(".."):
        return None
    return "" if new == "." else new


def path_status(tok: str):
    """('path', p) | ('pattern', tok) | ('placeholder', p) | ('other_tool', p) | ('no', None)."""
    if not tok or "://" in tok or tok.startswith(("-", "$", "~", "/", "@", "#", "http", "www.")) or re.match(r"^[A-Za-z]:", tok) or "@" in tok:
        return ("no", None)
    if GLOB.search(tok):
        return ("pattern", tok) if "/" in tok and not tok.startswith("../") else ("no", None)
    if tok.startswith("../"):
        return ("no", None)
    tok = re.sub(r"^\./", "", tok)
    if not tok or tok in {".", ".."}:
        return ("no", None)
    path = tok.rstrip("/")
    is_dir = tok.endswith("/")
    last = path.split("/")[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    has_ext = ext in core.KNOWN_EXT and len(last) > len(ext) + 1
    if "/" not in path:
        return ("no", None)  # bare filenames: too often runtime artifacts, examples or system files
    if not (has_ext or is_dir):
        return ("no", None)
    if not (has_ext or path.split("/")[0] in core.CODE_DIRS):
        return ("no", None)
    if re.match(r"^(path/to|your[-_/]|my[-_/]|foo/|bar/|example/|x/y)", path) or core._is_placeholder(path) \
            or any(seg.split(".")[0].lower() in STAND_IN for seg in path.split("/")) or re.search(r"\bv?X\.Y\.Z\b", path):
        return ("placeholder", path)
    if OTHER_TOOL_DIR.match(path):
        return ("other_tool", path)
    return ("path", path)


# ---------- extraction ----------

def _split(s: str) -> list[str]:
    return re.split(r"&&|\|\||;|\|", s)


def extract_path_refs(text: str) -> list[dict]:
    """All path-like references with line numbers, working directory and context. status != 'path' means it is a pattern,
    placeholder or another tool's folder (reported as NOT_A_REFERENCE)."""
    refs, seen = [], set()
    cwd: str | None = ""
    for i, (line, fence) in enumerate(core.iter_lines(text), 1):
        if line.lstrip().startswith(("```", "~~~")) or not fence:
            cwd = ""
        ctx = line_ctx(line)
        units: list[tuple[str, str]] = []
        if fence:
            s = line.strip()
            if s.startswith(("```", "~~~", "#")):
                continue
            s = re.split(r"\s(?:#|//)\s", s)[0]
            units.append(("code", s))
        else:
            units += [("backtick", u) for u in re.findall(r"`([^`]+)`", line)]
        for kind, u in units:
            for seg in _split(u):
                m = CD_RE.match(seg)
                if m:
                    cwd = apply_cd(cwd, m.group(1))
                    continue
                toks = seg.split()
                if kind == "code":
                    toks = [t for t in toks if "/" in t and (t.rstrip(",;:").endswith("/") or re.search(r"\.\w{1,5}$", t.rstrip(",;:")))]
                for raw in toks:
                    if "..." in raw or "…" in raw:
                        continue
                    st, p = path_status(core._clean_token(raw))
                    if st == "no":
                        continue
                    key = (kind, p, cwd, i)
                    if key not in seen:
                        seen.add(key)
                        refs.append({"kind": kind, "ref": p, "line": i, "cwd": cwd, "status": st, "ctx": ctx})
        if not fence:
            for m in re.findall(r"\]\(([^)\s]+)\)", line):
                st, p = path_status(core._clean_token(m))
                if st != "no" and ("link", p, "", i) not in seen:
                    seen.add(("link", p, "", i)); refs.append({"kind": "link", "ref": p, "line": i, "cwd": "", "status": st, "ctx": ctx})
            if not re.match(r"^\s*@(import|use|forward|tailwind|apply|media|layer|param|returns?|type|see|example)\b", line):
                for m in re.findall(r"(?<![\w/@.`\"'])@([\w.][\w./+-]*)", line):
                    if m.split("/")[0] in core.CODE_DIRS or m.startswith("."):
                        st, p = path_status(core._clean_token(m))
                        if st != "no" and ("import", p, "", i) not in seen:
                            seen.add(("import", p, "", i)); refs.append({"kind": "import", "ref": p, "line": i, "cwd": "", "status": st, "ctx": ctx})
    return refs


class TreeIndex:
    def __init__(self, tree_paths):
        t = set(tree_paths)
        self.tree = t | {re.sub(r"/\([^/)]+\)", "", p) for p in t if "(" in p}  # framework route groups
        self.dirs = {d for p in self.tree for d in core._parents(p)}
        self.base = Counter(basename(p) for p in self.tree)
        self.lower = {p.lower(): p for p in self.tree}
        self.stem: dict[str, list[str]] = {}
        self.dir_stem: dict[tuple, list[str]] = {}
        for p in self.tree:
            b = basename(p)
            if "." in b:
                stem = b.rsplit(".", 1)[0]
                self.stem.setdefault(stem.lower(), []).append(p)
                self.dir_stem.setdefault((p.rsplit("/", 1)[0] if "/" in p else "", stem), []).append(p)

    def exists(self, full: str) -> str | None:
        """'exact' | 'suffix' | 'link' (an ancestor is a symlink or submodule entry) | None."""
        if full in self.tree or full in self.dirs:
            return "exact"
        parts = full.split("/")
        if any("/".join(parts[:i]) in self.tree for i in range(1, len(parts))):
            return "link"
        suffix = "/" + full
        if SUFFIX_LENIENT and (any(t.endswith(suffix) for t in self.tree) or any(d.endswith(suffix) for d in self.dirs)):
            return "suffix"
        return None

    def moved(self, full: str):
        """moved-v3 evidence: (moved_to or None, evidence dict). Sources, in order: letter case, same dir + other extension,
        a unique NON-generic basename elsewhere, a unique NON-generic stem (ignoring extension) anywhere."""
        b = basename(full)
        stem = b.rsplit(".", 1)[0] if "." in b else b
        d = full.rsplit("/", 1)[0] if "/" in full else ""
        ev = {"case_insensitive": self.lower.get(full.lower()), "same_dir_other_ext": [p for p in self.dir_stem.get((d, stem), []) if p != full][:3],
              "same_basename": [p for p in self.tree if basename(p) == b][:3] if self.base.get(b) else [],
              "same_stem": self.stem.get(stem.lower(), [])[:3]}
        to = None
        if ev["case_insensitive"] and ev["case_insensitive"] != full:
            to = ev["case_insensitive"]
        elif ev["same_dir_other_ext"]:
            to = ev["same_dir_other_ext"][0]
        elif b.lower() not in core.GENERIC_BASENAMES and self.base.get(b, 0) == 1:
            to = ev["same_basename"][0]
        elif "." in b and stem.lower() not in core.GENERIC_BASENAMES and stem.lower() not in {g.split(".")[0] for g in core.GENERIC_BASENAMES} \
                and len(self.stem.get(stem.lower(), [])) == 1:
            to = self.stem[stem.lower()][0]
        return to, ev


def classify_paths(text: str, tree_paths, gitignore: str | None = None) -> list[dict]:
    idx = TreeIndex(tree_paths)
    pats = core.gitignore_patterns(gitignore)
    out = []
    for r in extract_path_refs(text):
        rec = {"kind": r["kind"], "ref": r["ref"], "line": r["line"], "cwd": r["cwd"], "moved": False, "moved_to": None, "evidence": {}}
        full = None
        if r["status"] != "path":
            cat, why = NOTREF, r["status"]
        elif r["cwd"] is None:
            cat, why = UNKNOWN, "cd_unresolved"
        elif r["ctx"]:
            why, cat = r["ctx"]
        else:
            full = normpath(join(r["cwd"], r["ref"])) if r["cwd"] else r["ref"]
            if core.is_ignored(full, pats):
                cat, why = CREATED, "ignored_or_generated"
            else:
                ex = idx.exists(full)
                if ex in ("exact", "suffix"):
                    cat, why = ALIVE, ex
                elif ex == "link":
                    cat, why = UNKNOWN, "symlink_or_submodule"
                else:
                    cat, why = STALE, "missing"
                    to, ev = idx.moved(full)
                    rec["moved"], rec["moved_to"], rec["evidence"] = to is not None, to, ev
        rec.update(category=cat, reason=why, dead=cat == STALE, unknown=cat == UNKNOWN, full=full)
        out.append(rec)
    return out


# ---------- commands ----------

def extract_commands(text: str) -> list[dict]:
    out, seen = [], set()
    cwd: str | None = ""
    for i, (line, fence) in enumerate(core.iter_lines(text), 1):
        if line.lstrip().startswith(("```", "~~~")) or not fence:
            cwd = ""
        if fence:
            s = line.strip()
            if s.startswith(("```", "~~~", "#", "//")):
                continue
            strings = [s]
        else:
            strings = re.findall(r"`([^`\n]+)`", line)
        ctx = line_ctx(line)
        for s0 in strings:
            for seg in _split(s0):
                seg = re.sub(r"^\s*(\$|>)\s+", "", seg.strip())
                seg = re.sub(r"^\s*sudo\s+", "", seg)
                seg = re.split(r"\s#\s", seg)[0]
                m = CD_RE.match(seg)
                if m:
                    cwd = apply_cd(cwd, m.group(1))
                    continue
                for kind, rx in core.CMD_RES:
                    mm = rx.match(seg)
                    if not mm:
                        continue
                    tgt = mm.group(mm.lastindex)
                    tool = mm.group(1) if kind in ("pkg", "py") else kind
                    nxt = seg[mm.end():mm.end() + 1]
                    reason = None
                    if nxt in ("*", "?", "[") or tgt.endswith(("-", "_")):
                        reason = "pattern"
                    elif re.search(r"[<{$]", seg):
                        reason = "placeholder"
                    key = (kind, tool, tgt, cwd, i)
                    if key not in seen:
                        seen.add(key)
                        out.append({"kind": kind, "tool": tool, "target": tgt, "line": i, "cwd": cwd, "ctx": ctx,
                                    "pre": reason, "scoped": bool(SCOPE.search(seg))})
    return out


def _eval(c: dict, tree: set, man: dict):
    """True = definitely missing, False = defined, None = cannot tell. `man`: manifest name -> text for the target dir."""
    pre = c["cwd"] or ""
    names = {p[len(pre) + 1 if pre else 0:] for p in tree if (not pre or p.startswith(pre + "/"))}
    rootnames = {n for n in names if "/" not in n}
    k, tgt = c["kind"], c["target"]
    if k == "pkg":
        pj = man.get("package.json")
        if pj is None:
            return None
        try:
            data = json.loads(pj)
        except Exception:
            return None
        if "workspaces" in data or "pnpm-workspace.yaml" in rootnames or any(n.endswith("/package.json") for n in names):
            return None
        return tgt not in (data.get("scripts") or {})
    if k == "make":
        mk = next((man[n] for n in ("Makefile", "makefile", "GNUmakefile") if n in man), None)
        if mk is None:
            if pre:
                return None
            return True if not any(basename(p) in ("Makefile", "makefile", "GNUmakefile") or p.endswith(".mk") for p in tree) else None
        tg = core.make_targets(mk)
        if tg is None:
            return None
        names_, pats = tg
        return tgt not in names_ and not any(fnmatch.fnmatch(tgt, q.replace("%", "*")) for q in pats)
    if k == "just":
        jf = man.get("justfile") or man.get("Justfile")
        if jf is None:
            if pre:
                return None
            return True if not any(basename(p).lower() == "justfile" for p in tree) else None
        rc = core.just_recipes(jf)
        return None if rc is None else tgt not in rc
    if k == "py":
        pp = man.get("pyproject.toml")
        if pp is None or tgt.endswith(".py") or "/" in tgt or tgt in core.PY_KNOWN or any(n.endswith("/pyproject.toml") for n in names):
            return None
        return not (tgt in core.pyproject_scripts(pp) or re.search(r"(?<![\w-])" + re.escape(tgt) + r"(?![\w-])", pp))
    return None


def classify_commands(text: str, tree_paths, manifests: dict, subdir_manifests: dict | None = None) -> list[dict]:
    """manifests: root manifest name -> text. subdir_manifests: dir -> {manifest name -> text} (for `cd <dir>` contexts)."""
    tree = set(tree_paths)
    out = []
    for c in extract_commands(text):
        rec = {k: c[k] for k in ("kind", "tool", "target", "line", "cwd")}
        rec["evidence"] = {}
        if c["pre"]:
            cat, why = NOTREF, c["pre"]
        elif c["ctx"]:
            why, cat = c["ctx"]
        elif c["cwd"] is None:
            cat, why = UNKNOWN, "cd_unresolved"
        elif c["scoped"]:
            cat, why = UNKNOWN, "scoped_to_other_dir"
        else:
            man = manifests if not c["cwd"] else (subdir_manifests or {}).get(c["cwd"])
            if man is None:
                cat, why = UNKNOWN, "cd_dir_not_indexed"
            else:
                rec["evidence"] = {"manifests_checked": sorted(man), "dir": c["cwd"] or "."}
                dead = _eval(c, tree, man)
                cat, why = (UNKNOWN, "ambiguous") if dead is None else ((STALE, "undefined_target") if dead else (ALIVE, "defined"))
        rec.update(category=cat, reason=why, dead=cat == STALE, unknown=cat == UNKNOWN, moved=False)
        out.append(rec)
    return out


def cd_dirs(text: str) -> set[str]:
    """Directories (relative to the repo root) that commands in this file are run from after a `cd`."""
    return {c["cwd"] for c in extract_commands(text) if c["cwd"]}


def checked(refs: list[dict]) -> list[dict]:
    """The refs that were actually verifiable and reference-like: ALIVE or STALE."""
    return [r for r in refs if r["category"] in (ALIVE, STALE)]
