# FIGURE-STYLE.md: how post-1 charts should look

Goal: charts that read like a good editorial publication (FT, The Pudding, Our World in Data), not default plotting output and not "AI-generated". Light mode, so they share well on LinkedIn and read on Substack.

## Canvas
- Background white #FFFFFF (or a very light warm off-white #FAFAF7). Text #1A1A2E (never pure black). Muted text #6B6B76.
- Size: 1600×1000 px PNG at 2× for the post; a 1200×1200 square variant for LinkedIn/carousel; SVG for the site.
- Generous margins (≥ 64 px). Whitespace does the work.

## Color
- ONE accent: brand purple #8B5CF6 for the thing the title talks about. Everything else in grays (#C9C9D1, #9A9AA6).
- A second series, if unavoidable: deep purple #5B3DB8, or a dark gray. No rainbow, no green/red/orange.
- Never use color as the only cue: label directly.

## Type
- Inter for titles and labels (title 600 at 28–32 px, labels 400 at 16–18 px); JetBrains Mono for numbers in callouts.
- Title = the finding as a sentence ("A third of CLAUDE.md files still start the way the generator left them"). Subtitle = what's plotted + n, in muted text.
- Footer, small and muted: "neri census of public GitHub · Nov 2026 · n = …" and, if relevant, "weighted; 95% intervals".

## Marks and labels
- Label directly on the chart (series names at the line end, values at the bar end). No legends unless unavoidable.
- One or two annotations per chart: a thin leader line + short text pointing at the thing that matters ("median: 1,374 tokens").
- Horizontal bars for categories, sorted. Thin bars, rounded ends only if subtle (≤ 2 px).
- Light horizontal gridlines only (#EEEEF2), no axis boxes, no tick clutter; show only the ticks a reader needs.
- Intervals: a thin whisker or light band, never a heavy error bar.
- Percent labels rounded sensibly ("43%", not "43.14%").

## Never
- Gradients, 3D, shadows, glow, emoji, stock icons, robots, clip-art, word clouds, pie/donut charts.
- Default matplotlib/seaborn look (blue/orange defaults, boxed axes, legend in a corner).
- More than ~6 categories in one chart; use "other".
- Third-party file text in any image.

## The hero infographic: "Anatomy of a typical CLAUDE.md"
- A drawn file card (light page, subtle border, monospace hints of lines) showing a typical file's measured parts, annotated at the side with leader lines:
  - total always-loaded tokens (median) and the top-10% threshold;
  - overview-type sections ≈ their share of tokens;
  - directory-tree dumps (share of files);
  - the generator/template header (share starting with it);
  - commands present (share);
  - "everything else": labeled honestly as unclassified.
- Only measured numbers from numbers.json; say "typical" = median. No invented proportions.

## Chart list for the post (each title states the finding)
0. Hero: anatomy of a typical CLAUDE.md
1. Who has these files: AGENTS.md vs CLAUDE.md; 9 in 10 in small repos
2. The cost: always-loaded tokens (ECDF or simple distribution), popular vs small labeled directly
3. What fills it: overview sections + tree dumps, with a small "research says" note
4. One file, rarely split: structure shares
5. Two names, one file: link / copy / independent among repos with both
6. Beyond the file: settings, hooks (when they run), skills
7. (More numbers) top commands by toolchain: small multiples

## "Post in one image" summary card (approved direction, Nov 16 v4; replaces earlier spec)
Purpose: the whole post at a glance, calm and editorial (NOT a marketing card). It can also stand alone as a LinkedIn post.
Sizes: 1080×1350 portrait (primary), 1200×630 (link preview), 1080×1080 square. Export PNG @2x + SVG to figures/social/.
Layout: white background, thin border, generous margins.
- Title (Inter 500): "What I learned from {{n.files.total}} CLAUDE.md and AGENTS.md files"
- Subline (muted): "From {{n.repo.overall}} public GitHub repos, weighted to the whole population"
- Five rows separated by hairlines. Left: a purple mono number (01–05) + one plain sentence. Right: a SMALL chart drawn from real data (no hand-sketched shapes):
  01 "AGENTS.md is now more common than CLAUDE.md. Many repos keep both." Two bars under the caption "share of repos that have it": AGENTS.md {{repo.overall.stack.has_agents_md}} (purple) vs CLAUDE.md {{repo.overall.stack.has_claude_md}} (gray). Use the SAMPLE-based shares, not GitHub-wide index counts, so every number on the card refers to the same {{n.repo.overall}} repos. (Index counts 1.2M / 877K stay in the post body, labeled as GitHub-wide.)
  02 "The typical CLAUDE.md loads about {{median}} tokens at the start of every session." A scale bar with "typical" marked at the median and "top 10%: {{p90}}+" at the end.
  03 "About a sixth of those tokens are project overviews. Research found overviews didn't help agents find the right code." A tiny file icon drawn as lines, with the top band shaded to the real overview token share (label "overview ≈ 1/6").
  04 "A quarter of CLAUDE.md files still start the way /init wrote them. Most overviews come from those." Two labeled bars under the caption "share with an overview section": "kept /init's start" {{g.sig.overview.share_files}} (purple) vs "all other files" {{g.nosig.overview.share_files}} (gray).
  05 "I expected well-known projects to write better files. Repos with 10+ stars write almost the same as everyone else. There's no best practice yet." Mini version of Fig 1: the two real size distributions (10+ stars purple solid; everyone else gray dashed), labeled directly.
- Closing line: "The full write-up has the method, the data, and what I'd change in my own file."
- Footer: "internals.laxmena.com" (purple) left; "INTERNALS.md #6 · Laxmena" (mono, muted) right.
Rules: every bar length and curve from numbers.json / figure CSVs; ~110 words max; FIGURE-STYLE rules apply.
Typography/layout update (v5, Nov 16): smaller text: title ~18 px equivalent at mockup scale (≈ 44 px on a 1080-wide card), sentences ≈ 12.5 px (≈ 30 px at 1080), labels 9–10 px (≈ 22–24 px). Right-hand chart column fixed width (~200 px mockup ≈ 360 px at 1080); every label must fit inside its chart box with no overlap (check bounding boxes); series names go under or beside bars, values at bar ends.
