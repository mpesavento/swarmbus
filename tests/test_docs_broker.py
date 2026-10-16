"""Pin the broker docs to what mosquitto and swarmbus actually do."""

from pathlib import Path

from swarmbus.topics import DEFAULT_TOPICS

ROOT = Path(__file__).resolve().parent.parent


def test_readme_rate_limit_advice_names_real_mosquitto_options():
    text = (ROOT / "README.md").read_text()

    # mosquitto has no such option and refuses to start with it.
    assert "message_rate_limit" not in text
    assert "max_queued_messages" in text
    assert "https://github.com/mpesavento/swarmbus/issues/26" in text


def test_security_doc_acl_matches_topic_layout():
    text = (ROOT / "docs" / "security.md").read_text()

    expected = [
        f"pattern read {DEFAULT_TOPICS.inbox('%u')}",
        f"pattern readwrite {DEFAULT_TOPICS.presence('%u')}",
        f"topic write {DEFAULT_TOPICS.inbox('+')}",
        f"topic read {DEFAULT_TOPICS.any_presence_filter()}",
        f"topic readwrite {DEFAULT_TOPICS.broadcast}",
        "allow_anonymous false",
        "mosquitto_passwd",
    ]
    missing = [line for line in expected if line not in text]
    assert not missing, missing


def test_docs_describe_rate_limit_broadcast_rule_send_exit_3_and_hop_limit_8():
    readme = (ROOT / "README.md").read_text()
    skill = (ROOT / "src" / "swarmbus" / "skills" / "using-swarmbus" / "SKILL.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert "swarmbus has no rate limiter of its own" not in readme
    for text in (readme, changelog):
        for token in ("--rate-limit", "--rate-cooldown", "SWARMBUS_RATE_LIMIT", "SWARMBUS_RATE_COOLDOWN"):
            assert token in text, token
    assert "default 8" in readme and "default 4" not in readme
    assert "never trips its sender" in readme
    assert "exits 3" in readme and "--force" in readme

    assert "default 8" in skill and "default 4" not in skill
    assert "exits 3" in skill and "Do not retry with `--force`" in skill
    assert "20 messages a minute" in skill

    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "default 8" in unreleased and "default 4" not in unreleased
    assert "**Per-sender rate limit at every listener**" in unreleased
    assert "never trips its sender" in unreleased
    assert "exits 3" in unreleased
    assert "`cooling` list" in unreleased
    assert unreleased.count("### Wire-compat") == 1
