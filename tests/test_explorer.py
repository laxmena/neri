import re
import shutil
import subprocess

import pytest

from neri import explorer


def test_template_has_single_data_slot_and_no_external_resources():
    t = explorer.TEMPLATE.read_text()
    assert t.count("__DATA__") == 1
    # local-only page: loads nothing from the network (outbound GitHub links the user clicks are fine)
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|<img[^>]+src=[\"']?https?:|@import\s+(?:url|[\"'])|fetch\(|XMLHttpRequest", t)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_template_script_parses(tmp_path):
    js = re.search(r"<script>(.*)</script>", explorer.TEMPLATE.read_text(), re.S).group(1).replace("__DATA__", "{}")
    f = tmp_path / "x.js"
    f.write_text(js)
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
