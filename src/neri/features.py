"""M4 driver: A1 shape/cost, A2 wrongness, A3 style, A4 drift, A5 copying, A7 ecosystem. Explore repos only.
Reads data/raw/fetch_index.jsonl (which is built from explore.parquet only) and cached blobs/trees. No network, no LLM."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import PurePosixPath

import polars as pl

from collections import Counter

from . import census, classify, config, core, devset, extras, strata
from . import enumerate as en

ROOT = config.ROOT
BLOBS, TREES = ROOT / "data/blobs", ROOT / "data/trees"
_enc = None


def tokens(text: str) -> int:
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding("o200k_base")  # proxy tokenizer; the post says so
    return len(_enc.encode(text, disallowed_special=()))


def _blob(sha: str) -> str | None:
    p = BLOBS / sha[:2] / sha
    return p.read_text(errors="replace") if p.exists() else None


def _tree_paths(o: str, r: str, sha: str) -> list[str]:
    p = TREES / f"{o}__{r}__{sha[:12]}.json.gz"
    d = json.loads(gzip.decompress(p.read_bytes()))
    return [t["p"] for t in d["tree"] if t["t"] in ("blob", "commit")]


def _reach(imports: list[dict], start: str) -> list[dict]:
    seen, out, stack = {start}, [], [start]
    while stack:
        cur = stack.pop()
        for i in imports:
            if i["from"] == cur and i["to"] not in seen:
                seen.add(i["to"]); out.append(i); stack.append(i["to"])
    return out


def _minhash(text: str, perm: int):
    from datasketch import MinHash
    w = re.findall(r"\w+", text.lower())
    sh = {" ".join(w[i:i + 5]) for i in range(max(1, len(w) - 4))}
    m = MinHash(num_perm=perm)
    for s in sh:
        m.update(s.encode())
    return m


def _skills(keep: set) -> tuple[list, list, list]:
    """Per-skill features from data/raw/skills_index.jsonl (explore repos only)."""
    idx = ROOT / "data/raw/skills_index.jsonl"
    rows, scmds, stools = [], [], []
    if not idx.exists():
        return rows, scmds, stools
    for line in open(idx):
        j = json.loads(line)
        if (j["owner"], j["repo"]) not in keep:
            continue
        for s in j["skills"]:
            text = _blob(s["blob_sha"]) if s.get("fetched") else None
            if text is None:
                continue
            fm = census.frontmatter(text)
            tools = census.allowed_tools(fm)
            cl = census.extract_all(text)
            refs = census.skill_script_refs(text)
            rows.append({"owner": j["owner"], "repo": j["repo"], "skill": s["name"], "linked": s["linked"],
                         "tokens": tokens(text), "lines": len(text.splitlines()), "has_frontmatter": bool(fm),
                         "description_len": len(fm.get("description", "")), "n_allowed_tools": len(tools),
                         "n_commands": len(set(cl)), "n_script_refs": len(refs), "n_files": s["n_files"],
                         "n_script_files": s["n_script_files"]})
            for cmd, k in Counter(cl).items():
                scmds.append({"owner": j["owner"], "repo": j["repo"], "skill": s["name"], "cmd": cmd, "n": k})
            for tname in set(c.split(":")[0] for c in cl):
                stools.append({"owner": j["owner"], "repo": j["repo"], "skill": s["name"], "tool": tname, "source": "command"})
            for tname in tools:
                stools.append({"owner": j["owner"], "repo": j["repo"], "skill": s["name"], "tool": tname, "source": "allowed-tools"})
    return rows, scmds, stools


def run(tag: str | None, log=print) -> list[str]:
    cfg = config.load()
    lo, hi = cfg["filter"]["min_bytes"], cfg["filter"]["max_bytes"]
    sample_dir = ROOT / "data/sample" / (f"c{tag}" if tag else "")
    out_dir = ROOT / "data/features" / (f"c{tag}" if tag else "")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = strata.load_explore(tag)
    keep = {(o, r) for o, r in meta.select("owner", "repo").iter_rows()}  # explore sample only
    cand = pl.read_parquet(ROOT / "data/raw" / ("checkpoints/" + f"c{tag}.parquet" if tag else "candidates.parquet"))
    pname = dict(cand.group_by("file_name").agg(pl.col("incl_prob").first()).iter_rows())
    recs = list({(j["owner"], j["repo"]): j for j in map(json.loads, open(ROOT / "data/raw/fetch_index.jsonl"))
                 if (j["owner"], j["repo"]) in keep}.values())  # last line per repo wins (nested-only repos were re-fetched)
    lookup = strata.bucket_lookup(en._load_buckets())

    resolved: dict[tuple, dict] = {}
    rp = ROOT / "data/raw/truncated_resolved.jsonl"
    if rp.exists():
        for line in open(rp):
            j = json.loads(line)
            if j.get("ref") is not None:
                resolved.setdefault((j["owner"], j["repo"]), {})[j["ref"]] = j["exists"]
    subman: dict[tuple, dict] = {}
    sp = ROOT / "data/raw/subdir_manifests.jsonl"
    if sp.exists():
        for line in open(sp):
            j = json.loads(line)
            subman[(j["owner"], j["repo"])] = {d: {n: _blob(h) or "" for n, h in ms.items()} for d, ms in j["dirs"].items()}
    dev_df = devset.load()
    dev_keys = set(dev_df.iter_rows()) if dev_df is not None else set()
    a1f, a1r, a2r, a2d, a3, a4, a7, repos, cmds, a8, stacks = [], [], [], [], [], [], [], [], [], [], []
    texts: dict[str, str] = {}  # for A5: key owner/repo/path -> text
    for rec in recs:
        o, r = rec["owner"], rec["repo"]
        if rec["status"] not in ("ok", "partial"):
            continue
        files, imports = rec["files"], rec.get("imports", [])
        tree = _tree_paths(o, r, rec["sha"])
        names_root = {p for p in tree if "/" not in p}
        man = {n: t for n, h in rec.get("manifests", {}).items() if (t := _blob(h)) is not None}
        # symlinks: resolve target; the target row gets symlinked=True, the link row is dropped
        by_path = {f["path"]: f for f in files}
        for f in files:
            if f.get("symlink") and f.get("symlink_target"):
                tgt = str(PurePosixPath(PurePosixPath(f["path"]).parent / f["symlink_target"]))
                if tgt in by_path:
                    by_path[tgt]["symlinked"] = True
                f["_drop"] = True
        usable = [f for f in files if not f.get("_drop") and (f["root"] is False or lo <= (f["size"] or 0) <= hi)]
        text_of = {f["path"]: _blob(f["blob_sha"]) for f in usable}
        nested_only = bool(rec.get("nested_only"))
        if nested_only:  # primary file = the shallowest (then largest) nested CLAUDE.md chosen at fetch time
            roots = [f for f in usable if f["path"] == rec.get("primary_path") and text_of[f["path"]] is not None]
        else:
            roots = [f for f in usable if f["root"] and text_of[f["path"]] is not None]
        if not roots:
            continue
        nested_tok = sum(tokens(text_of[f["path"]]) for f in usable if not f["root"] and text_of[f["path"]])
        for f in usable:
            t = text_of[f["path"]]
            if t is None:
                continue
            a1f.append({"owner": o, "repo": r, "path": f["path"], "root": f["root"], "name": f["name"],
                        "symlinked": bool(f.get("symlinked")), **core.shape(t), "tokens": tokens(t),
                        "primary": any(f is p for p in roots)})
        for f in roots:
            t = text_of[f["path"]]
            imp = _reach(imports, f["path"])
            imp_tok = sum(tokens(_blob(i["blob_sha"]) or "") for i in imp)
            n_tok = tokens(t)
            struct = "nested-only" if nested_only else ("both" if imp and nested_tok else "imports" if imp else "nested" if nested_tok else "single")
            a1r.append({"owner": o, "repo": r, "name": f["name"], "tokens": n_tok, "import_tokens": imp_tok,
                        "always_loaded_tokens": 0 if nested_only else n_tok + imp_tok, "on_demand_tokens": nested_tok,
                        "nested_only": nested_only,
                        "n_imports": len(imp), "structure": struct, "symlinked": bool(f.get("symlinked"))})
            if nested_only:  # A2 is not computed for nested-only repos (their paths may be relative to the file's folder)
                cp, cc = [], []
            else:
                cp = classify.classify_paths(t, tree, man.get(".gitignore"))
                cc = classify.classify_commands(t, tree, man, subman.get((o, r)))
            if rec.get("tree_truncated"):  # a partial listing cannot prove absence: walk-confirmed alive, else UNKNOWN
                cp = core.apply_truncation(cp, resolved.get((o, r), {}))
                cc = [{**x, "dead": False, "unknown": True, "category": "UNKNOWN", "reason": "truncated_tree"} if x["category"] == "STALE" else x for x in cc]
            n_ = lambda xs, *cats: sum(1 for x in xs if x["category"] in cats)
            tc = core.toolchain_mismatch(t, names_root)
            a2r.append({"owner": o, "repo": r, "name": f["name"],
                        "paths_checked": n_(cp, "ALIVE", "STALE"), "paths_dead": n_(cp, "STALE"),
                        "paths_dead_moved": sum(1 for x in cp if x["category"] == "STALE" and x["moved"]),
                        "paths_unknown": n_(cp, "UNKNOWN"), "paths_created": n_(cp, "EXPECTED_CREATED"), "paths_notref": n_(cp, "NOT_A_REFERENCE"),
                        "cmds_checked": n_(cc, "ALIVE", "STALE"), "cmds_dead": n_(cc, "STALE"), "cmds_unknown": n_(cc, "UNKNOWN"),
                        "cmds_created": n_(cc, "EXPECTED_CREATED"), "cmds_notref": n_(cc, "NOT_A_REFERENCE"),
                        "tree_truncated": bool(rec.get("tree_truncated")), "js_mismatch": tc["js_mismatch"], "py_mismatch": tc["py_mismatch"]})
            for typ, xs in (("path", cp), ("cmd", cc)):
                for x in xs:
                    a2d.append({"owner": o, "repo": r, "name": f["name"], "type": typ, "kind": x["kind"],
                                "ref": x["ref"] if typ == "path" else f"{x['tool']} {x['target']}", "category": x["category"],
                                "reason": x["reason"], "line": x["line"], "cwd": "?" if x["cwd"] is None else x["cwd"],
                                "dead": x["category"] == "STALE", "unknown": x["category"] == "UNKNOWN",
                                "moved": bool(x.get("moved")), "moved_to": x.get("moved_to") or "",
                                "evidence": json.dumps(x.get("evidence") or {})})
            a3.append({"owner": o, "repo": r, "name": f["name"], **core.style(t, n_tok)})
            dsp, osp = extras.tree_dump_spans(t), extras.overview_sections(t)  # POST1-PLAN section 3.1 and 3.2
            a8.append({"owner": o, "repo": r, "name": f["name"], "tokens": n_tok,
                       "tree_dump": bool(dsp), "tree_dump_lines": sum(b - a for a, b in dsp),
                       "tree_dump_tokens": tokens(extras.span_text(t, dsp)) if dsp else 0,
                       "overview": bool(osp), "overview_sections": len(osp),
                       "overview_tokens": tokens(extras.span_text(t, osp)) if osp else 0})
            tc_label = census.toolchain(names_root)
            for cmd, k in Counter(census.extract_all(t)).items():
                cmds.append({"owner": o, "repo": r, "name": f["name"], "toolchain": tc_label, "cmd": cmd,
                             "tool": cmd.split(":")[0], "n": k})
            texts[f"{o}/{r}/{f['path']}"] = t
        # A4: both files at repo root (per the tree, symlink-aware)
        both_root = {f["name"] for f in files if f["root"]} >= {"CLAUDE.md", "AGENTS.md"}
        if both_root:
            sym = any(f.get("symlink") for f in files if f["root"])
            ta = next((text_of.get(f["path"]) for f in usable if f["path"] == "CLAUDE.md"), None) or ""
            tb = next((text_of.get(f["path"]) for f in usable if f["path"] == "AGENTS.md"), None) or ""
            a_b = any(i["from"] == "CLAUDE.md" and i["to"] == "AGENTS.md" for i in imports)
            b_a = any(i["from"] == "AGENTS.md" and i["to"] == "CLAUDE.md" for i in imports)
            if sym or (ta and tb):
                a4.append({"owner": o, "repo": r, **core.drift(ta, tb, a_b, b_a, sym)})
        cl = next((f for f in files if f["root"] and f["name"] == "CLAUDE.md"), None)  # POST1-PLAN section 3.3: stacks and bridges
        ag = next((f for f in files if f["root"] and f["name"] == "AGENTS.md"), None)
        bk = extras.bridge_kind(bool(cl.get("symlink")), cl.get("symlink_target"), None if cl.get("symlink") else _blob(cl["blob_sha"])) if cl else None
        members = extras.stack_members(tree)
        stacks.append({"owner": o, "repo": r, "members": "|".join(members), "stack_size": len(members),
                       "bridge_kind": bk or "", "bridge": bool(bk), "both_root_files": bool(cl and ag),
                       "agents_symlink_to_claude": bool(ag and extras.bridge_kind(bool(ag.get("symlink")), ag.get("symlink_target"), None, "CLAUDE.md"))})
        d = set(rec.get("dirs", [])); fp = set(rec.get("files_present", []))
        a7.append({"owner": o, "repo": r, "skills": ".claude/skills" in d, "commands": ".claude/commands" in d,
                   "agents": ".claude/agents" in d, "settings_json": ".claude/settings.json" in fp,
                   "cursor_rules": ".cursor/rules" in d or ".cursorrules" in fp,
                   "copilot_instructions": ".github/copilot-instructions.md" in fp, "mcp_json": ".mcp.json" in fp})
        # inclusion probability counts EVERY agent file of the repo in its own size bucket (root and nested), not only the root file
        extra = max(0, rec.get("nested_total", 0) - sum(1 for f in files if not f["root"]))
        incl = strata.repo_incl_prob(files, pname, lookup, extra, lo, hi)
        repos.append({"owner": o, "repo": r, "tree_files": len(tree), "tree_truncated": rec.get("tree_truncated", False),
                      "has_claude": any(f["name"] == "CLAUDE.md" for f in files if f["root"]),
                      "has_agents": any(f["name"] == "AGENTS.md" for f in files if f["root"]),
                      "incl_prob_repo": incl, "nested_only": nested_only, "toolchain": census.toolchain(names_root),
                      "dev": (o, r) in dev_keys})
    # A5: exact + near duplicates
    a5 = []
    if texts:
        from datasketch import MinHashLSH
        perm = cfg["dedup"]["minhash_perm"]
        keys = sorted(texts)
        mh = {k: _minhash(texts[k], perm) for k in keys}
        lsh = MinHashLSH(threshold=cfg["dedup"]["near_dup_jaccard"], num_perm=perm)
        for k in keys:
            lsh.insert(k, mh[k])
        parent = {k: k for k in keys}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for k in keys:
            for m in lsh.query(mh[k]):
                parent[find(m)] = find(k)
        groups: dict[str, list[str]] = {}
        for k in keys:
            groups.setdefault(find(k), []).append(k)
        exact: dict[str, int] = {}
        for k in keys:
            h = hashlib.sha256(texts[k].encode()).hexdigest()
            exact[h] = exact.get(h, 0) + 1
        for gi, (g, members) in enumerate(sorted(groups.items())):
            for k in members:
                h = hashlib.sha256(texts[k].encode()).hexdigest()
                o, r = k.split("/")[:2]
                a5.append({"owner": o, "repo": r, "path": "/".join(k.split("/")[2:]), "cluster": gi,
                           "cluster_size": len(members), "exact_dup_size": exact[h]})
    sk, sk_cmds, sk_tools = _skills(keep)
    tables = {"a8": a8, "stacks": stacks, "commands": cmds, "skills": sk, "skill_commands": sk_cmds, "skill_tools": sk_tools, "a1_files": a1f, "a1_repo": a1r, "a2_repo": a2r, "a2_detail": a2d, "a3": a3, "a4": a4, "a5": a5, "a7": a7, "repos": repos}
    for name, rows in tables.items():
        if rows:
            pl.DataFrame(rows).write_parquet(out_dir / f"{name}.parquet")
    return summary(tag)


def summary(tag: str | None, include_dev: bool = False) -> list[str]:
    d = ROOT / "data/features" / (f"c{tag}" if tag else "")
    out = []
    def rd(n):
        p = d / f"{n}.parquet"
        return devset.nondev(pl.read_parquet(p), include_dev) if p.exists() else None
    allr = pl.read_parquet(d / "repos.parquet") if (d / "repos.parquet").exists() else None
    if allr is not None and not include_dev:
        out.append(f"REPORT MODE: excluding {allr.height - devset.nondev(allr).height:,} dev (c5000) repos; use --include-dev for tuning only")
    rp, a1, a2, a3, a4, a5, a7 = (rd(n) for n in ("repos", "a1_repo", "a2_repo", "a3", "a4", "a5", "a7"))
    if rp is None or rp.is_empty():
        return out + ["no non-dev repos with features yet (the c5000 checkpoint is the dev set)"]
    out.append(f"repos with features: {rp.height:,}; root-file rows: {a1.height:,} (CLAUDE {int((a1['name'] == 'CLAUDE.md').sum())}, AGENTS {int((a1['name'] == 'AGENTS.md').sum())})")
    def cov(df, cols, label):
        if df is None:
            return
        out.append(f"coverage {label}: " + ", ".join(f"{c}={df[c].drop_nulls().len() / df.height:.0%}" for c in cols))
    cov(a1, ["tokens", "always_loaded_tokens", "on_demand_tokens", "structure"], "A1")
    cov(a3, ["shout_per_1k", "negation_share", "imperative_share"], "A3")
    if a2 is not None:
        out.append(f"A2: files w/ >=1 path ref {int((a2['paths_checked'] > 0).sum())}/{a2.height}, w/ >=1 cmd {int((a2['cmds_checked'] > 0).sum())}/{a2.height}; "
                   f"truncated-tree repos {int(a2.filter(pl.col('tree_truncated')).select(['owner', 'repo']).unique().height)} -> UNKNOWN (truncated, ambiguous, cd or symlink; excluded): "
                   f"{int(a2['paths_unknown'].sum())} paths, {int(a2['cmds_unknown'].sum())} cmds; "
                   f"dead paths {int(a2['paths_dead'].sum())}/{int(a2['paths_checked'].sum())} (moved-tier {int(a2['paths_dead_moved'].sum())}), dead cmds {int(a2['cmds_dead'].sum())}/{int(a2['cmds_checked'].sum())}, "
                   f"js mismatch {int(a2['js_mismatch'].sum())}, py mismatch {int(a2['py_mismatch'].sum())}")
    if a4 is not None:
        out.append("A4 pairs: " + ", ".join(f"{k}={n}" for k, n in a4.group_by("kind").agg(pl.len()).iter_rows()))
    else:
        out.append("A4 pairs: none yet (no repo with both files in this sample)")
    if a5 is not None:
        big = a5.filter(pl.col("cluster_size") >= config.load()["dedup"]["cluster_min_size"]).height
        out.append(f"A5: files {a5.height:,}, in clusters >=5: {big / a5.height:.1%}, exact dups (size>1): {(a5['exact_dup_size'] > 1).mean():.1%}")
    if a7 is not None:
        out.append("A7: " + ", ".join(f"{c}={a7[c].mean():.0%}" for c in a7.columns if c not in ("owner", "repo")))
    return out[:50]


# ---------- summary tables: commands census and skills ----------

def table(tag: str | None, which: str, include_dev: bool = False) -> list[str]:
    d = ROOT / "data/features" / (f"c{tag}" if tag else "")
    def rd(n):
        p = d / f"{n}.parquet"
        return devset.nondev(pl.read_parquet(p), include_dev) if p.exists() else None
    rp = rd("repos")
    if rp is None or rp.is_empty():
        return ["no non-dev repos with features yet (the c5000 checkpoint is the dev set; --include-dev for tuning only)"]
    out = [] if include_dev else ["REPORT MODE: dev (c5000) repos excluded"]
    if which == "commands":
        cm = rd("commands")
        if cm is None or cm.is_empty():
            return ["no commands found"]
        per = cm.group_by(["owner", "repo", "cmd", "toolchain"]).agg(pl.col("n").sum().alias("n"))
        with_cmd = per.select("owner", "repo").unique().height
        out.append(f"commands census: {with_cmd:,}/{rp.height:,} repos mention >=1 command; {per['cmd'].n_unique():,} distinct normalized commands")
        top = per.group_by("cmd").agg(pl.len().alias("repos"), pl.col("n").sum().alias("m")).sort(["repos", "m"], descending=True).head(15)
        out.append("overall (share of all repos with files):")
        out += [f"  {c:<24}{r:>5} repos {r / rp.height:>4.0%}  {m} mentions" for c, r, m in top.iter_rows()]
        tcs = rp.group_by("toolchain").agg(pl.len().alias("n")).filter(pl.col("n") >= 10).sort("n", descending=True).head(5)
        for tc, n in tcs.iter_rows():
            sub = per.filter(pl.col("toolchain") == tc).group_by("cmd").agg(pl.len().alias("repos")).sort("repos", descending=True).head(5)
            out.append(f"toolchain {tc} (n={n} repos): " + ", ".join(f"{c} {r / n:.0%}" for c, r in sub.iter_rows()))
    elif which == "skills":
        sk = rd("skills")
        n_with = None
        if sk is None or sk.is_empty():
            return ["no skills fetched yet (run `neri fetch --skills --checkpoint N` then `neri features`)"]
        per = sk.group_by(["owner", "repo"]).agg(pl.len().alias("n"))
        out.append(f"skills: {per.height:,}/{rp.height:,} repos have fetched SKILL.md files ({per.height / rp.height:.0%}); {sk.height:,} skills; "
                   f"per repo median {per['n'].median():.0f}, max {per['n'].max()}")
        out.append(f"SKILL.md tokens median {sk['tokens'].median():.0f} (p90 {sk['tokens'].quantile(0.9):.0f}); frontmatter {sk['has_frontmatter'].mean():.0%}; "
                   f"via symlinked dir {sk['linked'].mean():.0%}")
        out.append(f"scripts: skills with a scripts/ dir {(sk['n_script_files'] > 0).mean():.0%}; skills referencing >=1 script {(sk['n_script_refs'] > 0).mean():.0%}; "
                   f"median refs {sk['n_script_refs'].median():.0f}, max {sk['n_script_refs'].max()}")
        sc, st = rd("skill_commands"), rd("skill_tools")
        if sc is not None and not sc.is_empty():
            top = sc.group_by("cmd").agg(pl.struct(["owner", "repo", "skill"]).n_unique().alias("skills")).sort("skills", descending=True).head(12)
            out.append("top commands in skills (by skills mentioning): " + ", ".join(f"{c} {n}" for c, n in top.iter_rows()))
        if st is not None and not st.is_empty():
            for src in ("allowed-tools", "command"):
                top = st.filter(pl.col("source") == src).group_by("tool").agg(pl.struct(["owner", "repo", "skill"]).n_unique().alias("skills")).sort("skills", descending=True).head(10)
                out.append(f"top tools ({src}): " + ", ".join(f"{c} {n}" for c, n in top.iter_rows()))
    return out[:50]


# Single source of truth for the explorer's "how computed" notes (keep in sync with the code above and core.py).
COLUMN_DOCS = {
    "repo": "owner/repo from the explore sample (sha256(owner/repo) % 2 == 0), pinned at the default-branch sha recorded at metadata time.",
    "stars": "stargazerCount from GraphQL at metadata time (M2).",
    "stratum": "established = stars >= 10 (stratified sample); long tail = stars 0-9, not fork/archived, pushed within 180 days (simple random sample). Both are weighted back to their population sizes N_h in reports.",
    "toolchain": "census.toolchain(): primary toolchain from root files, priority pnpm/yarn/bun/npm lockfiles, package.json, uv.lock, poetry.lock, Cargo.toml, go.mod, gradle, pom.xml, other python files, Makefile-only; several JS lockfiles = mixed-js.",
    "tokens": "tiktoken o200k_base token count of the root file (proxy tokenizer, not Anthropic's).",
    "always_loaded_tokens": "root file tokens + recursively resolved @path imports (max depth 5, max 30 imports). Imports inside code spans/blocks are ignored.",
    "on_demand_tokens": "tokens of nested CLAUDE.md/AGENTS.md files in subfolders (at most 40 per repo).",
    "structure": "single = no imports/nested; imports = has resolved @imports; nested = has nested agent files; both = imports and nested.",
    "lines": "line count of the file.",
    "headings": "markdown headings (# ..) outside code fences.",
    "code_blocks": "number of fenced code blocks (``` or ~~~).",
    "links": "markdown links [text](target).",
    "n_imports": "number of resolved @imports reachable from the root file.",
    "paths_dead": "references (backticks, links, @imports, slash paths in code blocks) to paths missing from the git tree at the pinned sha. Excluded: placeholders, globs, URLs, absolute paths, generated/ignored paths, creation/example/negated lines, bare filenames, other tools' dot-dirs. Symlinked dirs and submodules count as present.",
    "unknown": "refs in repos whose git tree was TRUNCATED by GitHub: the partial listing cannot prove absence, so a ref is alive if a non-recursive directory walk found it, otherwise UNKNOWN and excluded from every wrongness number (count shown, never counted as dead).",
    "tier": "M5 tier (POST-HOC, unvalidated): 'moved' = the missing target exists under another dir, extension or letter case; 'missing' = no similar file found. Precision is not established (see M5).",
    "cmds_dead": "npm/pnpm/yarn/bun run X, make X, just X, uv/poetry run X where X is not defined in package.json scripts / Makefile targets (incl. pattern rules) / justfile recipes / pyproject scripts. Ambiguous cases (monorepo, includes, modules) are skipped.",
    "skills": "number of .claude/skills/*/SKILL.md files fetched from raw at the pinned sha (symlinked skill dirs resolved through the link).",
    "shout_per_1k": "(count of IMPORTANT/CRITICAL/MUST/NEVER/ALWAYS words + ALL-CAPS lines) per 1,000 tokens, outside code fences.",
    "negation_share": "share of list-item rules containing never/don't/do not/avoid/must not/should not.",
    "imperative_share": "share of list items whose first word is in a curated imperative-verb list.",
    "a7": "presence of .claude/skills, .claude/commands, .claude/agents, .claude/settings.json, .cursor/rules (or .cursorrules), .github/copilot-instructions.md, .mcp.json in the git tree.",
}
