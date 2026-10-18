"""Pin the test harness and CI to how drydock runs the suite."""
import re
from pathlib import Path

import tests.conftest as conftest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_mosquitto_found_on_path_then_fallbacks(tmp_path, monkeypatch):
    on_path = tmp_path / "bin" / "mosquitto"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert conftest._mosquitto_bin() == str(on_path)

    fallback = tmp_path / "sbin" / "mosquitto"
    fallback.parent.mkdir()
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(conftest, "MOSQUITTO_FALLBACKS", (str(tmp_path / "nope"), str(fallback)))
    assert conftest._mosquitto_bin() == str(fallback)
    monkeypatch.setattr(conftest, "MOSQUITTO_FALLBACKS", (str(tmp_path / "nope"),))
    assert conftest._mosquitto_bin() is None


def test_workflow_actions_are_pinned_by_sha():
    uses = [
        m.group(1)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for m in re.finditer(r"^\s*(?:-\s*)?uses:\s*(\S+)", path.read_text(), re.M)
    ]
    assert uses
    unpinned = [u for u in uses if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u)]
    assert not unpinned, unpinned


def test_ci_installs_from_the_lockfile():
    text = (WORKFLOWS / "test.yml").read_text()
    assert "uv sync --frozen --all-extras" in text
    assert "uv run --frozen pytest" in text
    assert "pip install" not in text
