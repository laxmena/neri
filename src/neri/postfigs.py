"""Post figures (figures/post/*.svg + *.png + *.csv) built from reports/numbers.json and the weighted data, following the dataviz
skill: validated palette (blue = established, orange = long tail, both validated light + dark; the structure categories use other slots),
thin marks, a 2px surface gap between touching marks, a 2px surface ring on dots, a legend for >= 2 series, selective direct labels,
text in ink tokens (never the series colour), recessive hairline grid, and a CSV table view for every figure."""
from __future__ import annotations

import csv
import json

import matplotlib
import matplotlib.ticker
import numpy as np
import polars as pl

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from . import config, report  # noqa: E402

ROOT = config.ROOT
OUT = ROOT / "figures/post"
POST_OLD = ["fig1_size_ecdf", "fig2_structure", "fig3_ecosystem", "fig4_stack_size", "fig5_what_fills_the_file"]
INK, INK2, GRID, SURF = "#1A1A2E", "#6B6B76", "#EEEEF2", "#FFFFFF"  # docs/FIGURE-STYLE.md: light, one purple accent, grays
ACCENT, DEEP, G1, G2 = "#8B5CF6", "#5B3DB8", "#9A9AA6", "#C9C9D1"
EST, TAIL = G1, ACCENT  # popular = gray, small & personal = the accent (the bulk of the population)
STRUCT = [("single", ACCENT), ("nested", G1), ("nested-only", G2), ("imports", DEEP)]
LW = 1.6  # ~2px at 100 dpi


def _style():
    plt.rcParams.update({"svg.fonttype": "none", "font.family": "sans-serif", "font.sans-serif": ["Inter", "Helvetica Neue", "Helvetica", "Arial"], "font.monospace": ["JetBrains Mono", "Menlo", "DejaVu Sans Mono"], "font.size": 11, "text.color": INK, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "axes.edgecolor": GRID, "figure.facecolor": SURF, "axes.facecolor": SURF,
                         "savefig.facecolor": SURF})


def _axes(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="x", color=GRID, lw=.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def _save(fig, name: str, rows: list[dict]) -> list[str]:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("svg", "png"):
        p = OUT / f"{name}.{ext}"
        fig.savefig(p, dpi=200 if ext == "png" else None, bbox_inches="tight")
        paths.append(str(p.relative_to(ROOT)))
    plt.close(fig)
    if rows:
        with open(OUT / f"{name}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)), restval="")
            w.writeheader(); w.writerows(rows)
        paths.append(f"figures/post/{name}.csv")
    return paths


def _num(nums, key):
    return nums.get(key)


POP, SMALL = "Popular repos (10+ stars)", "Small & personal repos (0–9 stars)"
TOKNOTE = "Tokens counted with the o200k_base tokenizer, a proxy for Claude's own."


def _date() -> str:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(config.load()["snapshot"]["date"])[:10]).strftime("%b %Y")
    except Exception:
        return str(config.load()["snapshot"]["date"])


def _title(fig, title, sub, foot=None, n_text="", foot_y=-0.03):
    import textwrap
    h = fig.get_figheight()
    sub_l = textwrap.wrap(sub, 130)
    t_l = textwrap.wrap(title, 64)
    fig.text(0.0, 1.0 + 0.02, "\n".join(sub_l), fontsize=10, ha="left", va="bottom", color=INK2, linespacing=1.3)
    fig.text(0.0, 1.0 + (len(sub_l) * 0.21 + 0.16) / h, "\n".join(t_l), fontsize=16, fontweight="bold", ha="left", va="bottom", color=INK, linespacing=1.25)
    src = f"neri census of public GitHub · {_date()} · {n_text} · weighted; 95% intervals"
    fig.text(0.0, foot_y, ((foot + "\n") if foot else "") + src, fontsize=8.5, ha="left", va="top", color=INK2, linespacing=1.4)


def _v(nums, key, field="value", default=None):
    e = nums.get(key)
    return e[field] if e and e.get(field) is not None else default


def _legend(ax, handles, loc="lower right"):
    return None  # FIGURE-STYLE: label directly; callers add text labels



def _dot_panel(ax, items, nums, rows, panel, xmax=None):
    _axes(ax); ax.grid(axis="x", color=GRID, lw=.8)
    for i, (lab, ft, key) in enumerate(items):
        for s, col, off in (("established", EST, -.15), ("long_tail", TAIL, .15)):
            e = _num(nums, f"{ft}.{s}.{key}")
            if not e or e["value"] is None:
                continue
            y = i + off
            ax.plot([e["ci_lo"], e["ci_hi"]], [y, y], color=col, lw=LW, solid_capstyle="round", zorder=3)
            ax.plot([e["value"]], [y], "o", color=col, ms=8, mec=SURF, mew=2, zorder=4)
            if i == 0:
                ax.text(e["ci_hi"] + .012, y, "Popular" if s == "established" else "Small & personal", fontsize=8.5, color=(ACCENT if s == "long_tail" else INK2), va="center", fontweight="bold")
            rows.append({"panel": panel, "metric": lab, "group": POP if s == "established" else SMALL, "value": e["value"], "ci_lo": e["ci_lo"], "ci_hi": e["ci_hi"], "n": e["n"], "low_n": e.get("low_n")})
    ax.set_yticks(range(len(items))); ax.set_yticklabels([x[0] for x in items]); ax.set_ylim(len(items) - .5, -.5)
    ax.set_xlim(0, xmax); ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(.05 if (xmax or 1) < .5 else .1))


def _dot_legend(fig, ax):
    return None


def fig_pop(tag, nums) -> list[str]:
    """Fig0: population. Files found per name, and the share of repos that are small & personal."""
    fig, axs = plt.subplots(1, 2, figsize=(10, 2.9), gridspec_kw={"width_ratios": [1.1, 1]})
    ax = axs[0]; _axes(ax); ax.grid(axis="x", color=GRID, lw=.8)
    fa, fc = _v(nums, "enum.files.agents", default=0), _v(nums, "enum.files.claude", default=0)
    rows = []
    for i, (lab, val) in enumerate((("AGENTS.md", fa), ("CLAUDE.md", fc))):
        ax.barh(i, val / 1e6, height=.4, color=G1, edgecolor=SURF, linewidth=1.5)
        ax.text(val / 1e6 + .02, i, f"{val / 1e6:.2f} M files", va="center", fontsize=9, color=INK)
        rows.append({"panel": "files found", "name": lab, "files": val})
    ax.set_yticks([0, 1]); ax.set_yticklabels(["AGENTS.md", "CLAUDE.md"]); ax.set_ylim(1.6, -.6); ax.set_xlim(0, max(fa, fc) / 1e6 * 1.3)
    ax.set_xlabel("files found by GitHub code search (millions)"); ax.set_title("Files found per name", fontsize=10, loc="left", color=INK)
    ax = axs[1]; _axes(ax); ax.grid(False)
    sm_, lo, hi = _v(nums, "repo.overall.pop.small_personal_share", default=0), _v(nums, "repo.overall.pop.small_personal_share", "ci_lo", 0), _v(nums, "repo.overall.pop.small_personal_share", "ci_hi", 0)
    ax.barh(0, 1 - sm_, height=.35, color=EST, edgecolor=SURF, linewidth=1.5)
    ax.barh(0, sm_, left=1 - sm_, height=.35, color=TAIL, edgecolor=SURF, linewidth=1.5)
    ax.text((1 - sm_) / 2, 0, f"{1 - sm_:.0%}", ha="center", va="center", fontsize=10, color=INK)
    ax.text(1 - sm_ / 2, 0, f"{sm_:.0%}", ha="center", va="center", fontsize=12, color="white", fontweight="bold")
    ax.set_xlim(0, 1); ax.set_ylim(.8, -.8); ax.set_yticks([]); ax.set_xticks([0, .25, .5, .75, 1]); ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_title("Share of repos with an agent file (weighted)", fontsize=10, loc="left", color=INK)
    ax.text((1 - sm_) / 2, -.38, "Popular (10+ stars)", ha="center", fontsize=9, color=INK2); ax.text(1 - sm_ / 2, -.38, "Small & personal (0–9)", ha="center", fontsize=9, color=ACCENT, fontweight="bold")
    rows.append({"panel": "share of repos", "name": SMALL, "share": sm_, "ci_lo": lo, "ci_hi": hi})
    n = _v(nums, "repo.overall.pop.small_personal_share", "n", 0)
    _title(fig, f"About {sm_:.0%} of repos with an agent file have fewer than 10 stars; they are the bulk of the population",
           "Left: raw search counts before any filter. Right: weighted share of repos (95% CI "
           f"{lo:.1%}–{hi:.1%}), forks and archived repos excluded", "Code search only sees default branches and excludes forks and archived repos.", f"n = {n:,} sampled repos", foot_y=-0.16)
    return _save(fig, "fig0_population", rows)


def fig_size(tag, nums) -> list[str]:
    """Where files fall: weighted ECDF of always-loaded tokens, by group, for each file type."""
    t = report._tables(tag)
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.9), sharey=True)
    rows, ns = [], []
    for ax, (ft, name) in zip(axs, report.FILES.items()):
        df = report.load_file(tag, name, t).filter(pl.col("structure") != "nested-only")
        ns.append(f"{df.height:,} {name}")
        _axes(ax); ax.grid(axis="y", color=GRID, lw=.8)
        for s, col, lab in (("established", EST, POP), ("long_tail", TAIL, SMALL)):
            sub = df.filter(pl.col("stratum") == s)
            if sub.is_empty():
                continue
            x, w = sub["always_loaded_tokens"].to_numpy().astype(float), sub["w"].to_numpy()
            o = np.argsort(x)
            xs, cw = np.maximum(x[o], 1), np.cumsum(w[o]) / w.sum()
            ax.step(xs, cw, where="post", color=col, lw=LW, label=lab)
            ax.text(2200, .27 if s == "long_tail" else .17, "Small & personal" if s == "long_tail" else "Popular", fontsize=10, color=(ACCENT if s == "long_tail" else INK2), fontweight="bold", va="center")
            ax.plot([1500, 2000], [.27 if s == "long_tail" else .17] * 2, color=(ACCENT if s == "long_tail" else G1), lw=2.4)
            med = _num(nums, f"{ft}.{s}.a1.always_loaded_tokens.median")
            if med and med["value"]:
                ax.plot([med["value"]], [0.5], "o", color=col, ms=8, mec=SURF, mew=2, zorder=5)
                ax.annotate(f"median {med['value']:,.0f}", (med["value"], 0.5), xytext=(-8, 12 if s == "established" else -16), textcoords="offset points", fontsize=9.5, color=(ACCENT if s == "long_tail" else INK2), fontweight="bold", ha="right", arrowprops=dict(arrowstyle="-", color=G2, lw=.7))
            rows += [{"file": name, "group": lab, "tokens": float(a), "cumulative_weighted_share": float(b)} for a, b in zip(xs[::max(1, len(xs) // 200)], cw[::max(1, len(xs) // 200)])]
        ax.set_xscale("log"); ax.set_xlim(30, 60000); ax.set_ylim(0, 1)
        ax.set_xticks([100, 1000, 10000]); ax.set_xticklabels(["100", "1,000", "10,000"])
        ax.set_yticks([0, .25, .5, .75, 1]); ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.set_title(name, fontsize=10, color=INK, loc="left")
        ax.set_xlabel("tokens loaded every session (log scale)")
    axs[0].set_ylabel("share of repos with a file this size or smaller")
    _legend(axs[1], [Line2D([0], [0], color=EST, lw=LW, label=POP), Line2D([0], [0], color=TAIL, lw=LW, label=SMALL)])
    ce, cl = _v(nums, "claude.established.a1.always_loaded_tokens.median", default=0), _v(nums, "claude.long_tail.a1.always_loaded_tokens.median", default=0)
    ae, al = _v(nums, "agents.established.a1.always_loaded_tokens.median", default=0), _v(nums, "agents.long_tail.a1.always_loaded_tokens.median", default=0)
    r = lambda v: f"{round(v, -2):,.0f}"
    if ce and cl and abs(ce - cl) / max(ce, cl) <= .2:
        t1 = f"Popular or not, the typical CLAUDE.md costs ~{r((ce + cl) / 2)} tokens every session"
    else:
        t1 = f"The typical CLAUDE.md costs ~{r(ce)} tokens every session in popular repos and ~{r(cl)} in small ones"
    t2 = "; small & personal repos' AGENTS.md files are smaller" if al < ae * .9 else ("; small & personal repos' AGENTS.md files are larger" if al > ae * 1.1 else "; AGENTS.md files are about the same size")
    _title(fig, t1 + t2, "Share of repos by always-loaded size (root file plus @imports); repos whose instructions live only in subdirectories are excluded here",
           TOKNOTE, "n = " + " and ".join(ns) + " files")
    return _save(fig, "fig1_size_ecdf", rows)


def fig_structure(tag, nums) -> list[str]:
    fig, ax = plt.subplots(figsize=(9, 2.9))
    _axes(ax); ax.grid(False)
    scopes = [("established", POP), ("long_tail", SMALL), ("overall", "All repos (weighted)")]
    names = {"single": "single file", "nested": "nested", "nested-only": "nested-only", "imports": "imports"}
    rows = []
    for i, (s, lab) in enumerate(scopes):
        v = {}
        for k in ("single", "nested", "nested_only", "imports", "both"):
            v[k] = _v(nums, f"claude.{s}.a1.structure.{k}", default=0) or 0
        parts = [("single", v["single"]), ("nested", v["nested"]), ("nested-only", v["nested_only"]), ("imports", v["imports"] + v["both"])]
        left = 0
        for (name, val), (_, col) in zip(parts, STRUCT):
            ax.barh(i, val, left=left, height=.52 if i == 0 else .3, color=col, edgecolor=SURF, linewidth=1.5)
            if val >= .05:
                lab_ = ({"single": "single file", "nested": "nested", "nested-only": "nested-only", "imports": "imports"}[name] + "\n" if i == 0 else "") + f"{val:.0%}"
                ax.text(left + val / 2, i, lab_, ha="center", va="center", fontsize=9 if i else 7.5, color=("white" if name in ("single", "imports") else INK), linespacing=1.15)
            elif val > 0:
                ax.annotate((("<1% " if val < .01 else f"{val:.0%} ") + "imports") if i == 0 else ("<1%" if val < .01 else f"{val:.0%}"), (left + val / 2, i - .15), xytext=(0, 9), textcoords="offset points", ha="center", va="bottom", fontsize=8.5, color=INK2,
                            arrowprops=dict(arrowstyle="-", color=INK2, lw=.6))
            rows.append({"group": lab, "structure": name, "share": val})
            left += val
    fig.subplots_adjust(left=.30)
    ax.set_yticks(range(3)); ax.set_yticklabels([l for _, l in scopes]); ax.set_ylim(2.6, -.8)
    ax.set_xlim(0, 1); ax.set_xticks([0, .25, .5, .75, 1]); ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])

    sg = _v(nums, "claude.overall.a1.structure.single", default=0)
    no = _v(nums, "claude.overall.a1.structure.nested_only", default=0)
    _title(fig, f"{sg:.0%} of CLAUDE.md setups are one root file; {no:.0%} keep instructions only in subdirectories",
           "Share of repos with a CLAUDE.md, by how the instructions are organized (weighted)",
           "single = one root file; nested = root plus subdirectory files; nested-only = no root file; imports = root file pulls in other files with @path.",
           f"n = {_v(nums, 'claude.overall.a1.structure.single', 'n', 0):,} repos", foot_y=-0.2)
    return _save(fig, "fig2_structure", rows)


def fig_ecosystem(tag, nums):
    claude_items = [("skills (.claude/skills)", "repo", "a7_with_claude.skills"), ("settings / hooks (.claude/settings.json)", "repo", "a7_with_claude.settings_json"),
                    ("subagents (.claude/agents)", "repo", "a7_with_claude.agents"), ("slash commands (.claude/commands)", "repo", "a7_with_claude.commands")]
    other = [("MCP (.mcp.json)", "repo", "a7.mcp_json"), ("Copilot instructions", "repo", "a7.copilot_instructions"), ("Cursor rules", "repo", "a7.cursor_rules")]
    fig, axs = plt.subplots(2, 1, figsize=(8.6, 5.2), gridspec_kw={"height_ratios": [len(claude_items), len(other)]}, sharex=True)
    rows = []
    fig.subplots_adjust(left=.36)
    _dot_panel(axs[0], claude_items, nums, rows, "claude")
    axs[0].set_title("Among repos that have a CLAUDE.md", fontsize=10, loc="left", color=INK)
    _dot_panel(axs[1], other, nums, rows, "cross")
    axs[1].set_title("Among all repos with an agent file (cross-tool files)", fontsize=10, loc="left", color=INK)
    axs[1].set_xlabel("share of repos (95% CI)")
    _dot_legend(fig, axs[1])
    sk = _v(nums, "repo.overall.a7_with_claude.skills", default=0)
    anyf = _v(nums, "repo.overall.a7_with_claude.any_of_four", default=0)
    _title(fig, f"{1 - anyf:.0%} of repos with a CLAUDE.md use none of skills, hooks, subagents or slash commands" if anyf else "Ecosystem files",
           f"Share of repos with each file in the git tree, weighted; skills alone: {sk:.0%} of repos with a CLAUDE.md. Denominators in the panel titles.",
           None, f"n = {_v(nums, 'repo.overall.a7_with_claude.skills', 'n', 0):,} repos with a CLAUDE.md; {_v(nums, 'repo.overall.a7.mcp_json', 'n', 0):,} repos in all")
    return _save(fig, "fig3_ecosystem", rows)


def fig_fill(tag, nums):
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.4), sharey=True)
    fig.subplots_adjust(left=.22, wspace=.08)
    rows = []
    items = [("CLAUDE.md: directory tree", "claude", "a8.tree_dump"), ("CLAUDE.md: overview section", "claude", "a8.overview"),
             ("AGENTS.md: directory tree", "agents", "a8.tree_dump"), ("AGENTS.md: overview section", "agents", "a8.overview")]
    for ax, (suffix, title, panel) in zip(axs, (("share_files", "Share of files that have one", "files"), ("token_share", "Share of all tokens they use", "tokens"))):
        _dot_panel(ax, [(l, ft, f"{k}.{suffix}") for l, ft, k in items], nums, rows, panel)
        ax.set_title(title, fontsize=10, loc="left", color=INK)
        ax.set_xlabel("share (95% CI)")
    ct, co = _v(nums, "claude.overall.a8.tree_dump.share_files", default=0), _v(nums, "claude.overall.a8.overview.share_files", default=0)
    _title(fig, f"{ct:.0%} of CLAUDE.md files paste a directory tree and {co:.0%} restate an overview", "Directory-tree dumps and overview-type sections, weighted, by file type and repo group",
           TOKNOTE + " Overview = a heading like overview, about, architecture or project structure.",
           f"n = {_v(nums, 'claude.overall.a8.tree_dump.share_files', 'n', 0):,} CLAUDE.md + {_v(nums, 'agents.overall.a8.tree_dump.share_files', 'n', 0):,} AGENTS.md files", foot_y=-0.12)
    return _save(fig, "fig5_what_fills_the_file", rows)


def fig_stacks(tag, nums) -> list[str]:
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"width_ratios": [1.5, 1], "wspace": .4})
    ax = axs[0]; _axes(ax); ax.grid(axis="y", color=GRID, lw=.8); ax.grid(axis="x", visible=False)
    cats = [("1 file", "stack.size_1"), ("2 files", "stack.size_2"), ("3 files", "stack.size_3"), ("4+ files", "stack.size_4_plus")]
    rows = []
    for s, col, off, lab in (("established", EST, -.19, POP), ("long_tail", TAIL, .19, SMALL)):
        for i, (cl, key) in enumerate(cats):
            e = _num(nums, f"repo.{s}.{key}")
            if not e or e["value"] is None:
                continue
            ax.bar(i + off, e["value"], width=.34, color=col, edgecolor=SURF, linewidth=1.5)
            ax.text(i + off, e["value"] + .012, f"{e['value']:.0%}", ha="center", va="bottom", fontsize=9, color=INK2)
            if i == 0:
                ax.text(i + off + (-.02 if s == "established" else .06), e["value"] + .10, "Popular" if s == "established" else "Small & personal", ha=("center" if s == "established" else "left"), va="bottom", fontsize=8.5, color=(ACCENT if s == "long_tail" else INK2), fontweight="bold")
            rows.append({"panel": "stack size", "group": lab, "stack_size": cl, "share": e["value"], "ci_lo": e["ci_lo"], "ci_hi": e["ci_hi"], "n": e["n"]})
    ax.set_xticks(range(len(cats))); ax.set_xticklabels([c[0] for c in cats])
    ax.set_ylim(0, 1); ax.set_yticks([0, .25, .5, .75, 1]); ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    _legend(ax, [Patch(facecolor=EST, label=POP), Patch(facecolor=TAIL, label=SMALL)], "upper right")
    ax.set_title("Instruction files per repo", fontsize=10, loc="left", color=INK)
    ax = axs[1]; _axes(ax); ax.grid(False)
    segs = [("linked: symlink, import or pointer", ["symlink", "import_bridge", "prose_pointer"], ACCENT), ("near-copy", ["near_copy"], G1), ("independent", ["independent"], G2)]
    for i, (s, lab) in enumerate((("established", POP), ("long_tail", SMALL), ("overall", "All repos"))):
        vals = [[(_num(nums, f"repo.{s}.link.class.{c}") or {}).get("value") for c in cs] for _, cs, _ in segs]
        if any(x is None for g in vals for x in g):
            continue
        left = 0
        n_ = (_num(nums, f"repo.{s}.link.class.independent") or {}).get("n", 0)
        rec = {"panel": "link classes among repos with both files", "group": lab, "n": n_}
        for (nm, cs, col), g in zip(segs, vals):
            w = sum(g)
            ax.barh(i, w, left=left, height=.4, color=col, edgecolor=SURF, linewidth=1.5)
            if w > .06:
                ax.text(left + w / 2, i, f"{w:.0%}", ha="center", va="center", fontsize=9, color=("white" if col == ACCENT else INK))
            if i in (0, 2) and (nm != "near-copy" or i == 2):
                ax.text(left + w / 2, i - .36 if nm != "near-copy" else i + .36, {"linked: symlink, import or pointer": "linked", "near-copy": "near-copy", "independent": "independent"}[nm], ha="center", va="bottom" if nm != "near-copy" else "top", fontsize=8.5, color=(ACCENT if col == ACCENT else INK2), fontweight="bold")
            left += w
            rec[nm] = w
        for c in ("symlink", "import_bridge", "prose_pointer", "near_copy", "independent"):
            rec[c] = (_num(nums, f"repo.{s}.link.class.{c}") or {}).get("value")
        rows.append(rec)
    ax.set_yticks(range(3)); ax.set_yticklabels(["Popular", "Small & personal", "All repos"]); ax.set_ylim(2.6, -.6)
    ax.set_xlim(0, 1); ax.set_xticks([0, .5, 1]); ax.set_xticklabels(["0%", "50%", "100%"])
    ax.set_title("With both CLAUDE.md and AGENTS.md", fontsize=10, loc="left", color=INK)
    mf = _v(nums, "repo.overall.stack.multi_file", default=0)
    ind = _v(nums, "repo.overall.link.class.independent", default=0)
    _title(fig, f"{mf:.0%} of repos keep more than one instruction file; of those with both CLAUDE.md and AGENTS.md, {ind:.0%} keep two independent files",
           "Files counted: CLAUDE.md, AGENTS.md, Cursor rules, Copilot instructions, GEMINI.md, .windsurfrules; weighted share of repos",
           None, f"n = {_v(nums, 'repo.overall.stack.multi_file', 'n', 0):,} repos; {_v(nums, 'repo.overall.link.class.independent', 'n', 0):,} with both files", foot_y=-0.22)
    return _save(fig, "fig4_stack_size", rows)


TC_GROUPS = [("npm / pnpm / yarn", ["npm", "pnpm", "yarn", "bun", "mixed-js"]), ("Python (uv, poetry, pip)", ["uv", "poetry", "python-other"]),
             ("Rust (cargo)", ["cargo"]), ("Go", ["go"]), ("make / just only", ["make-only"])]


def fig_commands(tag, nums) -> list[str]:
    t = report._tables(tag)
    rp = report.load_repo(tag, t).select("owner", "repo", "toolchain", "w")
    cm = t["commands"].select("owner", "repo", "cmd").unique()
    fig, axs = plt.subplots(2, 3, figsize=(12, 7.2), gridspec_kw={"wspace": .75, "hspace": .3})
    rows, out = [], {}
    for ax, (lab, tcs) in zip(axs.flat, TC_GROUPS):
        g = rp.filter(pl.col("toolchain").is_in(tcs))
        W = float(g["w"].sum())
        top = cm.join(g, on=["owner", "repo"]).group_by("cmd").agg(pl.col("w").sum().alias("ws"), pl.len().alias("n")).sort("ws", descending=True).head(10)
        _axes(ax); ax.grid(axis="x", color=GRID, lw=.8)
        vals = [(c, ws / W, n) for c, ws, n in top.iter_rows()]
        for i, (c, v, n) in enumerate(vals):
            ax.barh(i, v, height=.55, color=ACCENT, edgecolor=SURF, linewidth=1.5)
            ax.text(v + .005, i, f"{v:.0%}", va="center", fontsize=8.5, color=INK2)
            rows.append({"toolchain": lab, "command": c, "share_weighted": v, "n_repos": n, "n_toolchain_repos": g.height})
        ax.set_yticks(range(len(vals))); ax.set_yticklabels([c for c, _, _ in vals], fontsize=8.5); ax.set_ylim(len(vals) - .5, -.5)
        ax.set_xlim(0, max([v for _, v, _ in vals] + [.1]) * 1.2); ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
        ax.set_title(f"{lab} (n = {g.height:,} repos)", fontsize=10, loc="left", color=INK)
        out[lab] = {"n_repos": g.height, "top": [{"cmd": c, "share_weighted": v, "n_repos": n} for c, v, n in vals]}
    axs.flat[-1].axis("off")
    (ROOT / "reports/top_commands.json").write_text(json.dumps(out, indent=1))
    first = out["npm / pnpm / yarn"]["top"][0] if out["npm / pnpm / yarn"]["top"] else None
    ttl = (f"{first['cmd']} is the most-mentioned command in npm-family repos ({first['share_weighted']:.0%}); every toolchain has its own short list"
           if first else "Top commands per toolchain")
    _title(fig, ttl, "Share of repos (weighted) whose CLAUDE.md or AGENTS.md mentions each command, among repos with that toolchain; top 10 per toolchain",
           "Toolchain = detected from lockfiles and manifests at the repo root. A command counts once per repo.", f"n = {sum(v['n_repos'] for v in out.values()):,} repos in these toolchains")
    return _save(fig, "fig6_top_commands", rows)


def fig_generator(tag, nums) -> list[str]:
    """Fig7: CLAUDE.md files with vs without the stock /init sentence: overview share and overview token share."""
    g = lambda k: _num(nums, f"claude.overall.g.{k}")
    fig, ax = plt.subplots(figsize=(10, 3.9))
    _axes(ax); ax.grid(axis="x", color=GRID, lw=.8)
    rows = []
    items = [("Have an overview section", "overview.share_files"), ("Share of their tokens in overview sections", "overview.token_share")]
    for i, (lab, key) in enumerate(items):
        for grp, col, off, name in (("sig", ACCENT, -.19, "With the stock line"), ("nosig", G1, .19, "Without it")):
            e = g(f"{grp}.{key}")
            y = i + off
            ax.barh(y, e["value"], height=.34, color=col, edgecolor=SURF, linewidth=1.5)
            ax.plot([e["ci_lo"], e["ci_hi"]], [y, y], color=INK, lw=1.0, alpha=.55)
            ax.text(max(e["ci_hi"], e["value"]) + .015, y, f"{e['value']:.0%}  {name}", va="center", fontsize=10.5, color=(ACCENT if grp == "sig" else INK2), fontweight="bold" if grp == "sig" else "normal")
            rows.append({"metric": lab, "group": name, "value": e["value"], "ci_lo": e["ci_lo"], "ci_hi": e["ci_hi"], "n": e["n"]})
    ax.set_yticks([0, 1]); ax.set_yticklabels([x[0] for x in items]); ax.set_ylim(1.6, -.6)
    ax.set_xlim(0, 1.18); ax.set_xticks([0, .25, .5, .75, 1]); ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
    fig.subplots_adjust(left=.30)
    sh, a, b = g("sig.share")["value"], g("sig.overview.share_files")["value"], g("nosig.overview.share_files")["value"]
    ta, tb = g("sig.overview.token_share")["value"], g("nosig.overview.token_share")["value"]
    _title(fig, f"Files with the stock /init line are far more likely to carry an overview: {a:.0%} vs {b:.0%}",
           f"CLAUDE.md files, weighted; they also spend {ta:.0%} of their tokens on overviews vs {tb:.0%}. {sh:.0%} contain \"This file provides guidance to Claude Code…\" in their first ten non-empty lines. Bars: share; thin line: 95% interval",
           "A text match, not proof of who wrote the file. " + TOKNOTE,
           f"n = {g('sig.overview.share_files')['n']:,} with the line, {g('nosig.overview.share_files')['n']:,} without", foot_y=-0.1)
    return _save(fig, "fig7_generator", rows)


def fig_hero(tag, nums) -> list[str]:
    """Hero: Anatomy of a typical CLAUDE.md. A drawn file card with measured parts annotated at the side. Only numbers.json values."""
    from matplotlib.patches import FancyBboxPatch, Rectangle
    c = lambda k, f="value": _v(nums, f"claude.overall.{k}", f)
    med, p90, p25 = c("a1.always_loaded_tokens.median"), c("a1.always_loaded_tokens.p90"), c("a1.always_loaded_tokens.p25")
    ov_f, ov_t = c("a8.overview.share_files"), c("a8.overview.token_share")
    tr_f, tr_t = c("a8.tree_dump.share_files"), c("a8.tree_dump.token_share")
    sig = c("g.sig.share")
    cmd = c("cmd.any")
    fig = plt.figure(figsize=(10, 6.6))
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 100); ax.set_ylim(0, 66); ax.axis("off")
    ax.text(3, 62, "Anatomy of a typical CLAUDE.md", fontsize=19, fontweight="bold", color=INK, va="top")
    ax.text(3, 57.6, f"The typical file loads about {med:,.0f} tokens every session (median). Here is what the census measured inside the files.", fontsize=10.5, color=INK2, va="top")
    # card
    x0, y0, w, h = 5, 8, 40, 44
    ax.add_patch(FancyBboxPatch((x0, y0), w, h, boxstyle="round,pad=0,rounding_size=1.2", fc="#FAFAF7", ec=G2, lw=1.2))
    ax.text(x0 + 2.5, y0 + h - 2.6, "CLAUDE.md", family="monospace", fontsize=10, color=INK2, va="center")
    ax.plot([x0 + 1.5, x0 + w - 1.5], [y0 + h - 5, y0 + h - 5], color=GRID, lw=1)
    # block heights proportional to token shares (measured): overview, tree, rest
    usable = h - 8.5
    parts = [("generator line", None, sig, ACCENT), ("overview", ov_t, ov_f, DEEP), ("tree", tr_t, tr_f, ACCENT), ("rest", None, None, G2)]
    hs = {"overview": usable * ov_t, "tree": usable * tr_t}
    hs["rest"] = usable - hs["overview"] - hs["tree"] - 3.2
    y = y0 + h - 7
    # generator line (fixed small band, not proportional)
    ax.add_patch(Rectangle((x0 + 2.5, y - 2.2), w - 5, 2.2, fc=ACCENT, ec="none", alpha=.9)); gy = y - 1.1
    y -= 3.2
    ys = {}
    for k in ("overview", "tree", "rest"):
        hh = hs[k]
        col = {"overview": DEEP, "tree": ACCENT, "rest": G2}[k]
        ax.add_patch(Rectangle((x0 + 2.5, y - hh), w - 5, hh, fc=col, ec="none", alpha=(.95 if k != "rest" else .55)))
        # monospace-like line hints
        n = int(hh // 1.6)
        for j in range(n):
            ax.plot([x0 + 4, x0 + 4 + (w - 9) * (0.55 + .4 * ((j * 37) % 7) / 7)], [y - 0.9 - j * 1.6] * 2, color="white", lw=1.6, alpha=.55, solid_capstyle="round")
        ys[k] = y - hh / 2
        y -= hh
    # annotations
    def note(yy, ty, big, small, col=INK):
        ax.plot([x0 + w - 1, 52], [yy, ty], color=G2, lw=.9)
        ax.plot([52, 55], [ty, ty], color=G2, lw=.9)
        ax.text(56, ty + .6, big, fontsize=13, fontweight="bold", color=col, va="center", family="monospace")
        ax.text(56, ty - 1.9, small, fontsize=9.5, color=INK2, va="center")
    note(gy, 49, f"{sig:.0%}", "of files open with the stock /init sentence (text match)", ACCENT)
    note(ys["overview"], 38.5, f"{ov_f:.0%} of files · {ov_t:.0%} of tokens", "have an overview-type section; the block above is drawn to the token share", DEEP)
    note(ys["tree"], 27.5, f"{tr_f:.0%} of files · {tr_t:.0%} of tokens", "paste a directory tree", ACCENT)
    note(ys["rest"], 15.5, f"{1 - ov_t - tr_t:.0%} of tokens", "everything else: not classified by the census, shown as unclassified", INK2)
    ax.text(56, 8.4, f"{cmd:.0%} of files name a command we recognize (lower bound)", fontsize=9.5, color=INK2, va="center")
    ax.text(56, 6.0, f"Size: 25% of files load under {p25:,.0f} tokens; the top 10% load over {p90:,.0f}", fontsize=9.5, color=INK2, va="center")
    ax.text(3, 2.6, f"neri census of public GitHub · {_date()} · n = {_v(nums, 'claude.overall.a8.overview.share_files', 'n', 0):,} CLAUDE.md files · weighted; 'typical' = median size; token shares are pooled across files, so the card shows an average composition, not one file. Block heights follow them; line hints are decorative; no file text shown. {TOKNOTE}", fontsize=7.6, color=INK2, va="center")
    rows = [{"metric": k, "value": v} for k, v in (("median_always_loaded_tokens", med), ("p90", p90), ("p25", p25), ("overview_share_files", ov_f), ("overview_token_share", ov_t), ("tree_share_files", tr_f), ("tree_token_share", tr_t), ("signature_share", sig), ("cmd_any", cmd))]
    return _save(fig, "fig_hero_anatomy", rows)


def make(tag: str | None) -> list[str]:
    _style()
    nums = json.loads((ROOT / "reports/numbers.json").read_text())["numbers"]
    out = []
    for f in (fig_pop, fig_size, fig_structure, fig_ecosystem, fig_stacks, fig_fill, fig_commands, fig_generator, fig_hero):
        out += f(tag, nums)
    return out
