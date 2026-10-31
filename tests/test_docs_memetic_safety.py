"""Pin docs/memetic-safety.md: it exists, keeps its sections, and is linked."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "memetic-safety.md"

HEADINGS = [
    "# Memetic safety: storing what agents say",
    "## Peer messages are claims, not facts",
    "## Record provenance",
    "## A promotion gate",
    "## Rank by quality, not recency",
    "## Do not rely on wiping memory",
    "## Audit the store",
    "## What swarmbus does and does not do",
    "## Background",
]


def test_memetic_safety_doc_has_its_sections_in_order():
    lines = DOC.read_text().splitlines()
    found = [h for h in HEADINGS if h in lines]
    assert found == HEADINGS, sorted(set(HEADINGS) - set(found))
    positions = [lines.index(h) for h in HEADINGS]
    assert positions == sorted(positions)


def test_memetic_safety_doc_names_the_envelope_fields_and_its_sources():
    text = DOC.read_text()
    for token in (
        "source: agent:<id>", "in_reply_to", "origin", "hops", "message_id",
        "Lying in `from`",
        "https://github.com/mpesavento/swarmbus/issues/26",
        "https://swarm-ai.org/blog/memetic-spread-reset-vs-detection/",
        "small mechanism model",
    ):
        assert token in text, token
    assert "released" not in text.lower()


def test_readme_and_skill_link_the_memetic_safety_doc():
    readme = (ROOT / "README.md").read_text()
    skill = (ROOT / "src" / "swarmbus" / "skills" / "using-swarmbus" / "SKILL.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert "(docs/memetic-safety.md)" in readme
    assert "`origin` names the agent that sent the first message of the chain" in readme
    assert "blob/main/docs/memetic-safety.md" in skill
    assert "source: agent:" in skill

    release = changelog.split("## [0.2.1]", 1)[1].split("\n## [", 1)[0]
    assert "`docs/memetic-safety.md`" in release
    assert release.count("### Wire-compat") == 1
