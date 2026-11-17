"""data/public/: the dataset for post 1 = frozen pointers (pointers.py) + derived per-repo features. NO third-party file text:
only identifiers (owner/repo, pinned sha, path), numbers, booleans and normalized command names (e.g. npm:test)."""
from __future__ import annotations

import polars as pl

from . import config, devset, report

MIN_REPOS = 20
ROOT = config.ROOT
PUB = ROOT / "data/public"
README = """# neri post 1 dataset (pointers + derived features, no text)

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
"""


def export(tag: str | None) -> str:
    t = report._tables(tag)
    frames = []
    for ft, name in report.FILES.items():
        df = report.load_file(tag, name, t)
        if df.height:
            frames.append(df.with_columns(file_type=pl.lit(name)))
    feat = pl.concat(frames, how="diagonal").drop([c for c in ("path",) if c in frames[0].columns])
    feat = feat.drop([c for c in feat.columns if c.startswith("smell_")])  # dropped finding: detector precision 8/20
    rp = report.load_repo(tag, t).select("owner", "repo", "stack_size", "members", "bridge_kind", "bridge", "agents_symlink_to_claude",
                                         "n_skills", "n_skills_scripts", "skills", "commands", "agents", "settings_json", "cursor_rules",
                                         "copilot_instructions", "mcp_json")
    feat = feat.join(rp, on=["owner", "repo"], how="left")
    PUB.mkdir(parents=True, exist_ok=True)
    feat.write_parquet(PUB / "neri-post1-features.parquet")
    cm = t["commands"].select("owner", "repo", "name", "cmd", "n")
    # no names taken from third-party files: script / python-file basenames are generalized, and any command seen in fewer than
    # MIN_REPOS repos becomes "(rare)" (k-anonymity), so custom script and make-target names cannot identify a repo
    cm = cm.with_columns(cmd=pl.when(pl.col("cmd").str.starts_with("script:")).then(pl.lit("script:(file)"))
                         .when(pl.col("cmd").str.starts_with("python:")).then(pl.lit("python:(file)")).otherwise(pl.col("cmd")))
    nrep = cm.group_by("cmd").agg(pl.struct("owner", "repo").n_unique().alias("_n"))
    cm = cm.join(nrep, on="cmd").with_columns(cmd=pl.when(pl.col("_n") >= MIN_REPOS).then(pl.col("cmd")).otherwise(pl.lit("(rare)"))).drop("_n")
    cm.write_parquet(PUB / "neri-post1-commands.parquet")
    (PUB / "README.md").write_text(README)
    return f"wrote data/public/neri-post1-features.parquet ({feat.height:,} rows), neri-post1-commands.parquet, README.md"
