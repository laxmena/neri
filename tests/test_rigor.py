from neri import core, validate, view


def test_cohen_kappa_known_value():
    a = {1: "TP", 2: "TP", 3: "FP", 4: "FP"}
    b = {1: "TP", 2: "FP", 3: "FP", 4: "FP"}
    k, agree = validate.cohen_kappa(a, b, [1, 2, 3, 4])
    assert agree == 0.75 and abs(k - 0.5) < 1e-9


def test_view_rejects_unsafe_paths():
    assert view.safe_parts("a/b/c.md") == ["a", "b", "c.md"]
    for bad in ("../x", "/abs", "a//b", "a/./b", ""):
        assert view.safe_parts(bad) is None


def test_moved_v3_tier():
    tree = ["src/core/llm.py", "scripts/run.cjs", "Docs/SECURITY.md", "a/README.md", "b/README.md", "x/AGENTS.md"]
    d = {r["ref"]: r for r in core.dead_paths(
        "`src/llm.py` `scripts/run.js` `docs/SECURITY.md` `lib/README.md` `sub/AGENTS.md` `app/other.py`", tree)}
    assert d["src/llm.py"]["moved"]            # unique non-generic basename elsewhere
    assert d["scripts/run.js"]["moved"]        # same dir, other extension
    assert d["docs/SECURITY.md"]["moved"]      # letter case
    assert not d["lib/README.md"]["moved"]     # generic and ambiguous basename
    assert not d["sub/AGENTS.md"]["moved"]     # generic basename: not evidence of a move
    assert not d["app/other.py"]["moved"]      # nothing similar exists


def test_multiclass_kappa_and_label_back_compat():
    a = {1: "STALE", 2: "STALE", 3: "NOT_A_REFERENCE", 4: "EXPECTED_CREATED"}
    b = {1: "STALE", 2: "UNKNOWN", 3: "NOT_A_REFERENCE", 4: "EXPECTED_CREATED"}
    k, agree = validate.cohen_kappa(a, b, [1, 2, 3, 4])
    assert agree == 0.75 and 0 < k < 1
    assert validate._stale("TP") and validate._stale("STALE") and not validate._stale("FP") and not validate._stale("UNKNOWN")
