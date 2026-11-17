"""Commands census (A2 addition) and skills extraction. Pure functions plus small table helpers. No network, no LLM.

normalize(): a shell command -> 'tool:subcommand' (e.g. `npm run test` -> npm:test, `uv run pytest` -> uv:pytest,
`cargo test --all` -> cargo:test, `pytest -x` -> pytest). Unknown tools return None (so prose does not enter the census).
"""
from __future__ import annotations

import re
from posixpath import basename, join, normpath

from .core import NEG_LINE, iter_lines

PKG = {"npm", "pnpm", "yarn", "bun"}
JS_VERBS = {"install", "i", "ci", "add", "remove", "update", "upgrade", "test", "start", "build", "dev", "lint", "publish",
            "init", "exec", "dlx", "audit", "outdated", "link", "pack", "create", "rebuild", "prune", "dedupe", "why",
            "ls", "list", "x", "format", "typecheck", "preview", "serve", "watch", "workspaces", "workspace"}
PYENV = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye", "conda", "mamba"}
SUBCMD_TOOLS = {"cargo", "go", "dotnet", "gradle", "gradlew", "mvn", "mvnw", "docker", "kubectl", "git", "gh", "terraform",
                "helm", "swift", "flutter", "dart", "bundle", "rake", "composer", "mix", "rails", "pip", "pip3", "deno",
                "brew", "apt", "apt-get", "xcodebuild", "fastlane", "aws", "gcloud", "az", "prisma", "drizzle-kit", "alembic",
                "ruff", "playwright", "next", "vite", "supabase", "wrangler", "vercel", "sam", "cdk", "sbt", "ant", "bazel",
                "ninja", "cmake", "ctest", "meson", "pod", "gem", "maturin", "pre-commit", "tox", "nox"}
TARGET_TOOLS = {"make", "just", "task", "nx", "turbo", "gulp", "grunt", "rake"}
BIN_TOOLS = {"pytest", "mypy", "black", "flake8", "pyright", "isort", "eslint", "prettier", "tsc", "vitest", "jest", "biome",
             "oxlint", "mocha", "cypress", "storybook", "webpack", "rollup", "esbuild", "tsx", "ts-node", "node", "bash", "sh",
             "zsh", "pylint", "bandit", "coverage", "rustc", "rustfmt", "clippy-driver", "golangci-lint", "gofmt", "swiftlint",
             "swiftformat", "ktlint", "detekt", "phpunit", "phpstan", "rspec", "rubocop", "dbt", "jupyter", "sphinx-build",
             "mkdocs", "uvicorn", "gunicorn", "flask", "django-admin", "celery", "streamlit", "npx", "bunx", "uvx", "pnpx"}
SCRIPT_EXT = re.compile(r"\.(sh|bash|py|js|mjs|cjs|ts|rb|pl)$")
INTERP = {"bash", "sh", "zsh", "python", "node", "ruby", "perl", "tsx", "ts-node", "bun", "deno"}
ALIAS = {"gradlew": "gradle", "mvnw": "mvn", "python3": "python", "py": "python", "pip3": "pip", "apt-get": "apt"}


def _args(toks: list[str]) -> list[str]:
    return [t for t in toks if not t.startswith("-") and "=" not in t.split("/")[-1][:1] and not t.startswith(("$", "<", "{"))]


def normalize(seg: str) -> str | None:
    toks = seg.strip().split()
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in ("sudo", "time", "env", "exec")):
        toks.pop(0)
    if not toks:
        return None
    first = toks[0]
    tool = basename(first.lstrip("./")) if first.startswith(("./", "../")) else first
    if first.startswith(("./", "../", "/")) and basename(first) not in ("gradlew", "mvnw"):
        return f"script:{basename(first)}" if SCRIPT_EXT.search(first) else None
    tool = ALIAS.get(tool, tool)
    rest = toks[1:]
    # "<interpreter> <script>" (bash/sh/python/node/uv run/...) -> one form: script:<basename>
    irest = rest[1:] if tool == "uv" and rest[:1] == ["run"] else rest
    if tool in INTERP or (tool == "uv" and irest is not rest):
        a = _args(irest)
        if tool == "deno" and a[:1] == ["run"]:
            a = a[1:]
        if a and SCRIPT_EXT.search(a[0]) and not (tool == "python" and "-m" in rest):
            return f"script:{basename(a[0])}"
        if tool in ("bash", "sh", "zsh", "node", "ruby", "perl"):
            return None
    if tool in PKG:
        a = _args(rest)
        if not a:
            return None
        if a[0] in ("run", "run-script") and len(a) > 1:
            return f"{tool}:{a[1]}"
        if a[0] in JS_VERBS or tool in ("yarn", "pnpm", "bun"):
            return f"{tool}:{a[0]}" if re.fullmatch(r"[\w:.-]+", a[0]) else None
        return None
    if tool in PYENV:
        a = _args(rest)
        if not a:
            return None
        if a[0] == "run" and len(a) > 1:
            return f"{tool}:{a[1]}"
        return f"{tool}:{a[0]}" if re.fullmatch(r"[\w-]+", a[0]) else None
    if tool in TARGET_TOOLS:
        a = [t for t in _args(rest) if re.fullmatch(r"[A-Za-z][\w.:-]*", t)]
        return f"{tool}:{a[0]}" if a else f"{tool}:(default)"
    if tool == "python":
        if "-m" in rest and rest.index("-m") + 1 < len(rest):
            mod = rest[rest.index("-m") + 1]
            return mod if mod in BIN_TOOLS else f"python:{mod}"
        a = _args(rest)
        return f"python:{basename(a[0])}" if a and a[0].endswith(".py") else None
    if tool in SUBCMD_TOOLS:
        a = _args(rest)
        return f"{tool}:{a[0]}" if a and re.fullmatch(r"[\w-]+", a[0]) else None
    if tool in ("npx", "bunx", "uvx", "pnpx"):
        a = _args(rest)
        return f"{tool}:{a[0]}" if a else None
    if tool in BIN_TOOLS:
        return tool
    return None


def extract_all(text: str) -> list[str]:
    """Every normalized command in a file (one entry per distinct occurrence kind), negated lines excluded."""
    out: list[str] = []
    for line, fence in iter_lines(text):
        if NEG_LINE.search(line):
            continue
        if fence:
            s = line.strip()
            if s.startswith(("```", "~~~", "#", "//")):
                continue
            strings = [s]
        else:
            strings = re.findall(r"`([^`\n]+)`", line)
        for s0 in strings:
            for seg in re.split(r"&&|\|\||;|\|", s0):
                seg = re.sub(r"^\s*(\$|>)\s+", "", seg.strip())
                seg = re.split(r"\s#\s", seg)[0]
                n = normalize(seg)
                if n:
                    out.append(n)
    return out


def extract_with_lines(text: str) -> list[tuple[str, int, str]]:
    """Like extract_all but returns (normalized command, 1-based line number, raw segment) for every occurrence."""
    out: list[tuple[str, int, str]] = []
    for i, (line, fence) in enumerate(iter_lines(text), 1):
        if NEG_LINE.search(line):
            continue
        if fence:
            s = line.strip()
            if s.startswith(("```", "~~~", "#", "//")):
                continue
            strings = [s]
        else:
            strings = re.findall(r"`([^`\n]+)`", line)
        for s0 in strings:
            for seg in re.split(r"&&|\|\||;|\|", s0):
                seg = re.sub(r"^\s*(\$|>)\s+", "", seg.strip())
                seg = re.split(r"\s#\s", seg)[0]
                n = normalize(seg)
                if n:
                    out.append((n, i, seg.strip()))
    return out


def toolchain(root_names: set[str]) -> str:
    """Primary toolchain of a repo from root files (priority order; 'mixed-js' if several JS lockfiles)."""
    js = [pm for pm, f in (("pnpm", {"pnpm-lock.yaml"}), ("yarn", {"yarn.lock"}), ("bun", {"bun.lockb", "bun.lock"}),
                           ("npm", {"package-lock.json"})) if root_names & f]
    if len(js) > 1:
        return "mixed-js"
    if js:
        return js[0]
    if "package.json" in root_names:
        return "npm"
    if "uv.lock" in root_names:
        return "uv"
    if "poetry.lock" in root_names:
        return "poetry"
    if "Cargo.toml" in root_names:
        return "cargo"
    if "go.mod" in root_names:
        return "go"
    if root_names & {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
        return "gradle"
    if "pom.xml" in root_names:
        return "maven"
    if root_names & {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}:
        return "python-other"
    if root_names & {"Package.swift"} or any(n.endswith(".xcodeproj") for n in root_names):
        return "swift"
    if root_names & {"Gemfile"}:
        return "ruby"
    if root_names & {"composer.json"}:
        return "php"
    if root_names & {"Makefile", "makefile", "justfile", "Justfile"}:
        return "make-only"
    return "none"


# ---------- skills ----------

SKILL_RE = re.compile(r"^\.claude/skills/([^/]+)/SKILL\.md$")


def frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}
    out: dict = {}
    key = None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", line)
        if kv:
            key, val = kv.group(1).lower(), kv.group(2).strip()
            out[key] = val
        elif key and line.strip().startswith("-"):
            out[key] = (out.get(key) or "") + " " + line.strip()[1:].strip()
    return out


def allowed_tools(fm: dict) -> list[str]:
    raw = fm.get("allowed-tools") or fm.get("allowed_tools") or ""
    toks = re.findall(r"[A-Za-z][\w]*(?:\([^)]*\))?", raw)
    return sorted({re.match(r"[A-Za-z]\w*", t).group(0) for t in toks})


def skill_script_refs(text: str) -> set[str]:
    """Scripts a skill points at: paths ending in .py/.sh/.js/.ts/.rb under scripts/ or relative ./x.ext."""
    refs = set()
    for line, _ in iter_lines(text):
        for m in re.finditer(r"(?<![\w/.-])((?:\./)?(?:[\w.-]+/)*[\w.-]+\.(?:py|sh|js|mjs|ts|rb))\b", line):
            p = m.group(1)
            if "/" in p or p.startswith("./"):
                if not p.startswith(("http", "www")):
                    refs.add(normpath(p))
    return refs


def resolve_skill_entries(tree: list[dict]) -> list[dict]:
    """Return [{'name', 'path', 'blob_sha', 'size', 'linked'}] for .claude/skills/*/SKILL.md, including skill directories that
    are symlinks (resolved by the caller using the link text via `resolve_link`)."""
    paths = {t["p"]: t for t in tree}
    out = []
    for p, t in paths.items():
        m = SKILL_RE.match(p)
        if m and t["t"] == "blob":
            out.append({"name": m.group(1), "path": p, "blob_sha": t["h"], "size": t["s"], "linked": False})
    return out


def symlinked_skill_dirs(tree: list[dict]) -> list[dict]:
    return [{"name": t["p"].split("/")[2], "link_path": t["p"], "link_sha": t["h"]} for t in tree
            if t["m"] == "120000" and re.fullmatch(r"\.claude/skills/[^/]+", t["p"])]


def resolve_link(link_path: str, target_text: str) -> str:
    return normpath(join(link_path.rsplit("/", 1)[0], target_text.strip()))
