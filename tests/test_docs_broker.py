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
