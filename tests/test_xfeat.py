from neri import xfeat


def test_smells_and_deontic():
    t = "# T\n- Always use pnpm for installs\n- Never use pnpm for installs\n- Follow best practices when appropriate\n- TODO fill in\n- You may run tests\n"
    f = xfeat.text_features(t)
    assert f["smell_vague"] and f["smell_template"] and f["smell_contra"]
    assert f["deont_prohib"] >= 1 and f["deont_permit"] >= 1


def test_settings_parse():
    s = xfeat.parse_settings(['{"permissions":{"allow":["Bash(*)","WebFetch"],"defaultMode":"bypassPermissions"},"hooks":{"PostToolUse":[{"hooks":[{"command":"npx prettier --write ."}]}]}}'])
    assert s["s_broad_bash"] and s["s_network"] and s["s_bypass"] and s["s_hook_format_lint"]
    assert xfeat.parse_settings(["not json"]) is None


def test_thirds_positions():
    t = "\n".join(["line"] * 4 + ["NEVER do x"] + ["line"] * 4)
    f = xfeat.text_features(t)
    assert f["emph_mid"] == 1 and f["emph_first"] == 0


def test_classify_pair():
    c = xfeat.classify_pair
    assert c(None, None, c_sym=True)[0] == "symlink"
    assert c("@AGENTS.md\n# extra\nmore", "# a\nrules " * 3)[0] == "import_bridge"
    assert c("# x\nuse tabs", "# x\nuse tabs")[0] == "near_copy"
    long_a = "\n".join(f"rule {i}" for i in range(80))
    assert c("# Claude\nRead AGENTS.md first for everything.\n" + "\n".join(f"c {i}" for i in range(60)), long_a)[0] == "prose_pointer"
    assert c("# a\nSee docs/AGENTS.md for more", long_a)[0] == "independent"
    assert c("\n".join(f"alpha {i}" for i in range(40)), long_a)[0] == "independent"
