"""Pure per-file / per-repo deterministic features for post 1 (D1-D6, X1-X3). No network, no LLM. Keyword lists are documented in DECISIONS.md."""
from __future__ import annotations

import json
import re

from .core import iter_lines

# ---------- D6 security ----------
SEC_RE = re.compile(r"\b(secur(?:e|ity)|secrets?|credentials?|passwords?|api[ _-]?keys?|auth(?:entication|orization|orize|enticate)?|oauth|vulnerabilit(?:y|ies)|encrypt\w*|sanitiz\w*|sql injection|xss|csrf|private key)\b", re.I)
# ---------- D5 tone ----------
PERSONA_RE = re.compile(r"^\W*(?:#+\s*)?(you are|you're|act as|your role is|as an?\s+(?:expert|senior|experienced)\b)", re.I)
PLEASE_RE = re.compile(r"\bplease\b", re.I)
THANKS_RE = re.compile(r"\b(thank you|thanks)\b", re.I)
VENDOR_RE = re.compile(r"\b(claude(?:\s+code)?|opus|sonnet|haiku|gpt-?\d\S*|chatgpt|codex|gemini|copilot|cursor|windsurf|cline|aider|openai|anthropic)\b", re.I)
CLAUDE_RE = re.compile(r"\b(claude(?:\s+code)?|opus|sonnet|haiku|anthropic)\b", re.I)
FILENAMES = re.compile(r"\b(CLAUDE|AGENTS|GEMINI)\.md\b|\.claude\b|\.cursor\b|\.github/copilot\S*", re.I)
# ---------- X1/X3 rules ----------
SHOUT_RE = re.compile(r"\b(IMPORTANT|CRITICAL|MUST|NEVER|ALWAYS|MANDATORY|REQUIRED|FORBIDDEN)\b")
DONT_RE = re.compile(r"\b(never|don'?t|do not|must not|should not|shouldn'?t|avoid|forbidden)\b", re.I)
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
PROHIB = re.compile(r"\b(never|do not|don'?t|must not|should not|shouldn'?t|avoid|prohibited|forbidden|not allowed|cannot|can'?t|no longer)\b", re.I)
OBLIG = re.compile(r"\b(must|always|should|shall|required|requires?|need(?:s)? to|have to|ensure|make sure|be sure to)\b", re.I)
PERMIT = re.compile(r"\b(may|can|could|feel free|optional(?:ly)?|allowed|ok to|okay to|is fine|permitted)\b", re.I)
# ---------- X2 smells ----------
VAGUE_RE = re.compile(r"\b(clean code|best practices?|be careful|high[- ]quality|as needed|as appropriate|when appropriate|where appropriate|if appropriate|good practices|industry standards?|production[- ]ready|maintainable code|readable code|write good code)\b", re.I)
TEMPLATE_RE = re.compile(r"(\bTODO\b|\bFIXME\b|\bTBD\b|<\s*your[ _-][^>]{0,40}>|\[\s*your[ _-][^\]]{0,40}\]|\[\s*insert[^\]]{0,40}\]|\{\{[^}]{1,40}\}\}|\bXXX\b|lorem ipsum|\[\s*project[ _-]?name\s*\]|<\s*project[ _-]?name\s*>|\[\s*describe[^\]]{0,40}\])", re.I)
ALWAYS_RE = re.compile(r"\b(?:always|must)\s+(?:use|run|prefer|include|keep|follow|write)\s+([a-z0-9_.\-/` ]{3,40})", re.I)
NEVER_RE = re.compile(r"\b(?:never|do not|don'?t|must not|should not)\s+(?:use|run|prefer|include|keep|follow|write)\s+([a-z0-9_.\-/` ]{3,40})", re.I)


def _prose(text: str) -> str:
    out = []
    for line, fence in iter_lines(text):
        if not fence:
            out.append(line)
    t = "\n".join(out)
    t = re.sub(r"`[^`\n]*`", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[#*>_|\[\]()~=-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def prose_for_lang(text: str, n: int = 1500) -> str | None:
    t = _prose(text)
    return t[:n] if sum(c.isalpha() for c in t) >= 40 else None


def _key(s: str) -> str:
    w = re.findall(r"[a-z0-9_.\-/]+", s.lower())
    return " ".join(w[:2])


def text_features(text: str) -> dict:
    lines_nb = [(l, f) for l, f in iter_lines(text) if l.strip()]
    nofence = [l for l, f in lines_nb if not f]
    body = "\n".join(nofence)
    scrub = FILENAMES.sub(" ", text)
    out: dict = {}
    # D6
    out["mentions_security"] = bool(SEC_RE.search(body))
    # D5
    out["persona"] = any(PERSONA_RE.search(l) for l in nofence)
    out["says_please"] = bool(PLEASE_RE.search(body))
    out["says_thanks"] = bool(THANKS_RE.search(body))
    out["names_vendor"] = bool(VENDOR_RE.search(scrub))
    out["names_claude"] = bool(CLAUDE_RE.search(scrub))
    # X1: position of emphasized / negated lines by third (non-empty lines)
    # ruler = prose lines only (outside fences); fenced code no longer shifts the thirds (Codex #7)
    prose_lines = [l for l, f in lines_nb if not f]
    n = len(prose_lines)
    emph, neg = [0, 0, 0], [0, 0, 0]
    for i, l in enumerate(prose_lines):
        t = min(2, int(3 * i / n))
        if SHOUT_RE.search(l) or (len(l.split()) >= 3 and l.strip().isupper()):
            emph[t] += 1
        if DONT_RE.search(l):
            neg[t] += 1
    out.update({"n_nonempty_lines": n, "emph_first": emph[0], "emph_mid": emph[1], "emph_last": emph[2],
                "neg_first": neg[0], "neg_mid": neg[1], "neg_last": neg[2]})
    # X3 deontic over list items (outside fences)
    cls = {"prohib": 0, "oblig": 0, "permit": 0, "descr": 0}
    items = []
    for l in nofence:
        m = BULLET_RE.match(l)
        if m:
            items.append(m.group(1).strip())
    for it in items:
        if PROHIB.search(it):
            cls["prohib"] += 1
        elif OBLIG.search(it):
            cls["oblig"] += 1
        elif PERMIT.search(it):
            cls["permit"] += 1
        else:
            cls["descr"] += 1
    out.update({"n_items": len(items), **{f"deont_{k}": v for k, v in cls.items()}})
    # X2 smells
    vag = bool(VAGUE_RE.search(body))
    norm = [re.sub(r"\W+", " ", it.lower()).strip() for it in items if len(it.split()) >= 4]
    dup = len(norm) != len(set(norm))
    al = {_key(m.group(1)) for m in ALWAYS_RE.finditer(body)}
    ne = {_key(m.group(1)) for m in NEVER_RE.finditer(body)}
    contra = bool({k for k in al & ne if len(k) >= 3})
    tmpl = bool(TEMPLATE_RE.search(body))
    out.update({"smell_vague": vag, "smell_dup": dup, "smell_contra": contra, "smell_template": tmpl, "smell_any": vag or dup or contra or tmpl})
    return out


def smell_examples(text: str) -> list[tuple[str, str]]:
    """(smell, short matched line) pairs for the human check; first hit per smell."""
    res, lines = [], text.splitlines()
    for kind, rx in (("vague", VAGUE_RE), ("template", TEMPLATE_RE)):
        for l in lines:
            if rx.search(l):
                res.append((kind, l.strip()[:160])); break
    seen = {}
    for l in lines:
        m = BULLET_RE.match(l)
        if m and len(m.group(1).split()) >= 4:
            k = re.sub(r"\W+", " ", m.group(1).lower()).strip()
            if k in seen:
                res.append(("dup", l.strip()[:160])); break
            seen[k] = 1
    al = {_key(m.group(1)): m.group(0) for m in ALWAYS_RE.finditer(text)}
    for m in NEVER_RE.finditer(text):
        k = _key(m.group(1))
        if k in al and len(k) >= 3:
            res.append(("contra", f"{al[k][:70]} || {m.group(0)[:70]}")); break
    return res


# ---------- D1 settings ----------
HOOK_PURPOSE = (("format_lint", re.compile(r"prettier|eslint|ruff|black\b|gofmt|rustfmt|\bfmt\b|format|lint|biome|isort|stylelint", re.I)),
                ("test", re.compile(r"pytest|jest|vitest|\btest|mocha|cargo test|go test|rspec", re.I)),
                ("guard", re.compile(r"block|guard|deny|exit 2|validate|prevent|protect|secret|forbid|reject|\bcheck", re.I)),
                ("notify", re.compile(r"notif|osascript|afplay|\bsay\b|terminal-notifier|slack|discord|ntfy|beep|display notification", re.I)))
BROAD_BASH = {"Bash", "Bash(*)", "Bash(*:*)", "Bash(**)"}


def parse_settings(blobs: list[str]) -> dict | None:
    """Merge committed settings JSONs of one repo into aggregate flags (None when nothing parses)."""
    allow, deny, modes, cmds = [], [], [], []
    ok = False
    for b in blobs:
        try:
            j = json.loads(b)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        ok = True
        p = j.get("permissions") if isinstance(j.get("permissions"), dict) else {}
        allow += [a for a in (p.get("allow") or []) if isinstance(a, str)]
        deny += [a for a in (p.get("deny") or []) if isinstance(a, str)]
        if isinstance(p.get("defaultMode"), str):
            modes.append(p["defaultMode"])
        if j.get("enableAllProjectMcpServers") is True:
            modes.append("enableAllProjectMcpServers")
        h = j.get("hooks")
        if isinstance(h, dict):
            for ev, arr in h.items():
                for ent in (arr if isinstance(arr, list) else []):
                    for hk in (ent.get("hooks", []) if isinstance(ent, dict) else []):
                        if isinstance(hk, dict) and isinstance(hk.get("command"), str):
                            cmds.append(hk["command"])
    if not ok:
        return None
    net = any(a.startswith(("WebFetch", "WebSearch")) or re.match(r"Bash\((curl|wget|ssh|nc|scp)\b", a) for a in allow)
    purposes = set()
    for c in cmds:
        hit = [n for n, rx in HOOK_PURPOSE if rx.search(c)]
        purposes.update(hit or ["other"])
        if not hit:
            purposes.add("other")
    return {"s_any_allow": bool(allow), "s_broad_bash": any(a in BROAD_BASH for a in allow),
            "s_network": net, "s_bypass": any(m == "bypassPermissions" for m in modes),
            "s_accept_edits": any(m == "acceptEdits" for m in modes), "s_all_mcp": "enableAllProjectMcpServers" in modes,
            "s_has_deny": bool(deny), "s_hooks": bool(cmds), **{f"s_hook_{n}": (n in purposes) for n, _ in HOOK_PURPOSE}, "s_hook_other": "other" in purposes}


# ---------- pair classification (A): root CLAUDE.md + AGENTS.md of one repo ----------
IMPORT_OF = {"AGENTS.md": re.compile(r"(?<![\w/@.])@(?:\./)?AGENTS\.md\b"), "CLAUDE.md": re.compile(r"(?<![\w/@.])@(?:\./)?CLAUDE\.md\b")}
POINTER_LINES = 15
POINTER_MAX_LINES, NEAR_COPY = 15, 0.8
POINTER_VERB = re.compile(r"\b(read|see|refer|follow|consult|defer|source of truth|canonical|single source|lives? in|are in|is in|details in|go to|start with)\b", re.I)
LINK_CLASSES = ("symlink", "import_bridge", "prose_pointer", "near_copy", "independent")


def nonempty(text: str) -> list[str]:
    return [l.rstrip() for l in text.splitlines() if l.strip()]


def _norm_set(text: str) -> set[str]:
    return {n for n in (re.sub(r"\s+", " ", re.sub(r"[#*>`_\-+|]", " ", l.lower())).strip() for l in text.splitlines()) if n}


def line_jaccard(a: str, b: str) -> float | None:
    x, y = _norm_set(a), _norm_set(b)
    return len(x & y) / len(x | y) if (x | y) else None


def classify_pair(ctext: str | None, atext: str | None, c_sym: bool = False, a_sym: bool = False) -> tuple[str | None, str]:
    """Class of a CLAUDE.md + AGENTS.md pair and the direction ('claude->agents' = CLAUDE.md points to AGENTS.md). Order:
    symlink > import_bridge (one file @-imports the other, with or without extra lines) > near_copy (line-Jaccard >= 0.8)
    > prose_pointer (the other file's name, not as a path like docs/AGENTS.md, appears in the first 15 non-empty lines on a line with a pointer verb (read/see/refer/follow/source of truth/lives in...), at any length; or anywhere in the first 15 lines of a file with < 15 non-empty lines) > independent. v1 used < 30 lines or < 25% of the other and no verb; v2 changed it after the first 30-pair hand check."""
    if c_sym or a_sym:
        return "symlink", "claude->agents" if c_sym else "agents->claude"
    if ctext is None or atext is None:
        return None, ""
    nof = lambda t: "\n".join(l for l, f in iter_lines(t) if not f)
    if IMPORT_OF["AGENTS.md"].search(nof(ctext)):
        return "import_bridge", "claude->agents"
    if IMPORT_OF["CLAUDE.md"].search(nof(atext)):
        return "import_bridge", "agents->claude"
    j = line_jaccard(ctext, atext)
    if j is not None and j >= NEAR_COPY:
        return "near_copy", ""
    cl, al = nonempty(ctext), nonempty(atext)
    for text, lines, other_lines, other_name, d in ((ctext, cl, al, "AGENTS.md", "claude->agents"), (atext, al, cl, "CLAUDE.md", "agents->claude")):
        head_lines = lines[:POINTER_LINES]
        head = "\n".join(head_lines)
        short = len(lines) < POINTER_MAX_LINES
        named = re.search(r"(?<![\w/.-])" + re.escape(other_name), head, re.I)
        # v2 (after the first hand check): a pointer verb on the same line counts at any file length
        verb = any(re.search(r"(?<![\w/.-])" + re.escape(other_name), l, re.I) and POINTER_VERB.search(l) for l in head_lines)
        if verb or (short and named):
            return "prose_pointer", d
    return "independent", ""
