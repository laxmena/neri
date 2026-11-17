> **Historical design document.** This is the original plan for post 1. The shipped scope and the validation results are in [METHODS.md](METHODS.md); the original "how much is already wrong" headline was dropped (see METHODS.md, "Detectors dropped, and why").

# neri — Post 1 design: "What developers tell their coding agents, and how much of it is already wrong"

Status: FINAL for post 1. Changes go through a dated note in JOURNAL.md.
Scope: CLAUDE.md + AGENTS.md on public GitHub. A standalone INTERNALS.md post.
The preregistered paper (PREREGISTRATION.md, if present) is separate; this post must not touch its confirm set.

## 1. What the reader gets
1. **A benchmark:** what a typical file looks like (size, token cost, structure, topics).
2. **A finding:** how often these files are wrong (dead paths, dead commands, wrong toolchain, CLAUDE.md vs AGENTS.md contradictions).
3. **A trend:** how files changed over time, lined up against model and tool releases (descriptive only).
4. **A tool:** `neri check` lints your own repo's agent file.
5. **A dataset:** pointers (repo, sha, path) plus derived features. No file text.

## 2. Selection filter
| Filter | Value |
|---|---|
| File names | `CLAUDE.md`, `AGENTS.md` (case-sensitive), at repo root = primary unit. Nested ones are counted separately. |
| Repo | public, not a fork, not archived, stars >= 10, pushed within 180 days of the snapshot date |
| File size | 200 B – 100 KB |
| Symlinks | resolved; AGENTS.md -> CLAUDE.md counts as one file plus a `symlinked` flag |
| Duplicates | exact (sha256) and near (MinHash, Jaccard >= 0.8) are **flagged, not dropped**. The duplication rate is a finding. |
| Sample | ~10K repos, stratified by stars (10–99 / 100–999 / 1K+) x file type (CLAUDE only / AGENTS only / both) |
| Split | deterministic: `sha256(owner/repo) % 2` -> 0 = explore, 1 = confirm. **Post 1 uses explore only.** Confirm repos are stored as ids only; no features are computed for them. |
| Snapshot | every file is read at a pinned commit sha; the snapshot date is recorded in config.toml |

## 3. Analyses
All analyses are deterministic Python. An LLM is used only in A6, and only for validation.

**A1 Shape and cost**
- Lines, headings, code blocks, links for each file.
- Tokens: count with `tiktoken o200k_base` as a proxy, and say so in the post. Calibrate once on 50 files against Anthropic's count_tokens if available.
- **Always-loaded cost** = root file + recursively resolved `@path` imports. **On-demand cost** = nested CLAUDE.md/AGENTS.md files in subfolders.
- Structure type: single file / imports / nested / both.

**A2 Wrongness (headline)**
- **Dead paths:** paths mentioned in the file (backticks, links, `@imports`, `scripts/x.sh`) that are missing from the git tree at the pinned sha.
- **Dead commands:** `npm|pnpm|yarn|bun run X`, `make X`, `just X`, `uv run X`/`poetry run X`, `cargo X` aliases, where X is not defined in package.json scripts / Makefile targets / justfile / pyproject scripts.
- **Toolchain mismatch:** the file says npm but the repo has pnpm-lock.yaml or yarn.lock or bun.lockb; the file says pip but the repo uses a uv.lock or poetry.lock.
- **Excluded from "dead":** paths with placeholders (`<x>`, `{x}`, `...`), globs, URLs, generated/ignored paths (match .gitignore), and paths the text says to create.
- Precision must be >= 90% on the 50-reference hand check (M5) before any number is published.

**A3 Instruction style**
- Shouting: count of IMPORTANT/CRITICAL/MUST/NEVER/ALWAYS and ALL-CAPS lines, per 1K tokens.
- Negations: share of rule lines with never/don't/do not/avoid.
- Imperative bullet share.

**A4 Drift (repos with both files)**
- identical / symlink / one imports the other / divergent.
- For divergent pairs: line-level Jaccard similarity, and contradicting commands (different test or build command).

**A5 Copying**
MinHash clusters; % of files in clusters of >= 5; identify `/init`-style boilerplate clusters.

**A6 Topics**
- Classify each section by heading plus keyword rules into: commands/build/test, architecture/layout, code style, workflow/git/PR, prohibitions/safety, persona/tone, domain knowledge, tooling/MCP, other.
- Validation: Claude Haiku via the Batch API labels 300 random sections. Report rule-vs-LLM agreement. If agreement is under 85%, use LLM labels for all sections (Batch API with prompt caching; budget cap $30).

**A7 Ecosystem (free, from the tree)**
% of repos with `.claude/skills/`, `.claude/commands/`, `.claude/agents/`, `.claude/settings.json` (hooks), `.cursor/rules`, `.github/copilot-instructions.md`, `.mcp.json`.

**A8 Time and models (subsample ~1.5K explore repos, stratified)**
- Full commit history of the root file(s): the commits API filtered by path, capped at 30 versions per file.
- Recompute A1, A3 and A6 for every version.
- `data/ref/model_timeline.csv`: release dates of Claude models, Claude Code milestones, `/init` template changes (if findable), the AGENTS.md launch, and major Codex/Cursor releases. **Every row needs a source URL from official release notes or changelogs. Nothing from memory.**
- Views:
  - cohort (file creation date -> era)
  - within-file trajectories
  - trend lines with release markers
- Confound checks: boilerplate share per era, and a repo-age control. The wording in the post stays descriptive ("co-occurs with"), never causal.

## 4. Statistics
- Report medians, IQR and percentages with 95% bootstrap CIs (1,000 resamples, clustered by repo).
- Weight by stratum so that population estimates are reweighted to the candidate-pool proportions. Report both weighted and unweighted values.
- Plot raw distributions before any summary. Exploratory figures go in `figures/explore/`; post figures go in `figures/post/`.
- Every number in the post comes from `reports/numbers.json` (key -> value, CI, n, query).

## 5. GitHub access and rate limits
- Auth: `gh auth token`. Never print or log the token.
- **Enumeration:** code search (`filename:CLAUDE.md`, `filename:AGENTS.md`), split by `size:` ranges until each query returns < 1,000 results. Budget ~10 requests/min; respect `Retry-After` and `X-RateLimit-Reset`.
- **Repo metadata:** GraphQL in batches of 100 (stars, isFork, isArchived, pushedAt, createdAt, licenseInfo.spdxId, defaultBranchRef target oid).
- **Trees:** `GET /repos/{o}/{r}/git/trees/{sha}?recursive=1`, one call per repo, cached gzipped JSON. Use ETags.
- **File contents:** `raw.githubusercontent.com/{o}/{r}/{sha}/{path}`; this doesn't count against the API limit.
- **Common to all calls:** exponential backoff with jitter; every stage resumable and idempotent (skip if the cached output exists); one global request log `data/log/requests.jsonl`.

## 6. Licenses and ethics
- Store the SPDX id per repo. Many repos are unlicensed, which means all rights reserved.
- **Never redistribute file text.** The published dataset is pointers plus derived numeric/categorical features only.
- Quotes in the post: under 2 lines, only from MIT/Apache-2.0/BSD/ISC/CC-BY repos, with attribution.
- Examples of wrong files are shown **anonymized** (no repo names). The point is the pattern, not shaming anyone.
- Research use of public data via the official API at polite rates. No scraping of HTML.

## 7. Token discipline (for the agent running this)
- No LLM in the pipeline except A6 validation and fallback.
- The agent never reads raw files or parquet into its context. It reads only `uv run neri ...` summaries (<= 50 lines) and at most 5 example files per investigation.
- Every stage prints a compact summary and writes details to disk.

## 8. Outputs
- `reports/numbers.json`
- `figures/post/*.svg` (plus PNG)
- `data/public/neri-post1-pointers.parquet` (no text)
- `docs/post1-draft.md` (outline + draft)
- `neri check` CLI (installable: `uvx --from git+https://github.com/laxmena/neri neri check`; the PyPI name, if published, is `neri-check` because `neri` is taken)

## 9. Out of scope for post 1
Skills content and execution, other agent-config formats beyond the A7 presence flags, causal claims, the confirm set, monthly refresh.
