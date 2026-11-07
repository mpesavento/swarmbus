"""Pin docs/codex.md: it exists, and the README links it.

Narrower than the sibling doc guards on purpose -- pinning headings or prose
makes every doc edit a test failure carrying no information. The defect worth
catching is the doc shipping unlinked.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "codex.md"


def test_codex_doc_exists():
    assert DOC.is_file(), DOC


def test_readme_links_the_codex_doc():
    assert "(docs/codex.md)" in (ROOT / "README.md").read_text()
