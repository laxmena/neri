import json
from pathlib import Path

import pytest

from neri import core, fetch, features

FIX = sorted(Path(__file__).parent.joinpath("fixtures").glob("*.json"))


def load(kind):
    return [(p.stem, json.loads(p.read_text())) for p in FIX if json.loads(p.read_text())["kind"] == kind]


@pytest.mark.parametrize("name,fx", load("imports"))
def test_import_chain(name, fx):
    for path, expected in fx["expect_extract"].items():
        assert fetch._imports(fx["texts"][path]) == expected or [x for x in fetch._imports(fx["texts"][path]) if "/" in x and not x.startswith("types")] == expected
    reach = [i["to"] for i in features._reach(fx["edges"], "CLAUDE.md")]
    assert sorted(reach) == sorted(fx["expect_reach"])


@pytest.mark.parametrize("name,fx", load("paths"))
def test_paths(name, fx):
    res = core.dead_paths(fx["text"], fx["tree"], fx.get("gitignore"))
    if "expect_checked" in fx:
        assert len(res) == fx["expect_checked"], res
    dead = {r["ref"] for r in res if r["dead"]}
    alive = {r["ref"] for r in res if not r["dead"]}
    assert dead >= set(fx.get("expect_dead", []))
    assert alive >= set(fx.get("expect_alive", []))
    if "expect_dead" in fx:
        assert dead == set(fx["expect_dead"])


@pytest.mark.parametrize("name,fx", load("cmds"))
def test_cmds(name, fx):
    res = core.dead_commands(fx["text"], fx["tree"], fx["manifests"])
    dead = {r["target"] for r in res if r["dead"]}
    alive = {r["target"] for r in res if not r["dead"]}
    assert dead == set(fx["expect_dead"]), res
    assert alive >= set(fx["expect_alive"]), res


@pytest.mark.parametrize("name,fx", load("toolchain"))
def test_toolchain(name, fx):
    for c in fx["cases"]:
        r = core.toolchain_mismatch(c["text"], c["root"])
        assert (r["js_mismatch"], r["py_mismatch"]) == (c["js"], c["py"]), c


@pytest.mark.parametrize("name,fx", load("drift"))
def test_drift(name, fx):
    assert core.drift(fx["a"], fx["b"], symlink=fx["symlink"])["kind"] == fx["expect_kind"]


@pytest.mark.parametrize("name,fx", load("style"))
def test_style(name, fx):
    s = core.style(fx["text"], fx["tokens"])
    assert s["shout_words"] == fx["expect_shout_words"]
    assert s["caps_lines"] == fx["expect_caps_lines"]
    assert s["negation_share"] == fx["expect_negation_share"]
    assert s["imperative_share"] == fx["expect_imperative_share"]


def test_ten_fixtures_exist():
    assert len(FIX) == 10


def test_drift_kinds():
    assert core.drift("a b", "a   b")["kind"] == "identical"
    assert core.drift("# T\nrun `npm test`\n", "# T\nrun `pytest`\n")["test_contradiction"] is True
    assert core.drift("x", "y", a_imports_b=True)["kind"] == "imports"
