"""Render docs/post1-draft.md ({{key}} placeholders) against reports/numbers.json -> docs/post1-draft.rendered.md.
Placeholder: {{key}} or {{key|fmt}}; fmt = pct (default for shares), pct1, int, ci (value with 95% CI), n, lo, hi, raw.
Unresolved keys are left as {{key}} and listed on stderr / in docs/review_notes.md checks. Third-party text is never touched."""
from __future__ import annotations

import json
import re
import sys

from . import config

ROOT = config.ROOT
PAT = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*(?:\|\s*([a-z0-9]+))?\s*\}\}")


def fmt(e: dict, how: str | None) -> str:
    v = e.get("value")
    if v is None:
        return "n/a"
    share = e.get("kind") == "share" or (e.get("kind") == "ratio" and 0 <= v <= 1)
    if e.get("kind") == "text":
        return str(v)
    how = how or ("pct" if share else "int")
    if how == "n":
        n = e.get("n")
        return f"{n:,}" if isinstance(n, int) else str(n)
    if how in ("lo", "hi"):
        x = e.get("ci_lo" if how == "lo" else "ci_hi")
        return f"{x:.0%}" if share else f"{x:,.0f}"
    if how == "ci":
        lo, hi = e.get("ci_lo"), e.get("ci_hi")
        return (f"{v:.0%} (95% CI {lo:.0%}–{hi:.0%})" if share else f"{v:,.0f} (95% CI {lo:,.0f}–{hi:,.0f})")
    if how == "pct":
        return f"{v:.0%}"
    if how == "pct1":
        return f"{v:.1%}"
    if e.get("kind") == "dec":
        return f"{v:.2f}"
    if how == "dec1":
        return f"{v:.1f}"
    if how == "int":
        return f"{v:,.0f}"
    return str(v)


def render(src=None, dst=None) -> list[str]:
    src = src or ROOT / "docs/post1-draft.md"
    dst = dst or ROOT / "docs/post1-draft.rendered.md"
    nums = json.loads((ROOT / "reports/numbers.json").read_text())
    flat = dict(nums["numbers"])
    meta = nums.get("meta", {})
    for k, n in meta.get("n_by_cell", {}).items():
        flat[f"n.{k}"] = {"value": n, "n": n, "kind": "count"}
    if "n.files.total" not in flat and "n.claude.overall" in flat and "n.agents.overall" in flat:
        _s = flat["n.claude.overall"]["value"] + flat["n.agents.overall"]["value"]
        flat["n.files.total"] = {"value": _s, "n": _s, "kind": "count"}
    st = nums.get("strata", {})
    lt = st.get("long_tail", {})
    for k in ("N_h", "n_sampled"):
        if lt.get(k) is not None:
            flat[f"strata.long_tail.{k}"] = {"value": lt[k], "n": lt[k], "kind": "count"}
    if lt.get("sample_prob") is not None:
        flat["strata.long_tail.sample_prob"] = {"value": lt["sample_prob"], "n": lt.get("n_sampled"), "kind": "share"}
    try:
        from . import config as _cfg
        _t = _cfg.load()["sample"]["longtail_target"]
    except Exception:
        _t = None
    if _t:
        flat["strata.long_tail.n_initial"] = {"value": _t, "n": _t, "kind": "count"}
    _c = nums["numbers"].get("repo.overall.pop.small_personal_share") or {}
    if _c.get("sensitivity"):
        flat["sens.small_share.trimmed"] = {"value": _c["sensitivity"]["trimmed_value"], "n": _c.get("n"), "kind": "share"}
    ab = ROOT / "reports/age_baseline.json"
    if ab.exists():
        _b = json.loads(ab.read_text())["counts"]
        for g in ("small", "popular", "all"):
            flat[f"age.base.{g}.share_lt6m"] = {"value": _b[g]["share_lt6m"], "n": _b[g]["total"], "kind": "share"}
    est = st.get("established", {})
    for k in ("N_nondev", "n_sampled"):
        if est.get(k) is not None:
            flat[f"strata.established.{k}"] = {"value": est[k], "n": est[k], "kind": "count"}
    flat["meta.bootstrap"] = {"value": nums.get("bootstrap"), "n": nums.get("bootstrap"), "kind": "count"}
    for k in ("sensitive_cells", "sensitivity_cells", "low_n_cells", "cells"):
        flat[f"meta.{k}"] = {"value": meta.get(k), "n": meta.get(k), "kind": "count"}
    mp = ROOT / "reports/manual_checks.json"
    if mp.exists():
        for k, v in json.loads(mp.read_text()).items():
            if not k.startswith("_"):
                flat[f"check.{k}"] = {"value": v, "n": v, "kind": "count" if isinstance(v, int) else "dec"}
    tp = ROOT / "reports/top_commands.json"
    if tp.exists():
        slug = {"npm / pnpm / yarn": "npm", "Python (uv, poetry, pip)": "python", "Rust (cargo)": "cargo", "Go": "go", "make / just only": "make"}
        for lab, d in json.loads(tp.read_text()).items():
            sg = slug.get(lab)
            flat[f"top.{sg}.n_repos"] = {"value": d["n_repos"], "n": d["n_repos"], "kind": "count"}
            for i, r in enumerate(d["top"][:10], 1):
                flat[f"top.{sg}.{i}.cmd"] = {"value": r["cmd"], "n": r["n_repos"], "kind": "text"}
                flat[f"top.{sg}.{i}.share"] = {"value": r["share_weighted"], "n": d["n_repos"], "kind": "share"}
    text = src.read_text()
    missing: list[str] = []

    def sub(m):
        e = flat.get(m.group(1))
        if e is None:
            missing.append(m.group(1))
            return m.group(0)
        return fmt(e, m.group(2))

    out = PAT.sub(sub, text)
    dst.write_text(out)
    used = len(PAT.findall(text))
    return [f"rendered {used} placeholders -> {dst.relative_to(ROOT)}; unresolved: {sorted(set(missing)) or 'none'}"]


if __name__ == "__main__":
    print("\n".join(render()))
    sys.exit(0)
