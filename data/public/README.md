# neri post 1 dataset (pointers + derived features, no text)

What is here
- `candidates-frozen.parquet`, `buckets-frozen.parquet`, `repo-meta-snapshot.parquet`, `FROZEN.json`: the frozen candidate pointer
  list (owner, repo, path, blob sha, size bucket, inclusion probability, split) and the snapshot metadata. Start here to reproduce
  everything downstream (split, fetch at pinned shas, features, statistics). The code-search enumeration itself is not reproducible.
- `neri-post1-features.parquet`: one row per sampled NON-DEV repo and file type with derived features only: size and token counts
  (o200k_base proxy), structure type, style rates, tree-dump / overview token counts, stack and bridge flags, ecosystem flags,
  toolchain, stratum, population size N_h, sampling probability, inclusion probability and the final weight.
- `neri-post1-commands.parquet`: normalized command names per repo (e.g. `npm:test`), not command text. Script and Python-file names are generalized to `script:(file)` / `python:(file)`, and any command seen in fewer than 20 repos is `(rare)`.
- Dropped from the features: the instruction-smell flags (detector precision 8/20 on hand review; see docs/METHODS.md).

What is NOT here: any text of any CLAUDE.md / AGENTS.md / SKILL.md file, any quote, any path string taken from a file.
Population: public, active, non-archived repositories that GitHub code search indexes; see docs/DECISIONS.md (limitations).
Weights: `w = 1 / (incl_prob_repo x sample_prob)`; the weighted overall estimate is defined once in the methods box and in
numbers.json (`definitions.overall`).

License: CC BY 4.0 for the derived numbers and features (see LICENSE in this folder; attribution: Lakshmanan Meiyappan, "neri"). The pointers (owner, repo, path,
pinned sha) only reference third-party repositories, which remain under their own licenses (SPDX ids are in
`repo-meta-snapshot.parquet`; many repos are unlicensed). No third-party text is included.
