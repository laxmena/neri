"""Regression cases from M5 round 1 false positives (handwritten, not third-party text)."""
from neri import core


def paths(text, tree, gi=None):
    return {r["ref"]: r["dead"] for r in core.dead_paths(text, tree, gi)}


def test_drive_letters_templates_and_creation_are_excluded():
    txt = ("Installed at `C:/Users/x/app/run.py`.\nUse `docs/XX-name.md` as the template.\n"
           "Copy `lib/config-sample.php` to `lib/config.php`.\nWrite findings to `docs/out.md`.\n"
           "Named `PascalCase.tsx` or `filename.en.md`.\nBuild goes to `bin/app` -> `dist/app`.\n")
    assert paths(txt, ["README.md"]) == {}


def test_tree_diagram_and_comments_excluded():
    txt = "```\n├── module.go   # registration\n└── style.css\nrun app.py   # see config.json here\n```\n"
    assert paths(txt, ["README.md"]) == {}


def test_css_import_is_not_an_agent_import():
    assert paths('@import "@scope/ui/theme/x.css";\n', ["a.md"]) == {}


def test_make_pattern_rule_and_just_defaults_and_modules():
    mk = {"Makefile": "%:\n\t@echo $@\nbuild:\n\techo\n"}
    assert [c["dead"] for c in core.dead_commands("`make anything`", ["Makefile"], mk)] == [False]
    jf = {"justfile": 'db-up tenant="local" *args:\n  echo\nfix:\n  echo\n'}
    r = core.dead_commands("`just db-up` and `just fix` and `just nope`", ["justfile"], jf)
    assert {c["target"]: c["dead"] for c in r} == {"db-up": False, "fix": False, "nope": True}
    assert core.dead_commands("`just x`", ["justfile"], {"justfile": "mod git\n"}) == []


def test_prose_just_and_monorepo_python_not_commands():
    assert core.extract_commands("We just the thing. `use just the tool`\n") == []
    assert core.dead_commands("`uv run mycli`", ["pyproject.toml", "pkg/pyproject.toml"], {"pyproject.toml": "[project]\nname='a'\n"}) == []


from neri import classify  # noqa: E402


def cat(text, tree, gi=None):
    return {(r["ref"]): r["category"] for r in classify.classify_paths(text, tree, gi)}


def test_symlink_or_submodule_ancestor_is_unknown_exact_entry_is_alive():
    tree = [".claude/skills/a", "packages/sub", "README.md"]  # a symlink blob and a submodule entry
    c = cat("See `.claude/skills/a/SKILL.md` and `packages/sub/src/x.ts` and `packages/sub/`.", tree)
    assert c[".claude/skills/a/SKILL.md"] == "UNKNOWN" and c["packages/sub/src/x.ts"] == "UNKNOWN" and c["packages/sub"] == "ALIVE"


def cmd(text, tree, man, sub=None):
    return {(c["kind"], c["target"], c["cwd"]): (c["category"], c["reason"]) for c in classify.classify_commands(text, tree, man, sub)}


def test_cd_is_honoured_and_resolved_against_the_manifests_of_that_dir():
    # human spot check r3-45: `make install-postgres` ran after `cd tests/postgres`
    txt = "```bash\ncd tests/postgres\nmake install-postgres\nmake nope\n```\nThen `make build`.\n"
    tree = ["Makefile", "tests/postgres/Makefile"]
    root = {"Makefile": "build:\n\techo\n"}
    sub = {"tests/postgres": {"Makefile": "install-postgres:\n\techo\n"}}
    r = cmd(txt, tree, root, sub)
    assert r[("make", "install-postgres", "tests/postgres")][0] == "ALIVE"
    assert r[("make", "nope", "tests/postgres")][0] == "STALE"
    assert r[("make", "build", "")][0] == "ALIVE"
    assert cmd(txt, tree, root, None)[("make", "install-postgres", "tests/postgres")] == ("UNKNOWN", "cd_dir_not_indexed")


def test_cd_same_line_resets_per_block_and_applies_to_paths():
    c = cmd("```\ncd sub\nmake a\n```\n```\nmake b\n```\n`cd e2e && npm run lint`", ["Makefile"], {"Makefile": "b:\n"}, {"sub": {}, "e2e": {}})
    assert c[("make", "b", "")][0] == "ALIVE" and c[("make", "a", "sub")][0] == "UNKNOWN" and c[("pkg", "lint", "e2e")][0] == "UNKNOWN"
    # a path relative to the cd dir is resolved there
    r = cat("```\ncd e2e\nnpx playwright test tests/a/b.spec.ts\n```\n", ["e2e/tests/a/b.spec.ts"])
    assert r["tests/a/b.spec.ts"] == "ALIVE"


def test_wildcards_patterns_and_placeholders_are_not_references():
    c = cmd("`just test-*` and `just build` and `npm run <script>`", ["justfile"], {"justfile": "build:\n"})
    assert c[("just", "test-", "")] == ("NOT_A_REFERENCE", "pattern") and c[("just", "build", "")][0] == "ALIVE"
    assert not [k for k in c if k[1] == "<script>"]
    p = cat("`tests/*.py`, `src/{x}/a.ts`, `tests/module/specific.spec.ts`, `docs/releases/vX.Y.Z.md`, `my-app/src/a.py`", ["README.md"])
    assert set(p.values()) == {"NOT_A_REFERENCE"} and len(p) == 5


def test_conditional_created_and_other_project_contexts():
    p = cat("When present, `blueprint/config.json` is read.\nCopy `lib/a-sample.php` to `lib/a.php`.\n"
            "The user's project keeps `wiki/getting-started.md`.\nSee `src/real.py`.\n", ["src/real.py"])
    assert p["blueprint/config.json"] == "NOT_A_REFERENCE" and p["lib/a.php"] == "EXPECTED_CREATED"
    assert p["wiki/getting-started.md"] == "NOT_A_REFERENCE" and p["src/real.py"] == "ALIVE"


def test_moved_v3_matches_stem_ignoring_extension_and_reports_target():
    tree = ["docs/reference/configuration.mdx", "docs/index.md"]
    r = classify.classify_paths("See `docs/configuration.md`.", tree)[0]
    assert r["category"] == "STALE" and r["moved"] and r["moved_to"] == "docs/reference/configuration.mdx"
    r2 = classify.classify_paths("See `docs/missing/thing.py`.", tree)[0]
    assert r2["category"] == "STALE" and not r2["moved"]


def test_line_numbers_are_reported():
    r = classify.classify_paths("intro\n\nSee `docs/gone.md`.\n", ["README.md"])[0]
    assert r["line"] == 3
