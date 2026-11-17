from neri import extras


TREE = "Intro\n\n```\nproject/\n├── src/\n│   └── main.py\n└── README.md\n```\nafter\n"


def test_box_drawing_tree_in_a_code_block_is_a_dump():
    sp = extras.tree_dump_spans(TREE)
    assert sp == [(2, 8)]
    assert "main.py" in extras.span_text(TREE, [(a, b) for a, b in sp])


def test_eight_consecutive_path_like_lines_are_a_dump_but_seven_are_not():
    seven = "\n".join(f"src/mod{i}.py  # part {i}" for i in range(7))
    eight = "\n".join(f"src/mod{i}.py  # part {i}" for i in range(8))
    assert extras.tree_dump_spans(seven) == [] and extras.tree_dump_spans(eight) == [(0, 8)]
    bullets = "\n".join(f"- docs/page{i}.md - page {i}" for i in range(9))
    assert extras.tree_dump_spans(bullets) == [(0, 9)]
    prose = "\n".join(f"We use the module number {i} for testing" for i in range(10))
    assert extras.tree_dump_spans(prose) == []


def test_one_box_character_line_or_a_normal_code_block_is_not_a_dump():
    assert extras.tree_dump_spans("```\n│ just one\nrun make\n```\n") == []
    assert extras.tree_dump_spans("```bash\nnpm test\nnpm run build\n```\n") == []


DOC = "# Project\n\n## Overview\nThis repo does X.\n\n### Details\nmore\n\n## Commands\nrun it\n\n## Architecture\nlayers\n"


def test_overview_sections_run_to_the_next_heading_of_the_same_or_higher_level():
    s = extras.overview_sections(DOC)
    assert [(a, b, t) for a, b, t in s] == [(2, 8, "Overview"), (11, 13, "Architecture")]
    assert extras.overview_sections("# Title\ntext\n## About the author\nx\n")[0][2] == "About the author"
    assert extras.overview_sections("```\n## Overview\n```\n") == []  # headings inside code fences do not count
    assert extras.overview_sections("## Tech Stack\na\n## Project Structure\nb\n")[0][2] == "Tech Stack"


def test_stack_members_and_bridge_detection():
    tree = ["CLAUDE.md", "AGENTS.md", ".cursor/rules/a.mdc", "docs/GEMINI.md", ".github/copilot-instructions.md", ".windsurfrules"]
    assert extras.stack_members(tree) == ["CLAUDE.md", "AGENTS.md", "cursor rules", "copilot instructions", "GEMINI.md", ".windsurfrules"]
    assert extras.stack_members(["README.md"]) == []
    assert extras.bridge_kind(True, "AGENTS.md", None) == "symlink" and extras.bridge_kind(True, "docs/x.md", None) is None
    assert extras.bridge_kind(False, None, "# Claude\n\n@AGENTS.md\n") == "import"
    assert extras.bridge_kind(False, None, "@AGENTS.md\n" + "Extra rule text. " * 40) is None  # more than a pointer
    assert extras.bridge_kind(False, None, "no import here") is None
