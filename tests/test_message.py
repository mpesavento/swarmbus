# tests/test_message.py
import json
import pytest
from swarmbus.message import AgentMessage

def test_create_sets_id_and_ts():
    msg = AgentMessage.create(
        from_="sparrow", to="wren",
        subject="hello", body="world",
    )
    assert msg.id  # uuid4 string
    assert msg.ts is not None
    assert msg.from_agent == "sparrow"
    assert msg.to == "wren"
    assert msg.content_type == "text/plain"
    assert msg.priority == "normal"
    assert msg.reply_to is None

def test_roundtrip_json():
    msg = AgentMessage.create(from_="sparrow", to="wren", subject="hi", body="hello")
    raw = msg.to_json()
    data = json.loads(raw)
    assert data["from"] == "sparrow"  # wire format uses "from"
    assert "from_" not in data
    restored = AgentMessage.from_json(raw)
    assert restored.from_agent == msg.from_agent
    assert restored.body == msg.body
    assert restored.id == msg.id

def test_content_type_preserved():
    msg = AgentMessage.create(
        from_="sparrow", to="wren", subject="code",
        body="```python\nprint('hi')\n```",
        content_type="text/markdown",
    )
    raw = msg.to_json()
    restored = AgentMessage.from_json(raw)
    assert restored.content_type == "text/markdown"
    assert "```python" in restored.body  # body preserved verbatim

def test_invalid_agent_id_rejected():
    with pytest.raises(Exception):
        AgentMessage.create(from_="SPARROW", to="wren", subject="hi", body="x")

def test_agent_id_spaces_rejected():
    with pytest.raises(Exception):
        AgentMessage.create(from_="sparrow agent", to="wren", subject="hi", body="x")

def test_agent_id_valid_chars():
    msg = AgentMessage.create(from_="my-agent_1", to="wren-2", subject="hi", body="x")
    assert msg.from_agent == "my-agent_1"

def test_body_size_limit():
    big_body = "x" * (64 * 1024 + 1)
    with pytest.raises(Exception):
        AgentMessage.create(from_="sparrow", to="wren", subject="big", body=big_body)

def test_from_json_invalid_envelope():
    with pytest.raises(Exception):
        AgentMessage.from_json('{"not": "valid"}')

def test_priority_high():
    msg = AgentMessage.create(
        from_="sparrow", to="wren", subject="urgent!", body="now",
        priority="high",
    )
    assert msg.priority == "high"
    raw = msg.to_json()
    restored = AgentMessage.from_json(raw)
    assert restored.priority == "high"


def test_priority_low():
    msg = AgentMessage.create(
        from_="sparrow", to="wren", subject="fyi", body="fire and forget",
        priority="low",
    )
    assert msg.priority == "low"


def test_priority_accepts_unknown_strings_for_forward_compat():
    """Regression test: the envelope must pass through unknown priority
    strings rather than raising. A real incident occurred when an earlier
    version used Literal["normal", "urgent"] — a newer peer emitting
    priority="high" caused older daemons to silently discard every such
    message (ValidationError at from_json). The Literal is now gone;
    priority is `str`, and callers that care validate at the application
    layer (e.g. wake wrappers gating on "high"). Unknown values MUST NOT
    raise."""
    msg = AgentMessage.create(
        from_="sparrow", to="wren", subject="x", body="y",
        priority="some-future-priority-value",
    )
    assert msg.priority == "some-future-priority-value"
    # And it must round-trip through JSON the same way:
    restored = AgentMessage.from_json(msg.to_json())
    assert restored.priority == "some-future-priority-value"

def test_reply_to():
    original = AgentMessage.create(from_="sparrow", to="wren", subject="q", body="?")
    reply = AgentMessage.create(
        from_="wren", to="sparrow", subject="re: q", body="!",
        reply_to=original.id,
    )
    raw = reply.to_json()
    restored = AgentMessage.from_json(raw)
    assert restored.reply_to == original.id
