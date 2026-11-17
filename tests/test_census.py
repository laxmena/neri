import pytest

from neri import census

CASES = {
    "npm run test -- --watch": "npm:test", "npm test": "npm:test", "pnpm install": "pnpm:install", "yarn dev": "yarn:dev",
    "uv run pytest -x": "uv:pytest", "uv sync": "uv:sync", "cargo test --all": "cargo:test", "make build": "make:build",
    "just": "just:(default)", "python -m pytest tests": "pytest", "python -m build": "python:build", "pytest -q": "pytest",
    "docker compose up -d": "docker:compose", "git commit -m x": "git:commit", "./gradlew test": "gradle:test",
    "FOO=1 npm run build": "npm:build", "go test ./...": "go:test", "./scripts/run.sh": "script:run.sh",
    "npx prisma migrate dev": "npx:prisma", "this is prose": None, "the build": None,
}


@pytest.mark.parametrize("cmd,expected", CASES.items())
def test_normalize(cmd, expected):
    assert census.normalize(cmd) == expected


@pytest.mark.parametrize("cmd", ["bash tests/run_mongo_tests.sh", "sh ./scripts/start.sh -x", "python scripts/run_validation.py --verbose",
                                 "uv run scripts/run_validation.py", "node scripts/run_validation.js"])
def test_interpreter_script_one_form(cmd):
    got = census.normalize(cmd)
    assert got and got.startswith("script:") and "/" not in got


def test_interpreter_script_values():
    assert census.normalize("bash tests/run_mongo_tests.sh") == "script:run_mongo_tests.sh"
    assert census.normalize("bash") is None
    assert census.normalize("uv run pytest") == "uv:pytest"
    assert census.normalize("python -m pytest") == "pytest"


def test_extract_all_skips_negations_comments_and_prose():
    text = "Run `npm run test` then\n```bash\n$ pnpm build && make lint\n# comment\n```\nNever run `npm run clean`.\nWe just the thing.\n"
    assert census.extract_all(text) == ["npm:test", "pnpm:build", "make:lint"]


def test_toolchain_priority():
    assert census.toolchain({"pnpm-lock.yaml", "package.json"}) == "pnpm"
    assert census.toolchain({"pnpm-lock.yaml", "yarn.lock"}) == "mixed-js"
    assert census.toolchain({"uv.lock", "pyproject.toml"}) == "uv"
    assert census.toolchain({"README.md"}) == "none"


def test_skills_helpers():
    fm = census.frontmatter("---\nname: x\ndescription: d\nallowed-tools: Bash(git:*), Read, Grep\n---\nbody\n")
    assert census.allowed_tools(fm) == ["Bash", "Grep", "Read"]
    assert census.skill_script_refs("Run `scripts/a.py` and ./b.sh, not plain.py") == {"scripts/a.py", "b.sh"}
    assert census.resolve_link(".claude/skills/a", "../../.agents/skills/a") == ".agents/skills/a"
