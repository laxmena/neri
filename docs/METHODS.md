# Methods, limitations and validation

Everything here is descriptive: the census counts what people wrote in public, committed instruction files. It does not measure
whether any instruction works. Numbers are in `reports/numbers.json`; figure tables are `figures/post/*.csv`.

## Population and sampling
- **Population:** public, non-fork, non-archived GitHub repositories that GitHub code search returns for `CLAUDE.md` or `AGENTS.md`
  (200 B to 100 KB, default branch), pushed in the 180 days before the snapshot (15 Nov 2026).
- **Enumeration:** code search caps each query at 1,000 hits, so file names were partitioned by size into buckets and a seeded random
  sample of buckets was paged. Each repo's inclusion probability was recorded. The frozen result is `data/public/candidates-frozen.parquet`.
- **Strata:** popular (10+ stars) and small & personal (0-9 stars). Every eligible repo in the sampled candidate pool was measured in
  both strata (33,762 repos with a readable file: 17,960 CLAUDE.md and 21,575 AGENTS.md files). The remaining uncertainty is the
  search enumeration (inclusion probabilities), not drawing repos.
- **Weights:** `w = 1 / (inclusion probability x sampling probability)`. "Overall" means public, active, non-archived repos that code
  search indexes. A development set of 2,508 repos was used to build detectors and is excluded from every number.
- **Estimates:** 95% bootstrap intervals clustered by repo (1,000 resamples); ratio estimators for token shares; a trimmed-weights
  (99th percentile) sensitivity check flagged 0 of 2,033 cells.
- **Tokens:** `o200k_base` via tiktoken, a proxy for Claude's tokenizer (not calibrated).
- **Held-out split:** a hash of owner/repo reserves a confirmation half that was never featurized.

## Definitions that matter
- **Always-loaded tokens:** the root file plus `@imports`. **On-demand tokens:** nested instruction files.
- **Shouting:** the words IMPORTANT, CRITICAL, MUST, NEVER or ALWAYS in capitals outside code fences. ALL-CAPS lines alone were
  checked by hand (1 of 20 was a real reader-directed emphasis line) and excluded.
- **Link classes** (repos with both root files): symlink, `@`-import, prose pointer, near-copy (>=80% shared lines), independent.
- **Generator signature:** the sentence "This file provides guidance to Claude Code (claude.ai/code) when working with code in this
  repository" in the first ten non-empty lines. Confirmed as the header written by `/init` in Claude Code 2.1.326 (run in an empty
  folder). A text match, not provenance: a file may have been edited or the line removed.
- **Hooks:** command-type hooks in committed `.claude/settings*.json`; one purpose class per command by first-match keyword patterns.
- **Co-presence:** root-anchored path presence in the repo tree ("also configure", not market share); truncated trees give lower bounds.
- **Languages:** offline detector (lingua) on prose only; approximate.

## What the census cannot see
Private repos; personal or global instruction files; `CLAUDE.local.md` and uncommitted settings; forks, archived repos, files over
384 KB, non-default branches; repos with no push in 180 days; who or what wrote a file; whether anyone runs a hook or skill; whether
an instruction is right.

## Validation
- **Command extraction:** 19 of 20 random commands correct (one `bash <script>` reduced to `bash`; fixed with a regression test).
- **Link classes:** my rule matched hand labels on 26 of 30 pairs; the rule was tightened and matched 23 of 25 unseen pairs.
- **Instruction-file type:** 39 of 40 sampled AGENTS.md and 20 of 20 CLAUDE.md were instruction files (one was an agent catalog).
- **Independent labelers:** check sheets were labeled blind by three models: Claude Sonnet 5.5 (the drafting model), Gemini
  (`google/gemini-4-hydrogen` via OpenRouter, temperature 0, JSON output, run 2026-11-16; only the first lines of public files were
  sent) and Codex (codex-cli 0.169.0, GPT-5.6 Sol). A human adjudicated every disagreement and a random sample of agreed rows; the
  human label is final. The models agreed with each other on 30/30 link rows, 60/60 file-type rows and 25/25 held-out link rows;
  high agreement may reflect easy rows and shared definitions.
- **Settings parser:** 5 of 5 human-checked settings files correct, including the hook commands.
- **Hook purpose classes:** keyword patterns; a 20-command check sheet is part of the author's review. The largest class is "other".
- **Earlier spot check:** a 10-row human check of an early wrongness detector agreed on 7 of 10 (kappa 0.40) and exposed detector blind
  spots, which is why no wrongness percentages are reported.

## Detectors dropped, and why
- **Package-manager mismatch:** precision 2 of 20 on hand review. Cut. A later version should restrict to install commands and re-validate.
- **Instruction "smells"** (vague phrases, repeated items, contradictions, template tokens): precision 8 of 20. Cut; the flags are
  removed from the public features.
- **ALL-CAPS lines as emphasis:** precision 1 of 20. Cut; shouting is keyword-only.
- **Wrongness rates** (dead paths, dead commands): not reported; self-labeling bias and detector blind spots made them unreliable.

## Limitations
Proxy tokenizer; stars as a popularity proxy; code-search enumeration is not reproducible; the age baseline uses GitHub search totals
that were not independently verified (agent-file repos are not unusually young, so age is not a finding); keyword patterns are not judgments.
