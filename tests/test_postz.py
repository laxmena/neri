from neri.postz import SIG_RE, classify_hook, copresence, hook_commands
import json


def test_signature_regex():
    assert SIG_RE.search("This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.")
    assert not SIG_RE.search("Guidance for Claude Code and other agents")


def test_hook_classes():
    assert classify_hook("PostToolUse", "prettier --write $FILE") == "format"
    assert classify_hook("PostToolUse", "npx eslint --fix") == "lint"
    assert classify_hook("PostToolUse", "tsc --noEmit") == "typecheck"
    assert classify_hook("Stop", "pytest -x") == "test"
    assert classify_hook("PreToolUse", "python guard.py || exit 2") == "guard_block"
    assert classify_hook("Notification", "osascript -e 'display notification'") == "notify"
    assert classify_hook("SessionStart", "cat docs/context.md") == "context_injection"
    assert classify_hook("Stop", "git add -A") == "git"
    assert classify_hook("PostToolUse", "echo $X >> audit.log") == "log_audit"
    assert classify_hook("Stop", "./run.sh") == "other"


def test_hook_commands_parse():
    j = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x.sh"}, {"type": "prompt", "prompt": "p"}]}]}})
    assert hook_commands(j) == [("PreToolUse", "Bash", "x.sh")]
    assert hook_commands("not json") == []


def test_copresence():
    c = copresence([".cursor/rules/a.mdc", "sub/GEMINI.md", ".claude/settings.json", "src/x.py"])
    assert c["cursor"] and c["gemini"] and c["claude_dir"]
    assert not c["copilot"] and not c["codex"]
