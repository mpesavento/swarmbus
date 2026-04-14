# tests/test_message.py
import json
import pytest
from agentbus.message import AgentMessage

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

def test_priority_urgent():
    msg = AgentMessage.create(
        from_="sparrow", to="wren", subject="urgent!", body="now",
        priority="urgent",
    )
    assert msg.priority == "urgent"
    raw = msg.to_json()
    restored = AgentMessage.from_json(raw)
    assert restored.priority == "urgent"

def test_reply_to():
    original = AgentMessage.create(from_="sparrow", to="wren", subject="q", body="?")
    reply = AgentMessage.create(
        from_="wren", to="sparrow", subject="re: q", body="!",
        reply_to=original.id,
    )
    raw = reply.to_json()
    restored = AgentMessage.from_json(raw)
    assert restored.reply_to == original.id
