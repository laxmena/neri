"""Independent second labeler via OpenRouter (non-Anthropic model). Blind: only row content + label definitions are sent; no detector output, no
other labels. Temperature 0, JSON output, hard budget cap. The API key is read from .env and never printed or logged.
Sent data: first lines of public GitHub agent files (the same rows shown in reports/link_check.md and reports/agentsmd_check.md)."""
from __future__ import annotations

import json
import re
import time
import urllib.request

from . import config

ROOT = config.ROOT
MODEL = "google/gemini-4-hydrogen"
CAP_USD = 10.0
LINK_DEFS = """You label how two instruction files of one GitHub repository (CLAUDE.md and AGENTS.md, both in the repo root) relate. Labels:
- symlink: one file is a git symlink to the other (the row says "[git symlink to ...]").
- import_bridge: one file imports the other with an @-import such as a line "@AGENTS.md" or "@CLAUDE.md", with or without extra content.
- prose_pointer: one file tells the reader to read the other file ("see AGENTS.md", "read CLAUDE.md first", "instructions live in AGENTS.md"), without an @-import; either direction.
- near_copy: the two files contain mostly the same lines.
- independent: both files carry substantial, different instructions and neither points to the other.
- unclear: you cannot decide from the lines shown.
If several apply, choose the first in this order: symlink, import_bridge, near_copy, prose_pointer, independent. Only the first 15 non-empty lines of each file are shown; line counts are the full file.
Answer with JSON only: {"label": "<one label>", "reason": "<one short sentence>"}"""
AG_DEFS = """You label one file from a GitHub repository (named CLAUDE.md or AGENTS.md). Labels:
- instruction_file: it tells a coding agent how to work in this repository (commands, conventions, rules, project context for the agent).
- agent_catalog_or_other: it documents a project's own agents (for example a catalog or description of the agents a framework ships) or is unrelated content.
- unclear: you cannot decide from the lines shown.
Only the first 20 non-empty lines are shown; the line count is the full file.
Answer with JSON only: {"label": "<one label>", "reason": "<one short sentence>"}"""


def _key() -> str:
    for line in open(ROOT / ".env"):
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("OPENROUTER_API_KEY missing in .env")


def _call(key: str, system: str, user: str) -> tuple[dict, float]:
    body = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": 1200, "usage": {"include": True},
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    for attempt in range(5):
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=120))
            txt = r["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{.*\}", txt, re.S)
            return (json.loads(m.group(0)) if m else {"label": "unclear", "reason": "unparseable"}), float((r.get("usage") or {}).get("cost") or 0.0)
        except Exception:
            time.sleep(5 * (attempt + 1))
    return {"label": "error", "reason": "request failed"}, 0.0


def run(log=print) -> dict:
    key, spent, meta = _key(), 0.0, {"model": MODEL, "date": time.strftime("%Y-%m-%d"), "cap_usd": CAP_USD}
    LV = {"symlink", "import_bridge", "prose_pointer", "near_copy", "independent", "unclear"}
    for name, rows_f, defs, valid in (("link", "link_rows.json", LINK_DEFS, LV), ("agentsmd", "agentsmd_rows.json", AG_DEFS, {"instruction_file", "agent_catalog_or_other", "unclear"}),
                                      ("link_v2", "link_rows_v2.json", LINK_DEFS, LV)):
        out_f = ROOT / f"data/log/or_labels_{name}.json"
        done = json.load(open(out_f)) if out_f.exists() else {}
        for r in json.load(open(ROOT / "data/log" / rows_f)):
            if r["id"] in done:
                continue
            if spent >= CAP_USD:
                raise SystemExit("budget cap reached")
            if name.startswith("link"):
                user = (f"CLAUDE.md ({r['claude_lines']} non-empty lines):\n" + "\n".join(r["claude_head"]) +
                        f"\n\nAGENTS.md ({r['agents_lines']} non-empty lines):\n" + "\n".join(r["agents_head"]))
            else:
                user = f"File name: {r['file_name']} ({r['lines']} non-empty lines)\n\n" + "\n".join(r["head"])
            res, cost = _call(key, defs, user)
            spent += cost
            if res.get("label") not in valid:
                res = {"label": "unclear", "reason": "invalid label: " + str(res.get("label"))[:40]}
            done[r["id"]] = res
            json.dump(done, open(out_f, "w"))
        log(f"{name}: {len(done)} labeled; spent so far ${spent:.4f}")
    prev = json.load(open(ROOT / "data/log/or_meta.json")) if (ROOT / "data/log/or_meta.json").exists() else {}
    meta["spent_usd"] = round(spent + (prev.get("spent_usd", 0.0) if prev.get("model") == MODEL else 0.0), 4)
    json.dump(meta, open(ROOT / "data/log/or_meta.json", "w"))
    return meta


def _kappa(a: list[str], b: list[str]) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    cats = set(a) | set(b)
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def compare(log=print) -> list[str]:
    import random
    meta = json.load(open(ROOT / "data/log/or_meta.json"))
    L = ["# Labels compare (two independent labelers)", "",
         f"Labeler A = Claude (me), labeled the rows independently from the row text before seeing any other label. Labeler B = {meta['model']} via OpenRouter, run {meta['date']}, temperature 0, JSON output, blind (row content + label definitions only), cost ${meta['spent_usd']}.", ""]
    adj = ["# Human adjudication (labels hidden)", "",
           "Final label = your label where present, else the agreed label. Rows: every disagreement plus 10 random agreed rows per sheet is NOT used; 10 random agreed rows in total are mixed in. Row content only; the two labelers' labels are in reports/labels_compare.md.", ""]
    rng = random.Random(config.load()["snapshot"]["seed"] + 11)
    summ = {}
    for name, rows_f, labs, det_f, sheet in (("link", "link_rows.json", "link_labels_claude.json", "link_detector.json", "link"), ("agentsmd", "agentsmd_rows.json", "agentsmd_labels_claude.json", None, "agentsmd"), ("link_v2", "link_rows_v2.json", "link_labels_claude_v2.json", "link_detector_v2.json", "link_v2")):
        rows = json.load(open(ROOT / "data/log" / rows_f))
        mine = json.load(open(ROOT / "data/log" / labs))
        orl = {k: v["label"] for k, v in json.load(open(ROOT / f"data/log/or_labels_{name}.json")).items()}
        det = json.load(open(ROOT / "data/log" / det_f)) if det_f else {}
        ids = [r["id"] for r in rows]
        a, b = [mine[i] for i in ids], [orl[i] for i in ids]
        agree = sum(x == y for x, y in zip(a, b))
        k = _kappa(a, b)
        summ[name] = (ids, a, b, agree, k)
        L += [f"## {name}: agreement {agree}/{len(ids)} = {agree / len(ids):.0%}; Cohen's kappa = {k:.2f}", ""]
        if name == "agentsmd":
            L += ["Kappa is unstable here: nearly all rows share one label, so chance agreement is high. Read the raw disagreements.", ""]
        if det:
            dm = sum(det[i] == m for i, m in zip(ids, a))
            db = sum(det[i] == m for i, m in zip(ids, b))
            L += [f"Detector class vs my label: {dm}/{len(ids)}; vs {meta['model'].split('/')[1]}: {db}/{len(ids)} (the detector was not shown to either labeler).", ""]
        L += ["| id | mine | other model | detector |", "|---|---|---|---|"]
        for i, x, y in zip(ids, a, b):
            L.append(f"| {i} | {x} | {y}{'' if x == y else ' **(differs)**'} | {det.get(i, '')} |")
        L += ["", "Disagreements: " + (", ".join(i for i, x, y in zip(ids, a, b) if x != y) or "none"), ""]
    (ROOT / "reports/labels_compare.md").write_text("\n".join(L))
    # adjudication sheet
    for name, rows_f in (("link", "link_rows.json"), ("agentsmd", "agentsmd_rows.json"), ("link_v2", "link_rows_v2.json")):
        rows = {r["id"]: r for r in json.load(open(ROOT / "data/log" / rows_f))}
        summ[name] = summ[name] + (rows,)
    dis, agr = [], []
    for name, (ids, a, b, ag, k, rows) in summ.items():
        for i, x, y in zip(ids, a, b):
            (dis if x != y else agr).append((name, rows[i], x))
    rng.shuffle(agr)
    items = dis + agr[:10]
    adj += ["Label sets: link rows = symlink / import_bridge / prose_pointer / near_copy / independent / unclear. AGENTS/CLAUDE rows = instruction_file / agent_catalog_or_other / unclear.", ""]
    for name, r, _ in items:
        adj.append(f"## {r['id']} ({'link pair' if name == 'link' else r['file_name'] + ' file'})")
        if name.startswith("link"):
            adj += [f"CLAUDE.md ({r['claude_lines']} non-empty lines):", "```"] + r["claude_head"] + ["```", f"AGENTS.md ({r['agents_lines']} non-empty lines):", "```"] + r["agents_head"] + ["```", "", "Your label: ", ""]
        else:
            adj += [f"{r['file_name']} ({r['lines']} non-empty lines):", "```"] + r["head"] + ["```", "", "Your label: ", ""]
    (ROOT / "reports/human_adjudicate.md").write_text("\n".join(adj))
    return [f"{n}: {s[3]}/{len(s[0])} agree, kappa {s[4]:.2f}" for n, s in summ.items()] + [f"adjudication rows: {len(dis)} disagreements + {min(10, len(agr))} agreed"]
