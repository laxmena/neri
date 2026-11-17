"""Regression: dead-path flags must not be raised from a TRUNCATED git tree (M5 spot-check bug)."""
from neri import core, fetch

TEXT = "Source is `src/loader.ts`, data lives in `data/skills.json`, docs in `docs/guide.md`.\n"
PARTIAL = ["data/skills.json", "data/a/b.json"]  # the listing stopped inside data/: src/ and docs/ were never seen


def test_partial_listing_alone_would_wrongly_call_unseen_dirs_dead():
    dead = {r["ref"] for r in core.dead_paths(TEXT, PARTIAL) if r["dead"]}
    assert dead == {"src/loader.ts", "docs/guide.md"}  # the bug: these exist upstream but were never listed


def test_truncated_tree_never_yields_dead_only_alive_or_unknown():
    refs = core.dead_paths(TEXT, PARTIAL)
    out = {r["ref"]: r for r in core.apply_truncation(refs, {"src/loader.ts": True, "docs/guide.md": None})}
    assert not any(r["dead"] for r in out.values()) and not any(r["moved"] for r in out.values())
    assert out["src/loader.ts"]["unknown"] is False  # a directory walk found it: alive
    assert out["docs/guide.md"]["unknown"] is True   # the walk could not tell: unknown, excluded
    assert out["data/skills.json"]["unknown"] is False  # present in the partial listing: alive
    out2 = core.apply_truncation(refs, {"src/loader.ts": False})  # walk says absent: still unknown, never dead
    assert out2[0]["dead"] is False and out2[0]["unknown"] is True


def _fake(tree):
    return lambda sha: tree.get(sha)


def test_walk_path_per_directory():
    tree = {"root": [{"n": "src", "t": "tree", "m": "040000", "h": "S"}, {"n": "link", "t": "blob", "m": "120000", "h": "L"},
                     {"n": "sub", "t": "commit", "m": "160000", "h": "C"}],
            "S": [{"n": "x.ts", "t": "blob", "m": "100644", "h": "X"}]}
    g = _fake(tree)
    assert fetch.walk_path("src/x.ts", "root", g) is True
    assert fetch.walk_path("src/y.ts", "root", g) is False
    assert fetch.walk_path("nope/x.ts", "root", g) is False
    assert fetch.walk_path("link/deep/file.md", "root", g) is True    # symlinked dir resolves on disk
    assert fetch.walk_path("sub/a/b.go", "root", g) is True           # submodule
    assert fetch.walk_path("src/x.ts", "missing-root", g) is None     # API could not tell


def test_commands_in_truncated_repos_are_unknown():
    dc = core.dead_commands("`make deploy`", ["README.md"], {})
    assert dc and dc[0]["dead"]  # complete tree: no Makefile anywhere -> dead
    out = core.apply_truncation([{**x, "ref": x["target"]} for x in dc], {})
    assert out[0]["dead"] is False and out[0]["unknown"] is True
