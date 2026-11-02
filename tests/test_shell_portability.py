"""Keep the shell scripts parseable by macOS /bin/bash 3.2, which CI (Linux, bash 5) never runs (#71)."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted([*ROOT.glob("examples/*.sh"), *ROOT.glob("scripts/*.sh")])
HEREDOC_IN_SUBST = re.compile(r"\$\(cat <<-?'?(\w+)'?")


def _bash3():
    try:
        out = subprocess.run(["/bin/bash", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return "version 3." in out


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_heredocs_in_command_substitution_have_balanced_quotes(script):
    # bash 3.2 matches quotes inside a heredoc in $( ... ), so one apostrophe
    # ("host's") leaves the rest of the script unparseable. Read such text with
    # `IFS= read -r -d '' var <<EOF || true` instead.
    lines = script.read_text().splitlines()
    for i, line in enumerate(lines):
        m = HEREDOC_IN_SUBST.search(line)
        if not m:
            continue
        body = []
        for later in lines[i + 1:]:
            if later.strip() == m.group(1):
                break
            body.append(later)
        text = "\n".join(body)
        assert text.count("'") % 2 == 0, f"{script.name}:{i + 1}: odd number of ' in heredoc"
        assert text.count('"') % 2 == 0, f"{script.name}:{i + 1}: odd number of \" in heredoc"
        assert text.count("(") == text.count(")"), f"{script.name}:{i + 1}: unbalanced () in heredoc"


@pytest.mark.skipif(not _bash3(), reason="needs bash 3 at /bin/bash (macOS)")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_under_bash_3(script):
    result = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
