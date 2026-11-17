"""figures/linkedin/carousel/slide1..6.png + carousel.pdf, built from reports/numbers.json (so they update after the boost).
1080x1350 (4:5). One finding per slide: finding -> chart -> takeaway. Palette as in postfigs (validated blue/orange)."""
from __future__ import annotations

import json
import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402

from . import config, postfigs  # noqa: E402

ROOT = config.ROOT
OUT = ROOT / "figures/linkedin/carousel"
INK, INK2, GRID, SURF, EST, TAIL = postfigs.INK, postfigs.INK2, postfigs.GRID, postfigs.SURF, postfigs.EST, postfigs.TAIL


def _slide(n, finding, takeaway, draw, source):
    fig = plt.figure(figsize=(10.8, 13.5), dpi=100, facecolor=SURF)
    fig.text(.07, .94, f"{n} / 6", fontsize=14, color=INK2, va="top")
    fig.text(.07, .90, "\n".join(textwrap.wrap(finding, 30)), fontsize=34, fontweight="bold", color=INK, va="top", linespacing=1.2)
    ax = fig.add_axes([.31, .30, .62, .34])
    ax.set_facecolor(SURF)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID); ax.tick_params(length=0, colors=INK2, labelsize=16)
    draw(ax)
    fig.text(.07, .21, "\n".join(textwrap.wrap(takeaway, 52)), fontsize=22, color=INK, va="top", linespacing=1.35)
    fig.text(.07, .05, source, fontsize=13, color=INK2, va="bottom")
    p = OUT / f"slide{n}.png"
    fig.savefig(p, facecolor=SURF)
    plt.close(fig)
    return p


def build() -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    N = json.loads((ROOT / "reports/numbers.json").read_text())["numbers"]
    v = lambda k, f="value": N[k][f]
    date = postfigs._date()
    src = lambda n: f"neri census of public GitHub · {date} · n = {n:,} repos · weighted estimates"
    pct = lambda x: f"{x:.0%}"
    n_all = v("repo.overall.pop.small_personal_share", "n")
    small = v("repo.overall.pop.small_personal_share")
    paths = []

    def d1(ax):
        ax.barh([0], [1 - small], color=EST, edgecolor=SURF, height=.5); ax.barh([0], [small], left=1 - small, color=TAIL, edgecolor=SURF, height=.5)
        ax.text((1 - small) / 2, 0, pct(1 - small), ha="center", va="center", color="white", fontsize=22)
        ax.text(1 - small / 2, 0, pct(small), ha="center", va="center", color=INK, fontsize=26, fontweight="bold")
        ax.set_xlim(0, 1); ax.set_yticks([]); ax.set_xticks([0, .5, 1]); ax.set_xticklabels(["0%", "50%", "100%"])
        ax.text(0, -.55, "blue: popular repos (10+ stars)   orange: small & personal (0–9 stars)", color=INK2, fontsize=14)
    paths.append(_slide(1, f"About {pct(small)} of repos with an agent file have fewer than 10 stars", "Studies so far looked mostly at popular repos. I looked at the whole population and weighted it.", d1, src(n_all)))

    def d2(ax):
        keys = [("CLAUDE.md", "claude"), ("AGENTS.md", "agents")]
        for i, (lab, ft) in enumerate(keys):
            for j, (s, col) in enumerate((("established", EST), ("long_tail", TAIL))):
                m = v(f"{ft}.{s}.a1.always_loaded_tokens.median")
                ax.barh(i * 3 + j, m, color=col, edgecolor=SURF, height=.8)
                ax.text(m + 20, i * 3 + j, f"{m:,.0f}", va="center", fontsize=18, color=INK)
        ax.set_yticks([.5, 3.5]); ax.set_yticklabels(["CLAUDE.md", "AGENTS.md"], fontsize=18); ax.set_ylim(5.6, -.8); ax.set_xlim(0, 2100)
        ax.set_xlabel("median tokens loaded every session (blue popular, orange small)", fontsize=14, color=INK2)
    paths.append(_slide(2, f"The typical CLAUDE.md loads about {v('claude.overall.a1.always_loaded_tokens.median'):,.0f} tokens every session", "That cost is paid on every session. Do you know yours? (o200k tokenizer, a proxy for Claude's.)", d2, src(v("claude.overall.a1.always_loaded_tokens.median", "n"))))

    def d3(ax):
        items = [("directory tree", "a8.tree_dump.share_files"), ("overview section", "a8.overview.share_files")]
        for i, (lab, k) in enumerate(items):
            for j, (ft, col) in enumerate((("claude", EST), ("agents", TAIL))):
                x = v(f"{ft}.overall.{k}")
                ax.barh(i * 3 + j, x, color=col, edgecolor=SURF, height=.8)
                ax.text(x + .01, i * 3 + j, pct(x), va="center", fontsize=18, color=INK)
        ax.set_yticks([.5, 3.5]); ax.set_yticklabels(["directory tree", "overview section"], fontsize=18); ax.set_ylim(5.6, -.8); ax.set_xlim(0, .65)
        ax.xaxis.set_major_formatter(lambda x, _: f"{x:.0%}"); ax.set_xlabel("share of files (blue CLAUDE.md, orange AGENTS.md)", fontsize=14, color=INK2)
    paths.append(_slide(3, f"{pct(v('claude.overall.a8.tree_dump.share_files'))} of CLAUDE.md files paste a directory tree; {pct(v('claude.overall.a8.overview.share_files'))} restate an overview", "ETH Zurich found repository overviews did not help agents find the right files. Worth a look at your own file.", d3, src(v("claude.overall.a8.overview.share_files", "n"))))

    def d4(ax):
        items = [("skills", "skills"), ("hooks / settings", "settings_json"), ("subagents", "agents"), ("slash commands", "commands"), ("any of the four", "any_of_four")]
        for i, (lab, k) in enumerate(items):
            x = v(f"repo.overall.a7_with_claude.{k}")
            ax.barh(i, x, color=EST, edgecolor=SURF, height=.6)
            ax.text(x + .005, i, pct(x), va="center", fontsize=18, color=INK)
        ax.set_yticks(range(len(items))); ax.set_yticklabels([a for a, _ in items], fontsize=18); ax.set_ylim(len(items) - .4, -.6); ax.set_xlim(0, .4)
        ax.xaxis.set_major_formatter(lambda x, _: f"{x:.0%}"); ax.set_xlabel("share of repos with a CLAUDE.md", fontsize=14, color=INK2)
    paths.append(_slide(4, f"{pct(1 - v('repo.overall.a7_with_claude.any_of_four'))} of repos with a CLAUDE.md use no skills, hooks, subagents or commands", "Most setups are one plain file. The advanced features are a minority practice.", d4, src(v("repo.overall.a7_with_claude.skills", "n"))))

    br = v("repo.overall.link.linked_any")
    ind = v("repo.overall.link.class.independent")

    def d5(ax):
        ax.barh([0], [br], color=INK2, edgecolor=SURF, height=.5); ax.barh([0], [1 - br], left=br, color=GRID, edgecolor=SURF, height=.5)
        ax.text(br / 2, 0, pct(br), ha="center", va="center", color="white", fontsize=24); ax.text(br + (1 - br) / 2, 0, pct(1 - br), ha="center", va="center", color=INK, fontsize=24)
        ax.set_xlim(0, 1); ax.set_yticks([]); ax.set_xticks([0, .5, 1]); ax.set_xticklabels(["0%", "50%", "100%"])
        ax.text(0, -.55, "dark: linked (symlink, import or pointer)   light: near-copy or independent", color=INK2, fontsize=14)
    paths.append(_slide(5, f"Repos with both CLAUDE.md and AGENTS.md: {pct(br)} link one to the other, {pct(ind)} keep independent files", "Link = symlink, import or a pointer line; the rest are near-copies. Classes are pattern-based and hand-checked on a sample.", d5, src(v("repo.overall.link.class.independent", "n"))))

    def d6(ax):
        ax.axis("off")
        lines = ["1  Measure what you load every session.", "2  Question pasted trees and overviews.", "3  Link CLAUDE.md and AGENTS.md if you keep both.", "", "Check your file against the data:", "[link to the post and the page]"]
        for i, t in enumerate(lines):
            ax.text(0, 1 - i * .16, t, fontsize=22, color=INK, transform=ax.transAxes)
    paths.append(_slide(6, "What I would try", "These are proposals scoped to this sample. The post says what would change my mind.", d6, "A personal side project using public GitHub data only. Not affiliated with any employer."))
    imgs = [Image.open(p).convert("RGB") for p in paths]
    imgs[0].save(OUT / "carousel.pdf", save_all=True, append_images=imgs[1:], resolution=100)
    return f"wrote {len(paths)} slides + carousel.pdf in figures/linkedin/carousel/"


if __name__ == "__main__":
    print(build())
