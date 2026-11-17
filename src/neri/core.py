"""Pure, deterministic feature functions (A1-A4). No I/O, no LLM. Conservative by design: when a reference is
ambiguous it is NOT counted as dead (precision first; recall is traded away and reported as a limitation)."""
from __future__ import annotations

import fnmatch
import json
import re
from posixpath import basename, normpath

# ---------- text walking ----------

def iter_lines(text: str):
    """Yield (line, in_fence)."""
    fence = False
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            yield line, True
            continue
        yield line, fence


def shape(text: str) -> dict:
    heads = blocks = 0
    fence = False
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            blocks += 0 if fence else 1
            fence = not fence
        elif not fence and re.match(r"^#{1,6}\s", line):
            heads += 1
    return {"lines": len(text.splitlines()), "headings": heads, "code_blocks": blocks,
            "links": len(re.findall(r"\[[^\]]*\]\([^)\s]+\)", text)), "chars": len(text)}


# ---------- A2 dead paths ----------

KNOWN_EXT = {"py", "js", "ts", "tsx", "jsx", "mjs", "cjs", "md", "mdx", "json", "toml", "yaml", "yml", "sh", "bash", "zsh",
             "rs", "go", "java", "kt", "rb", "c", "cc", "cpp", "h", "hpp", "cs", "swift", "php", "css", "scss", "html",
             "sql", "txt", "cfg", "ini", "lock", "gradle", "xml", "proto", "tf", "dockerfile", "env", "csv", "ipynb"}
CODE_DIRS = {"src", "lib", "app", "apps", "packages", "pkg", "cmd", "internal", "test", "tests", "docs", "doc", "scripts",
             "tools", "bin", "config", "configs", "components", "pages", "public", "assets", "server", "client", "web",
             "api", "crates", "examples", "spec", "specs", "migrations", "models", "utils", "hooks", "services",
             ".claude", ".github", ".cursor", "backend", "frontend", "e2e", "fixtures", "templates", "plugins", "modules"}
FRAMEWORK_JS = {"node", "next", "vue", "react", "express", "three", "d3", "nuxt", "nest", "ember", "backbone", "angular",
                "svelte", "solid", "alpine", "deno", "bun", "p5", "chart", "anime", "socket", "moment", "lodash",
                "jquery", "handlebars", "ts-node", "chart.js", "electron", "gatsby", "remix", "astro"}
GENERATED_DIRS = {"node_modules", "dist", "build", "target", ".venv", "venv", "__pycache__", ".next", "coverage", "out",
                  ".cache", ".turbo", ".nuxt", ".git", "vendor", ".gradle", ".pytest_cache", ".mypy_cache", "htmlcov",
                  "tmp", "temp", "logs", ".idea", ".vscode"}
SKIP_LINE = re.compile(
    r"\b(create[sd]?|creating|mkdir|touch|generat\w*|will be (created|generated)|outputs?|new file|e\.g\.?|i\.e\.|"
    r"for example|for instance|example|such as|copy|copied|cp|mv|rename[sd]?|move[sd]? to|download\w*|clone[sd]?|"
    r"upstream|external|third-party|vendored|submodule|sibling|other repo|optional|if present|if it exists|"
    r"locally|per-user|user-specific|private|persist\w*|cache[sd]?|state|runtime|at runtime|installed? (?:at|to|into)|"
    r"(?:writ(?:e|es|ing)|sav(?:e|es|ing)|stor(?:e|es|ing)|append\w*|logs?|dump\w*)\b[^`]{0,40}\b(?:to|into|in))\b"
    r"|\u2192|->|=>|\u2500\u2500\u25ba|https?://github\.com/|\w's\s+`", re.I)
NEG_LINE = re.compile(r"\b(never|don'?t|do not|avoid|instead of|rather than|deprecated|no longer|legacy|old)\b", re.I)


def gitignore_patterns(text: str | None) -> list[str]:
    if not text:
        return []
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#") and not l.startswith("!")]


def is_ignored(path: str, patterns: list[str]) -> bool:
    parts = path.split("/")
    if any(p in GENERATED_DIRS for p in parts):
        return True
    for pat in patterns:
        p = pat.rstrip("/")
        anchored = p.startswith("/") or "/" in p
        p = p.lstrip("/")
        if anchored:
            if fnmatch.fnmatch(path, p) or path.startswith(p.rstrip("*") + "/") or fnmatch.fnmatch(path, p + "/*"):
                return True
        elif any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


def _clean_token(tok: str) -> str:
    tok = tok.strip().strip("\"'`()[]<>,;:")
    tok = re.sub(r"[.,;:!?)]+$", "", tok)
    tok = re.sub(r"#.*$", "", tok)               # anchor
    tok = re.sub(r":\d+(-\d+)?$", "", tok)       # :line suffix
    return tok


PLACEHOLDER_STEMS = {"xx", "xxx", "nnn", "yyyy", "name", "filename", "file", "foo", "bar", "baz", "example", "sample", "pascalcase",
                     "camelcase", "kebab-case", "snake_case", "todo", "your-file", "my-file", "nom", "mon", "result", "results"}


def _is_placeholder(path: str) -> bool:
    if "path/to" in path:
        return True
    for seg in path.split("/"):
        stem = seg.split(".")[0].lower()
        if stem in PLACEHOLDER_STEMS or re.match(r"^(xx+|nnn+|yyyy|your[-_]|my[-_]|nom[-_]|mon[-_]|some[-_]|test[-_]file)", stem) or seg.isdigit() or \
                re.search(r"[-_](name|xx+|nnn+)$", stem) or re.match(r"^(xx+|nnn+)[-_]", stem):
            return True
    return False


def _path_like(tok: str) -> str | None:
    """Return a normalized relative path if tok is a confident path reference, else None."""
    if not tok or "://" in tok or tok.startswith(("-", "$", "~", "/", "@", "#", "http", "www.")) or re.match(r"^[A-Za-z]:", tok):
        return None
    if re.search(r"[<>{}*?\[\]|=()!,\"'`\\ ]|\.\.\.|%|\$", tok) or "@" in tok:
        return None
    if tok.startswith("../"):
        return None
    tok = re.sub(r"^\./", "", tok)
    if not tok or tok in {".", ".."} or re.match(r"^(path/to|your[-_/]|my[-_/]|foo/|bar/|example/|x/y)", tok):
        return None
    is_dir = tok.endswith("/")
    path = tok.rstrip("/")
    if _is_placeholder(path):
        return None
    if re.match(r"^\.(cline|opencode|cursor|windsurf|agents|gemini|codex|aider|roo|kiro|trae|continue|zed|vscode)\b", path):
        return None
    last = path.split("/")[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    has_ext = ext in KNOWN_EXT and len(last) > len(ext) + 1
    if "/" not in path:
        return None  # bare filenames: recall traded for precision (runtime artifacts, examples, system headers)
    if not (has_ext or is_dir):
        return None  # extensionless slash tokens are routes, identifiers or ranges
    if not (has_ext or path.split("/")[0] in CODE_DIRS):
        return None
    return path


def extract_path_refs(text: str) -> list[dict]:
    from . import classify
    return classify.extract_path_refs(text)


def path_exists(ref: str, tree: set[str], basenames: set[str], dirs: set[str]) -> bool:
    if ref in tree or ref in dirs:
        return True
    parts = ref.split("/")
    if any("/".join(parts[:i]) in tree for i in range(1, len(parts))):  # symlinked dir / submodule on the way
        return True
    if "/" not in ref:
        return ref in basenames
    suffix = "/" + ref
    return any(t.endswith(suffix) for t in tree) or any(d.endswith(suffix) for d in dirs)


TIER_VERSION = "moved-v3"  # FROZEN before the fresh M5 sheet is drawn; changing it invalidates that sheet
GENERIC_BASENAMES = {
    "readme.md", "readme", "license", "license.md", "changelog.md", "contributing.md", "agents.md", "claude.md", "gemini.md",
    "security.md", "code_of_conduct.md", "skill.md", "package.json", "package-lock.json", "pyproject.toml", "cargo.toml",
    "cargo.lock", "go.mod", "go.sum", "makefile", "dockerfile", "docker-compose.yml", ".gitignore", ".env", ".env.example",
    "tsconfig.json", "config.json", "settings.json", "index.md", "index.ts", "index.js", "index.tsx", "index.html",
    "main.py", "main.ts", "main.rs", "main.go", "mod.rs", "lib.rs", "__init__.py", "setup.py", "conftest.py",
    "getting-started.md", "installation.md", "architecture.md", "overview.md"}


def dead_paths(text: str, tree_paths, gitignore: str | None = None, root_imports_ok: bool = True) -> list[dict]:
    """Verifiable path references (category ALIVE or STALE); see classify.py for every other category."""
    from . import classify
    return classify.checked(classify.classify_paths(text, tree_paths, gitignore))


def moved_tier(ref: str, tree_paths) -> bool:
    from . import classify
    return classify.TreeIndex(tree_paths).moved(ref)[0] is not None


def _parents(p: str):
    parts = p.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        yield "/".join(parts[:i])


# ---------- A2 dead commands ----------

PY_KNOWN = {"python", "python3", "pip", "pytest", "ruff", "mypy", "black", "flake8", "pyright", "isort", "coverage",
            "sphinx-build", "mkdocs", "uvicorn", "gunicorn", "jupyter", "ipython", "tox", "nox", "pre-commit", "alembic",
            "django-admin", "flask", "celery", "hatch", "build", "twine", "streamlit", "fastapi", "pylint", "bandit",
            "pyinstaller", "maturin", "cython", "poetry", "uv", "pdm", "pip-compile", "ty", "pyrefly", "pytest-cov", "inv", "invoke", "fab", "sphinx-apidoc"}
SCOPE_FLAGS = re.compile(r"(--prefix|--workspace|--filter|--cwd|--dir|\s-C\s|\s-w\s|\s-F\s|--project|--directory|\bcd\s)")
CMD_RES = [
    ("pkg", re.compile(r"(npm|pnpm|yarn|bun)\s+run\s+([\w:.@/-]+)")),
    ("make", re.compile(r"make\s+(?!-)([A-Za-z][\w.-]*)")),
    ("just", re.compile(r"just\s+(?!-)([A-Za-z][\w-]*)")),
    ("py", re.compile(r"(uv|poetry|pipenv|hatch|pdm)\s+run\s+(?!-)([\w./-]+)")),
]


CD_LINE = re.compile(r"^\s*(?:\$\s*)?(?:cd|pushd)\s+\S")


def extract_commands(text: str) -> list[dict]:
    from . import classify
    return classify.extract_commands(text)


def make_targets(text: str):
    """Return (names, patterns) or None when we cannot tell (includes)."""
    if re.search(r"^\s*-?include\s", text, re.M):
        return None
    names, pats = set(), []
    for m in re.finditer(r"^([A-Za-z0-9_.%/ -]+?)\s*:(?![=:])", text, re.M):
        for name in m.group(1).split():
            if "%" in name:
                pats.append(name)
            else:
                names.add(name)
    for m in re.finditer(r"^\.PHONY\s*:\s*(.+)$", text, re.M):
        names.update(m.group(1).split())
    return names, pats


def just_recipes(text: str):
    """Recipe names, or None if the justfile uses mod/import (recipes live elsewhere)."""
    if re.search(r"^(mod|import)\s", text, re.M):
        return None
    r = set()
    for line in text.splitlines():
        m = re.match(r"^@?([A-Za-z_][\w-]*)(.*)$", line)
        if not m:
            continue
        if m.group(1) in ("set", "export", "alias", "import", "mod"):
            a = re.match(r"^alias\s+([\w-]+)\s*:=", line)
            if a:
                r.add(a.group(1))
            continue
        rest = m.group(2).lstrip()
        if rest.startswith(":=") or rest.startswith("="):
            continue  # variable assignment
        if re.search(r":(?!=)", m.group(2)):
            r.add(m.group(1))
    return r


def pyproject_scripts(text: str) -> set[str]:
    names = set()
    for sec in re.finditer(r"^\[(project\.scripts|tool\.poetry\.scripts|tool\.pdm\.scripts|tool\.hatch\.envs\.[\w.-]+\.scripts)\]\s*$(.*?)(?=^\[|\Z)",
                           text, re.M | re.S):
        names.update(re.findall(r"^\s*([A-Za-z0-9_.-]+)\s*=", sec.group(2), re.M))
    return names


def dead_commands(text: str, tree_paths, manifests: dict[str, str]) -> list[dict]:
    """Verifiable commands (category ALIVE or STALE), resolved against the root manifests."""
    from . import classify
    return classify.checked(classify.classify_commands(text, tree_paths, manifests))


# ---------- A2 toolchain mismatch ----------

PM_LOCK = {"npm": {"package-lock.json"}, "pnpm": {"pnpm-lock.yaml"}, "yarn": {"yarn.lock"}, "bun": {"bun.lockb", "bun.lock"}}


def toolchain_mismatch(text: str, root_names) -> dict:
    names = set(root_names)
    repo_pm = [pm for pm, locks in PM_LOCK.items() if names & locks]
    mentioned = set()
    for line, _ in iter_lines(text):
        if NEG_LINE.search(line) or re.search(r"\bnot\s+(npm|yarn|pnpm|bun|pip)\b", line, re.I):
            continue
        for pm in ("npm", "pnpm", "yarn", "bun"):
            if re.search(rf"(?<![\w/-]){pm}\s+(install|i|ci|run|add|exec|test|start|build|dev)\b", line):
                mentioned.add(pm)
    js = bool(len(repo_pm) == 1 and mentioned and repo_pm[0] not in mentioned)
    py_repo = [t for t, f in (("uv", "uv.lock"), ("poetry", "poetry.lock")) if f in names]
    pip_ment = False
    uv_poetry_ment = False
    for line, _ in iter_lines(text):
        if NEG_LINE.search(line):
            continue
        if re.search(r"(?<![\w-])(pip3?|python3? -m pip)\s+install\b", line):
            pip_ment = True
        if re.search(r"(?<![\w-])(uv|poetry)\s+(run|sync|add|install|lock)\b", line):
            uv_poetry_ment = True
    py = bool(len(py_repo) == 1 and pip_ment and not uv_poetry_ment)
    return {"js_mismatch": js, "py_mismatch": py, "mentioned_js": sorted(mentioned), "repo_pm": repo_pm, "repo_py": py_repo}


# ---------- A3 style ----------

SHOUT = re.compile(r"\b(IMPORTANT|CRITICAL|MUST|NEVER|ALWAYS)\b")
NEGS = re.compile(r"\b(never|don'?t|do not|avoid|must not|should not|shouldn'?t)\b", re.I)
IMPERATIVES = {"use", "run", "avoid", "never", "always", "keep", "write", "add", "prefer", "ensure", "make", "check",
               "follow", "do", "don't", "create", "update", "test", "install", "import", "return", "name", "put",
               "place", "document", "commit", "push", "open", "read", "call", "define", "set", "specify", "wrap",
               "remember", "treat", "build", "avoid", "validate", "handle", "format", "lint", "store", "load", "pass",
               "mock", "assert", "log", "prefix", "group", "extend", "delete", "remove", "move", "rename", "verify",
               "implement", "include", "exclude", "limit", "require", "separate", "split", "sort", "type", "tag"}


def style(text: str, tokens: int) -> dict:
    shout = 0
    caps_lines = 0
    rules = neg = imp = bullets = 0
    for line, fence in iter_lines(text):
        if fence:
            continue
        shout += len(SHOUT.findall(line))
        letters = re.sub(r"[^A-Za-z]", "", line)
        if len(letters) >= 10 and letters.isupper() and not line.lstrip().startswith(("#", "|")):
            caps_lines += 1
        m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            bullets += 1
            rules += 1
            body = re.sub(r"[*_`]", "", m.group(1)).strip()
            first = body.split(" ", 1)[0].lower().strip(":,.")
            if first in IMPERATIVES:
                imp += 1
            if NEGS.search(body):
                neg += 1
    k = max(tokens, 1) / 1000
    return {"shout_per_1k": (shout + caps_lines) / k, "shout_words": shout, "caps_lines": caps_lines,
            "bullets": bullets, "negation_share": neg / rules if rules else None,
            "imperative_share": imp / bullets if bullets else None}


# ---------- A4 drift ----------

def _norm_lines(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", l.strip().lower()) for l in text.splitlines() if l.strip()}


def line_jaccard(a: str, b: str) -> float:
    A, B = _norm_lines(a), _norm_lines(b)
    return len(A & B) / len(A | B) if A | B else 1.0


TEST_RE = re.compile(r"\b(npm test|npm run test\S*|pnpm (?:run )?test\S*|yarn test\S*|bun test\S*|pytest\S*|uv run pytest\S*|"
                     r"python -m pytest\S*|cargo test\S*|go test\S*|make test\S*|just test\S*|jest\S*|vitest\S*|mvn test|gradle test)")
BUILD_RE = re.compile(r"\b(npm run build\S*|pnpm (?:run )?build\S*|yarn build\S*|bun run build\S*|cargo build\S*|go build\S*|"
                      r"make build\S*|make\b(?! \S)|just build\S*|mvn (?:package|install)|gradle build|uv build)")


def _cmds(text: str, rx) -> set[str]:
    out = set()
    for line, fence in iter_lines(text):
        for s in ([line] if fence else re.findall(r"`([^`]+)`", line)):
            out.update(m.group(0).strip() for m in rx.finditer(s))
    return out


def drift(a: str, b: str, a_imports_b: bool = False, b_imports_a: bool = False, symlink: bool = False) -> dict:
    na, nb = re.sub(r"\s+", " ", a.strip()), re.sub(r"\s+", " ", b.strip())
    if symlink:
        kind = "symlink"
    elif na == nb:
        kind = "identical"
    elif a_imports_b or b_imports_a:
        kind = "imports"
    else:
        kind = "divergent"
    out = {"kind": kind, "jaccard": line_jaccard(a, b) if kind == "divergent" else None,
           "test_contradiction": None, "build_contradiction": None}
    if kind == "divergent":
        for key, rx in (("test_contradiction", TEST_RE), ("build_contradiction", BUILD_RE)):
            ta, tb = _cmds(a, rx), _cmds(b, rx)
            out[key] = bool(ta and tb and not (ta & tb))
    return out


# ---------- truncated trees ----------

def apply_truncation(refs: list[dict], resolved: dict) -> list[dict]:
    """The git tree of this repo was TRUNCATED (GitHub cuts the recursive listing), so the partial listing cannot prove
    that anything is missing. A ref the partial listing calls dead becomes alive if a non-recursive directory walk found
    it (resolved[ref] is True); otherwise it is UNKNOWN (never dead), because absence is unproven and the suffix/moved
    rules cannot be evaluated against an incomplete listing. Unknown refs are excluded from every wrongness number."""
    out = []
    for r in refs:
        if r.get("dead"):
            found = resolved.get(r.get("ref") or r.get("target")) is True
            r = {**r, "dead": False, "moved": False, "unknown": not found,
                 "category": "ALIVE" if found else "UNKNOWN", "reason": "truncated_tree"}
        else:
            r = {**r, "unknown": bool(r.get("unknown", False))}
        out.append(r)
    return out
