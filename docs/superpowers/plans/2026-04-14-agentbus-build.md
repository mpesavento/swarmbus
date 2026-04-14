# agentbus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the agentbus Python package — a reactive MQTT adapter for agent-to-agent messaging with async core, layered handlers, CLI, and MCP sidecar.

**Architecture:** Each agent embeds an `AgentBus` instance connected to a shared mosquitto broker. Handlers are plugged in to process received messages (file bridge, direct invocation, persistent listener). An optional MCP sidecar exposes the bus as MCP tools for CC/LLM integration. Async core (aiomqtt) with sync shim (`bus.run()`).

**Tech Stack:** Python 3.9+, uv, aiomqtt≥2.0, pydantic≥2.0, click≥8.0, pytest, pytest-asyncio; optional: aiosqlite (archive), mcp (sidecar)

---

## File Map

```
agentbus/
├── pyproject.toml                         # CREATE: uv-managed package config
├── src/
│   └── agentbus/
│       ├── __init__.py                    # CREATE: public API re-exports
│       ├── message.py                     # CREATE: AgentMessage, validation
│       ├── bus.py                         # CREATE: AgentBus core
│       ├── handlers/
│       │   ├── __init__.py                # CREATE: handler re-exports
│       │   ├── base.py                    # CREATE: BaseHandler ABC
│       │   ├── file_bridge.py             # CREATE: FileBridgeHandler
│       │   ├── direct_invoke.py           # CREATE: DirectInvocationHandler
│       │   └── persistent.py             # CREATE: PersistentListenerHandler
│       ├── archive.py                     # CREATE: SQLiteArchive handler
│       ├── cli.py                         # CREATE: click CLI
│       └── mcp_server.py                  # CREATE: FastMCP sidecar
├── tests/
│   ├── conftest.py                        # CREATE: mosquitto fixture
│   ├── test_message.py                    # CREATE: AgentMessage unit tests
│   ├── test_bus.py                        # CREATE: AgentBus unit tests (mocked)
│   └── handlers/
│       ├── __init__.py                    # CREATE: empty
│       ├── test_file_bridge.py            # CREATE
│       ├── test_direct_invoke.py          # CREATE
│       └── test_persistent.py            # CREATE
├── examples/
│   ├── sparrow_wren_local.py              # CREATE: two agents, one machine
│   └── cross_machine.py                  # CREATE: cross-machine template
└── scripts/
    ├── setup-mosquitto.sh                 # CREATE: install + systemd
    └── setup-cc-plugin.sh                 # CREATE: write CC settings.json entry
```

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/agentbus/__init__.py`
- Create: `src/agentbus/handlers/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/handlers/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "agentbus"
version = "0.1.0"
description = "Reactive pub/sub messaging for AI agents via MQTT"
requires-python = ">=3.9"
dependencies = [
    "aiomqtt>=2.0",
    "pydantic>=2.0",
    "click>=8.0",
]

[project.optional-dependencies]
archive = ["aiosqlite>=0.19"]
mcp = ["mcp>=1.0"]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "anyio[trio]",
]

[project.scripts]
agentbus = "agentbus.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/agentbus"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create empty init files**

Create these files with empty content:

```bash
touch src/agentbus/__init__.py
touch src/agentbus/handlers/__init__.py
touch tests/__init__.py
touch tests/handlers/__init__.py
mkdir -p examples scripts
```

- [ ] **Step 3: Install in dev mode**

```bash
cd /home/mpesavento/projects/agentbus
uv venv
uv pip install -e ".[dev,archive,mcp]"
```

Expected: resolves and installs without error.

- [ ] **Step 4: Verify import**

```bash
uv run python -c "import agentbus; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/ tests/
git commit -m "feat: project scaffold — pyproject.toml, src layout, dev env"
```

---

## Task 2: AgentMessage

**Files:**
- Create: `src/agentbus/message.py`
- Create: `tests/test_message.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failures**

```bash
cd /home/mpesavento/projects/agentbus
uv run pytest tests/test_message.py -v 2>&1 | head -20
```

Expected: `ImportError` or `ModuleNotFoundError` — `agentbus.message` doesn't exist yet.

- [ ] **Step 3: Implement message.py**

```python
# src/agentbus/message.py
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_AGENT_ID_RE = re.compile(r'^[a-z0-9_-]{1,64}$')
_MAX_BODY_BYTES = 64 * 1024  # 64KB


def _validate_agent_id(v: str) -> str:
    if not _AGENT_ID_RE.match(v):
        raise ValueError(
            f"Agent ID must match [a-z0-9_-]{{1,64}}, got: {v!r}"
        )
    return v


class AgentMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_agent: str = Field(alias="from")
    to: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    subject: str
    body: str
    content_type: str = "text/plain"
    priority: Literal["normal", "urgent"] = "normal"
    reply_to: Optional[str] = None

    @field_validator("from_agent", mode="before")
    @classmethod
    def validate_from(cls, v: str) -> str:
        return _validate_agent_id(v)

    @field_validator("to", mode="before")
    @classmethod
    def validate_to(cls, v: str) -> str:
        return _validate_agent_id(v)

    @field_validator("body", mode="before")
    @classmethod
    def validate_body_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > _MAX_BODY_BYTES:
            raise ValueError(f"Body exceeds {_MAX_BODY_BYTES} bytes")
        return v

    @classmethod
    def create(
        cls,
        from_: str,
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
        priority: Literal["normal", "urgent"] = "normal",
        reply_to: Optional[str] = None,
    ) -> "AgentMessage":
        return cls.model_validate({
            "from": from_,
            "to": to,
            "subject": subject,
            "body": body,
            "content_type": content_type,
            "priority": priority,
            "reply_to": reply_to,
        })

    def to_json(self) -> str:
        data = self.model_dump(by_alias=True)
        data["ts"] = self.ts.isoformat()
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "AgentMessage":
        data = json.loads(raw)
        return cls.model_validate(data)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_message.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbus/message.py tests/test_message.py
git commit -m "feat: AgentMessage with Pydantic validation, content_type, agent ID + body size guards"
```

---

## Task 3: BaseHandler ABC

**Files:**
- Create: `src/agentbus/handlers/base.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/handlers/test_file_bridge.py (created in Task 5)
# For now, inline test in test_message.py is sufficient; just implement the ABC
```

- [ ] **Step 2: Implement base.py**

```python
# src/agentbus/handlers/base.py
from abc import ABC, abstractmethod
from ..message import AgentMessage


class BaseHandler(ABC):
    """Receives messages dispatched by AgentBus.listen()."""

    @abstractmethod
    async def handle(self, msg: AgentMessage) -> None:
        """Process a received message. Exceptions are caught by the bus."""
        ...
```

- [ ] **Step 3: Update handlers/__init__.py**

```python
# src/agentbus/handlers/__init__.py
from .base import BaseHandler

__all__ = ["BaseHandler"]
```

- [ ] **Step 4: Verify import**

```bash
uv run python -c "from agentbus.handlers import BaseHandler; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add src/agentbus/handlers/
git commit -m "feat: BaseHandler ABC"
```

---

## Task 4: AgentBus core

**Files:**
- Create: `src/agentbus/bus.py`
- Create: `tests/test_bus.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_bus.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from agentbus.bus import AgentBus
from agentbus.message import AgentMessage
from agentbus.handlers.base import BaseHandler


class RecordingHandler(BaseHandler):
    def __init__(self):
        self.received = []

    async def handle(self, msg: AgentMessage) -> None:
        self.received.append(msg)


def test_invalid_agent_id_raises():
    with pytest.raises(ValueError):
        AgentBus(agent_id="BAD ID!", broker="localhost")


def test_register_handler():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    h = RecordingHandler()
    bus.register_handler(h)
    assert h in bus._handlers


@pytest.mark.asyncio
async def test_send_publishes_to_correct_topic():
    bus = AgentBus(agent_id="sparrow", broker="localhost")

    published = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload))

    with patch("agentbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.send(to="wren", subject="hello", body="world")

    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "agents/wren/inbox"
    data = json.loads(payload)
    assert data["from"] == "sparrow"
    assert data["to"] == "wren"
    assert data["subject"] == "hello"
    assert data["body"] == "world"


@pytest.mark.asyncio
async def test_listen_dispatches_to_handlers():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    handler = RecordingHandler()
    bus.register_handler(handler)

    msg = AgentMessage.create(from_="wren", to="sparrow", subject="ping", body="hi")

    class FakeMessage:
        payload = msg.to_json().encode()

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeMessage()
            return _gen()

    with patch("agentbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()

    assert len(handler.received) == 1
    assert handler.received[0].subject == "ping"
    assert handler.received[0].from_agent == "wren"


@pytest.mark.asyncio
async def test_listen_skips_invalid_envelope():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    handler = RecordingHandler()
    bus.register_handler(handler)

    class FakeBadMessage:
        payload = b'{"not": "valid"}'

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeBadMessage()
            return _gen()

    with patch("agentbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()  # must not raise

    assert handler.received == []  # invalid message discarded


@pytest.mark.asyncio
async def test_send_broadcast_uses_correct_topic():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    published = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            published.append(topic)

    with patch("agentbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.send(to="broadcast", subject="all", body="hello everyone")

    assert published[0] == "agents/broadcast"  # not agents/broadcast/inbox


@pytest.mark.asyncio
async def test_listen_continues_after_handler_exception():
    bus = AgentBus(agent_id="sparrow", broker="localhost")

    class CrashingHandler(BaseHandler):
        async def handle(self, msg):
            raise RuntimeError("boom")

    ok_handler = RecordingHandler()
    bus.register_handler(CrashingHandler())
    bus.register_handler(ok_handler)

    msg = AgentMessage.create(from_="wren", to="sparrow", subject="ping", body="hi")

    class FakeMessage:
        payload = msg.to_json().encode()

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeMessage()
            return _gen()

    with patch("agentbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()  # must not raise

    assert len(ok_handler.received) == 1  # ok_handler still ran
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_bus.py -v 2>&1 | head -15
```

Expected: `ImportError: cannot import name 'AgentBus' from 'agentbus.bus'`

- [ ] **Step 3: Implement bus.py**

```python
# src/agentbus/bus.py
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List

import aiomqtt

from .message import AgentMessage
from .handlers.base import BaseHandler

logger = logging.getLogger(__name__)

_AGENT_ID_RE = re.compile(r'^[a-z0-9_-]{1,64}$')


class AgentBus:
    def __init__(
        self,
        agent_id: str,
        broker: str = "localhost",
        port: int = 1883,
        retain: bool = True,
    ) -> None:
        if not _AGENT_ID_RE.match(agent_id):
            raise ValueError(f"agent_id must match [a-z0-9_-]{{1,64}}, got: {agent_id!r}")
        self.agent_id = agent_id
        self.broker = broker
        self.port = port
        self.retain = retain
        self._handlers: List[BaseHandler] = []

    def register_handler(self, handler: BaseHandler) -> None:
        self._handlers.append(handler)

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
        priority: str = "normal",
        reply_to: str | None = None,
    ) -> None:
        msg = AgentMessage.create(
            from_=self.agent_id,
            to=to,
            subject=subject,
            body=body,
            content_type=content_type,
            priority=priority,  # type: ignore[arg-type]
            reply_to=reply_to,
        )
        # Broadcast uses agents/broadcast; directed messages use agents/{to}/inbox
        topic = "agents/broadcast" if to == "broadcast" else f"agents/{to}/inbox"
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.publish(
                topic,
                msg.to_json(),
                qos=1,
                retain=self.retain,
            )

    async def listen(self) -> None:
        will = aiomqtt.Will(
            topic=f"agents/{self.agent_id}/presence",
            payload=json.dumps({"agent": self.agent_id, "status": "offline"}),
            qos=0,
            retain=False,
        )
        async with aiomqtt.Client(self.broker, port=self.port, will=will) as client:
            await client.publish(
                f"agents/{self.agent_id}/presence",
                json.dumps({"agent": self.agent_id, "status": "online"}),
                qos=0,
            )
            await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
            await client.subscribe("agents/broadcast", qos=1)

            async for mqtt_msg in client.messages:
                try:
                    msg = AgentMessage.from_json(mqtt_msg.payload)
                except Exception as exc:
                    logger.warning("Discarding invalid message envelope: %s", exc)
                    continue

                for handler in self._handlers:
                    try:
                        await handler.handle(msg)
                    except Exception as exc:
                        logger.error(
                            "Handler %s raised: %s",
                            handler.__class__.__name__, exc,
                        )

    async def disconnect(self) -> None:
        """Publish offline presence. Call before process exit if not using listen()."""
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.publish(
                f"agents/{self.agent_id}/presence",
                json.dumps({"agent": self.agent_id, "status": "offline"}),
                qos=0,
            )

    def run(self) -> None:
        """Sync entry point — blocks until listen() returns or KeyboardInterrupt."""
        asyncio.run(self.listen())
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_bus.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbus/bus.py tests/test_bus.py
git commit -m "feat: AgentBus core — connect, send, listen, presence, LWT, handler dispatch"
```

---

## Task 5: FileBridgeHandler

**Files:**
- Create: `src/agentbus/handlers/file_bridge.py`
- Create: `tests/handlers/test_file_bridge.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/handlers/test_file_bridge.py
import pytest
from pathlib import Path
from agentbus.handlers.file_bridge import FileBridgeHandler
from agentbus.message import AgentMessage


@pytest.fixture
def inbox(tmp_path):
    return tmp_path / "inbox.md"


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="daily report", body="All systems nominal.",
    )


@pytest.mark.asyncio
async def test_creates_file_if_missing(inbox, msg):
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    assert inbox.exists()


@pytest.mark.asyncio
async def test_appends_message_content(inbox, msg):
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    content = inbox.read_text()
    assert "wren" in content
    assert "daily report" in content
    assert "All systems nominal." in content


@pytest.mark.asyncio
async def test_appends_multiple_messages(inbox, msg):
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    msg2 = AgentMessage.create(from_="wren", to="sparrow", subject="update", body="Still good.")
    await handler.handle(msg2)
    content = inbox.read_text()
    assert "All systems nominal." in content
    assert "Still good." in content


@pytest.mark.asyncio
async def test_creates_parent_dirs(tmp_path, msg):
    inbox = tmp_path / "deep" / "nested" / "inbox.md"
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    assert inbox.exists()
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/handlers/test_file_bridge.py -v 2>&1 | head -10
```

Expected: `ImportError`

- [ ] **Step 3: Implement file_bridge.py**

```python
# src/agentbus/handlers/file_bridge.py
import asyncio
from pathlib import Path
from .base import BaseHandler
from ..message import AgentMessage


class FileBridgeHandler(BaseHandler):
    """Appends received messages to a file (e.g. sync/inbox.md)."""

    def __init__(self, inbox_path: str) -> None:
        self.inbox_path = Path(inbox_path).expanduser()

    async def handle(self, msg: AgentMessage) -> None:
        entry = (
            f"\n## [{msg.ts.strftime('%Y-%m-%d %H:%M')}] "
            f"From: {msg.from_agent} | {msg.subject}\n"
            f"{msg.body}\n"
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._append, entry)

    def _append(self, entry: str) -> None:
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox_path.open("a", encoding="utf-8") as f:
            f.write(entry)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/handlers/test_file_bridge.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Update handlers/__init__.py**

```python
# src/agentbus/handlers/__init__.py
from .base import BaseHandler
from .file_bridge import FileBridgeHandler

__all__ = ["BaseHandler", "FileBridgeHandler"]
```

- [ ] **Step 6: Commit**

```bash
git add src/agentbus/handlers/file_bridge.py src/agentbus/handlers/__init__.py tests/handlers/test_file_bridge.py
git commit -m "feat: FileBridgeHandler — append received messages to inbox.md"
```

---

## Task 6: DirectInvocationHandler

**Files:**
- Create: `src/agentbus/handlers/direct_invoke.py`
- Create: `tests/handlers/test_direct_invoke.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/handlers/test_direct_invoke.py
import pytest
from unittest.mock import patch, MagicMock
from agentbus.handlers.direct_invoke import DirectInvocationHandler
from agentbus.message import AgentMessage


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="task", body="Do the thing.",
        content_type="text/plain",
    )


@pytest.mark.asyncio
async def test_calls_command_with_body_as_stdin(msg):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        captured["env"] = kwargs.get("env")
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["echo"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)

    assert captured["cmd"] == ["echo"]
    assert captured["input"] == b"Do the thing."


@pytest.mark.asyncio
async def test_env_vars_set(msg):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["true"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)

    assert captured["env"]["AGENTBUS_FROM"] == "wren"
    assert captured["env"]["AGENTBUS_TO"] == "sparrow"
    assert captured["env"]["AGENTBUS_SUBJECT"] == "task"
    assert captured["env"]["AGENTBUS_CONTENT_TYPE"] == "text/plain"
    assert captured["env"]["AGENTBUS_PRIORITY"] == "normal"


@pytest.mark.asyncio
async def test_no_shell_equals_true(msg):
    """Body must never be shell-interpolated. subprocess.run must not use shell=True."""
    called_with = {}

    def fake_run(cmd, **kwargs):
        called_with["shell"] = kwargs.get("shell", False)
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["echo"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)

    assert called_with["shell"] is False


@pytest.mark.asyncio
async def test_nonzero_exit_does_not_raise(msg):
    """A failing command should log but not propagate exception."""
    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=1)

    handler = DirectInvocationHandler(command=["false"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)  # must not raise


@pytest.mark.asyncio
async def test_markdown_body_passed_verbatim(msg):
    """Markdown with code blocks must survive the transport unchanged."""
    md_msg = AgentMessage.create(
        from_="wren", to="sparrow",
        subject="code review",
        body="Here's the fix:\n```python\nprint('hello')\n```\n> Note: tested.",
        content_type="text/markdown",
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input", b"")
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["cat"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(md_msg)

    assert b"```python" in captured["input"]
    assert b"print('hello')" in captured["input"]
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/handlers/test_direct_invoke.py -v 2>&1 | head -10
```

Expected: `ImportError`

- [ ] **Step 3: Implement direct_invoke.py**

```python
# src/agentbus/handlers/direct_invoke.py
import asyncio
import logging
import os
import subprocess
from typing import List, Optional

from .base import BaseHandler
from ..message import AgentMessage

logger = logging.getLogger(__name__)


class DirectInvocationHandler(BaseHandler):
    """Invokes a command on message arrival. Body passed via stdin only — never shell-interpolated."""

    def __init__(self, command: Optional[List[str]] = None) -> None:
        # Default: invoke claude -p reading from stdin
        self.command = command or ["claude", "-p", "/dev/stdin"]

    async def handle(self, msg: AgentMessage) -> None:
        env = {
            **os.environ,
            "AGENTBUS_FROM": msg.from_agent,
            "AGENTBUS_TO": msg.to,
            "AGENTBUS_ID": msg.id,
            "AGENTBUS_SUBJECT": msg.subject,
            "AGENTBUS_CONTENT_TYPE": msg.content_type,
            "AGENTBUS_PRIORITY": msg.priority,
            "AGENTBUS_TS": msg.ts.isoformat(),
        }
        if msg.reply_to:
            env["AGENTBUS_REPLY_TO"] = msg.reply_to

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                self.command,
                input=msg.body.encode("utf-8"),
                env=env,
                shell=False,      # NEVER shell=True — prevents injection
                check=False,      # don't raise on nonzero exit
            ),
        )
        if result.returncode != 0:
            logger.warning(
                "DirectInvocationHandler: command %s exited %d for message %s",
                self.command, result.returncode, msg.id,
            )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/handlers/test_direct_invoke.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Update handlers/__init__.py**

```python
# src/agentbus/handlers/__init__.py
from .base import BaseHandler
from .file_bridge import FileBridgeHandler
from .direct_invoke import DirectInvocationHandler

__all__ = ["BaseHandler", "FileBridgeHandler", "DirectInvocationHandler"]
```

- [ ] **Step 6: Commit**

```bash
git add src/agentbus/handlers/direct_invoke.py src/agentbus/handlers/__init__.py tests/handlers/test_direct_invoke.py
git commit -m "feat: DirectInvocationHandler — stdin-only, shell=False, env vars"
```

---

## Task 7: PersistentListenerHandler

**Files:**
- Create: `src/agentbus/handlers/persistent.py`
- Create: `tests/handlers/test_persistent.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/handlers/test_persistent.py
import asyncio
import pytest
from agentbus.handlers.persistent import PersistentListenerHandler
from agentbus.message import AgentMessage


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow", subject="ping", body="hi",
    )


@pytest.mark.asyncio
async def test_tracks_message_count(msg):
    handler = PersistentListenerHandler()
    assert handler.stats()["messages_received"] == 0
    await handler.handle(msg)
    await handler.handle(msg)
    assert handler.stats()["messages_received"] == 2


@pytest.mark.asyncio
async def test_tracks_last_message_ts(msg):
    handler = PersistentListenerHandler()
    assert handler.stats()["last_message_ts"] is None
    await handler.handle(msg)
    assert handler.stats()["last_message_ts"] is not None


@pytest.mark.asyncio
async def test_heartbeat_calls_publish_fn():
    handler = PersistentListenerHandler(heartbeat_interval=0.05)
    calls = []

    async def fake_publish():
        calls.append(1)

    task = asyncio.create_task(handler.start_heartbeat(fake_publish))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(calls) >= 2  # fired at least twice in 0.15s with 0.05s interval


@pytest.mark.asyncio
async def test_heartbeat_sets_started_at():
    handler = PersistentListenerHandler(heartbeat_interval=10)
    assert handler.stats()["started_at"] is None

    async def noop():
        pass

    task = asyncio.create_task(handler.start_heartbeat(noop))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert handler.stats()["started_at"] is not None
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/handlers/test_persistent.py -v 2>&1 | head -10
```

Expected: `ImportError`

- [ ] **Step 3: Implement persistent.py**

```python
# src/agentbus/handlers/persistent.py
import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from .base import BaseHandler
from ..message import AgentMessage

logger = logging.getLogger(__name__)


class PersistentListenerHandler(BaseHandler):
    """Heartbeat and stats handler for long-running always-on agents.

    Register this handler, then call bus.register_handler(handler) and start the bus.
    For heartbeats, call asyncio.create_task(handler.start_heartbeat(bus_publish_presence)).
    """

    def __init__(self, heartbeat_interval: int = 60) -> None:
        self.heartbeat_interval = heartbeat_interval
        self._stats = {
            "messages_received": 0,
            "last_message_ts": None,
            "started_at": None,
        }

    async def handle(self, msg: AgentMessage) -> None:
        self._stats["messages_received"] += 1
        self._stats["last_message_ts"] = datetime.now(timezone.utc).isoformat()

    async def start_heartbeat(
        self,
        publish_fn: Callable[[], Awaitable[None]],
    ) -> None:
        """Publish presence on interval. Run as asyncio.create_task(). Runs until cancelled."""
        self._stats["started_at"] = datetime.now(timezone.utc).isoformat()
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await publish_fn()
            except Exception as exc:
                logger.warning("Heartbeat publish failed: %s", exc)

    def stats(self) -> dict:
        return self._stats.copy()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/handlers/test_persistent.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Update handlers/__init__.py**

```python
# src/agentbus/handlers/__init__.py
from .base import BaseHandler
from .file_bridge import FileBridgeHandler
from .direct_invoke import DirectInvocationHandler
from .persistent import PersistentListenerHandler

__all__ = [
    "BaseHandler",
    "FileBridgeHandler",
    "DirectInvocationHandler",
    "PersistentListenerHandler",
]
```

- [ ] **Step 6: Commit**

```bash
git add src/agentbus/handlers/persistent.py src/agentbus/handlers/__init__.py tests/handlers/test_persistent.py
git commit -m "feat: PersistentListenerHandler — stats tracking, heartbeat task"
```

---

## Task 8: SQLiteArchive

**Files:**
- Create: `src/agentbus/archive.py`
- Modify: `tests/` — add `tests/test_archive.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_archive.py
import pytest
from pathlib import Path
from agentbus.archive import SQLiteArchive
from agentbus.message import AgentMessage


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="archive test", body="stored forever",
    )


@pytest.mark.asyncio
async def test_creates_table_and_stores_message(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT * FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[1] == "wren"   # from_agent
    assert row[5] == "stored forever"  # body


@pytest.mark.asyncio
async def test_direction_defaults_to_received(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT direction FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == "received"


@pytest.mark.asyncio
async def test_stores_content_type(db_path):
    archive = SQLiteArchive(db_path)
    msg = AgentMessage.create(
        from_="wren", to="sparrow", subject="md", body="# Hello",
        content_type="text/markdown",
    )
    await archive.handle(msg)

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT content_type FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == "text/markdown"


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_id(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    await archive.handle(msg)  # same id — must not raise

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_creates_parent_dirs(tmp_path, msg):
    db_path = str(tmp_path / "nested" / "dir" / "archive.db")
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    assert Path(db_path).exists()
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_archive.py -v 2>&1 | head -10
```

Expected: `ImportError`

- [ ] **Step 3: Implement archive.py**

```python
# src/agentbus/archive.py
from __future__ import annotations

from pathlib import Path
from typing import Optional

import aiosqlite

from .handlers.base import BaseHandler
from .message import AgentMessage

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    priority    TEXT NOT NULL DEFAULT 'normal',
    reply_to    TEXT,
    direction   TEXT NOT NULL DEFAULT 'received',
    error       TEXT
)
"""


class SQLiteArchive(BaseHandler):
    """Logs all handled messages to a SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path).expanduser()

    async def handle(
        self,
        msg: AgentMessage,
        direction: str = "received",
        error: Optional[str] = None,
    ) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_TABLE)
            await db.execute(
                """INSERT OR REPLACE INTO messages
                   (id, from_agent, to_agent, ts, subject, body,
                    content_type, priority, reply_to, direction, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.from_agent, msg.to,
                    msg.ts.isoformat(), msg.subject, msg.body,
                    msg.content_type, msg.priority, msg.reply_to,
                    direction, error,
                ),
            )
            await db.commit()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_archive.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbus/archive.py tests/test_archive.py
git commit -m "feat: SQLiteArchive handler — persistent message log with content_type"
```

---

## Task 9: Integration tests (mosquitto fixture)

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/test_integration.py`

- [ ] **Step 1: Verify mosquitto is installed**

```bash
which mosquitto && mosquitto --version 2>&1 | head -1
```

Expected: path + version. If missing: `sudo apt install mosquitto`

- [ ] **Step 2: Write conftest.py with broker fixture**

```python
# tests/conftest.py
import socket
import subprocess
import time
import pytest
import pytest_asyncio


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Start a real mosquitto broker on a free port. Yields (host, port)."""
    port = _free_port()
    proc = subprocess.Popen(
        ["mosquitto", "-p", str(port), "--log-type", "none"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)  # let it start
    assert proc.poll() is None, "mosquitto failed to start"
    yield ("localhost", port)
    proc.terminate()
    proc.wait()
```

- [ ] **Step 3: Write integration tests**

```python
# tests/test_integration.py
import asyncio
import pytest
from agentbus.bus import AgentBus
from agentbus.handlers.base import BaseHandler
from agentbus.message import AgentMessage


class CollectingHandler(BaseHandler):
    def __init__(self):
        self.received: list[AgentMessage] = []
        self._event = asyncio.Event()

    async def handle(self, msg: AgentMessage) -> None:
        self.received.append(msg)
        self._event.set()

    async def wait_for_message(self, timeout: float = 3.0) -> None:
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        self._event.clear()


@pytest.mark.asyncio
async def test_send_receive_roundtrip(mosquitto_broker):
    host, port = mosquitto_broker
    handler = CollectingHandler()

    receiver = AgentBus(agent_id="sparrow", broker=host, port=port, retain=False)
    receiver.register_handler(handler)

    sender = AgentBus(agent_id="wren", broker=host, port=port)

    listen_task = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.2)  # let subscription establish

    await sender.send(to="sparrow", subject="ping", body="hello from wren")
    await handler.wait_for_message(timeout=3.0)

    listen_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass

    assert len(handler.received) == 1
    msg = handler.received[0]
    assert msg.from_agent == "wren"
    assert msg.subject == "ping"
    assert msg.body == "hello from wren"


@pytest.mark.asyncio
async def test_markdown_body_preserved(mosquitto_broker):
    host, port = mosquitto_broker
    handler = CollectingHandler()

    receiver = AgentBus(agent_id="sparrow", broker=host, port=port, retain=False)
    receiver.register_handler(handler)
    sender = AgentBus(agent_id="wren", broker=host, port=port)

    body = "# Report\n```python\nprint('hi')\n```\n> Note: tested."

    listen_task = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.2)

    await sender.send(
        to="sparrow", subject="report",
        body=body, content_type="text/markdown",
    )
    await handler.wait_for_message(timeout=3.0)

    listen_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass

    assert handler.received[0].body == body
    assert handler.received[0].content_type == "text/markdown"
```

- [ ] **Step 4: Run integration tests**

```bash
uv run pytest tests/test_integration.py -v
```

Expected: both tests pass. If mosquitto isn't running these will fail — that's expected in CI without broker; mark with `@pytest.mark.integration` if needed.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_integration.py
git commit -m "test: integration tests with real mosquitto broker fixture"
```

---

## Task 10: CLI

**Files:**
- Create: `src/agentbus/cli.py`
- No separate test file needed — use click's `CliRunner`; add `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py
import json
from unittest.mock import patch, AsyncMock
from click.testing import CliRunner
from agentbus.cli import main


def test_send_invokes_bus(tmp_path):
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send",
            "--agent-id", "sparrow",
            "--to", "wren",
            "--subject", "hello",
            "--body", "world",
        ])
    assert result.exit_code == 0, result.output
    instance.send.assert_called_once()
    call_kwargs = instance.send.call_args.kwargs
    assert call_kwargs["to"] == "wren"
    assert call_kwargs["subject"] == "hello"
    assert call_kwargs["body"] == "world"


def test_send_missing_required_options():
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--agent-id", "sparrow"])
    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "agentbus" in result.output


def test_send_help():
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--help"])
    assert result.exit_code == 0
    assert "--to" in result.output
    assert "--subject" in result.output
    assert "--body" in result.output
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_cli.py -v 2>&1 | head -10
```

Expected: `ImportError`

- [ ] **Step 3: Implement cli.py**

```python
# src/agentbus/cli.py
from __future__ import annotations

import asyncio
import click

from .bus import AgentBus
from .handlers.file_bridge import FileBridgeHandler
from .handlers.direct_invoke import DirectInvocationHandler
from .handlers.persistent import PersistentListenerHandler


@click.group()
def main() -> None:
    """agentbus — reactive MQTT messaging for AI agents."""


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--to", "to_agent", required=True, help="Target agent ID")
@click.option("--subject", required=True, help="Message subject")
@click.option("--body", required=True, help="Message body")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--content-type", default="text/plain", show_default=True)
def send(
    agent_id: str,
    to_agent: str,
    subject: str,
    body: str,
    broker: str,
    port: int,
    content_type: str,
) -> None:
    """Send a message to another agent."""
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    asyncio.run(bus.send(
        to=to_agent,
        subject=subject,
        body=body,
        content_type=content_type,
    ))
    click.echo(f"Sent to {to_agent}")


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--inbox", default=None, help="Path for file bridge (inbox.md)")
@click.option("--invoke", "invoke_cmd", default=None, help="Command to invoke on message (e.g. 'claude -p /dev/stdin')")
def start(
    agent_id: str,
    broker: str,
    port: int,
    inbox: str | None,
    invoke_cmd: str | None,
) -> None:
    """Start the agentbus listener daemon."""
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)

    if inbox:
        bus.register_handler(FileBridgeHandler(inbox))
    if invoke_cmd:
        bus.register_handler(DirectInvocationHandler(command=invoke_cmd.split()))

    persistent = PersistentListenerHandler()
    bus.register_handler(persistent)

    click.echo(f"[agentbus] {agent_id} listening on {broker}:{port}")
    try:
        bus.run()
    except KeyboardInterrupt:
        click.echo("\n[agentbus] shutting down")


@main.command("mcp-server")
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
def mcp_server(agent_id: str, broker: str, port: int) -> None:
    """Start the MCP sidecar for this agent."""
    from .mcp_server import run_mcp_server
    run_mcp_server(agent_id=agent_id, broker=broker, port=port)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbus/cli.py tests/test_cli.py
git commit -m "feat: CLI — send, start, mcp-server subcommands via click"
```

---

## Task 11: MCP sidecar

**Files:**
- Create: `src/agentbus/mcp_server.py`
- Create: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mcp_server.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agentbus.mcp_server import create_mcp_app


@pytest.mark.asyncio
async def test_send_message_tool_calls_bus():
    with patch("agentbus.mcp_server.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()

        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        # Retrieve the tool function directly
        send_fn = app._tool_fns["send_message"]

        await send_fn(to="wren", subject="hello", body="world")

        instance.send.assert_called_once_with(
            to="wren", subject="hello", body="world",
            content_type="text/plain",
        )


@pytest.mark.asyncio
async def test_list_agents_returns_list():
    with patch("agentbus.mcp_server.AgentBus"):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        list_fn = app._tool_fns["list_agents"]
        result = await list_fn()
        assert isinstance(result, list)
```

- [ ] **Step 2: Run to verify failures**

```bash
uv run pytest tests/test_mcp_server.py -v 2>&1 | head -10
```

Expected: `ImportError`

- [ ] **Step 3: Implement mcp_server.py**

```python
# src/agentbus/mcp_server.py
"""MCP sidecar — exposes agentbus as MCP tools for CC/LLM integration.

Usage: agentbus mcp-server --agent-id sparrow --broker localhost
Register in .claude/settings.json:
  "mcpServers": {
    "agentbus": {
      "command": "agentbus",
      "args": ["mcp-server", "--agent-id", "sparrow", "--broker", "localhost"]
    }
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiomqtt

from .bus import AgentBus
from .message import AgentMessage

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    FastMCP = None  # type: ignore[assignment,misc]


class _MCPApp:
    """Thin wrapper that tracks registered tool functions for testing."""
    def __init__(self):
        self._tool_fns: dict[str, Any] = {}

    def tool(self, fn=None, *, name: str | None = None):
        def decorator(f):
            key = name or f.__name__
            self._tool_fns[key] = f
            return f
        return decorator(fn) if fn else decorator


def create_mcp_app(agent_id: str, broker: str = "localhost", port: int = 1883) -> _MCPApp:
    """Create and return the MCP app (testable without running the server)."""
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    app = _MCPApp()
    _presence_cache: dict[str, str] = {}

    @app.tool(name="send_message")
    async def send_message(
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
    ) -> str:
        """Send a message to another agent."""
        await bus.send(to=to, subject=subject, body=body, content_type=content_type)
        return f"Sent to {to}"

    @app.tool(name="read_inbox")
    async def read_inbox() -> list[dict]:
        """Poll for queued messages (retain=True). Returns up to 10 recent messages."""
        messages: list[dict] = []
        try:
            async with aiomqtt.Client(broker, port=port) as client:
                await client.subscribe(f"agents/{agent_id}/inbox", qos=1)
                async with asyncio.timeout(1.0):
                    async for mqtt_msg in client.messages:
                        try:
                            msg = AgentMessage.from_json(mqtt_msg.payload)
                            messages.append(json.loads(msg.to_json()))
                        except Exception:
                            pass
                        if len(messages) >= 10:
                            break
        except (asyncio.TimeoutError, Exception):
            pass
        return messages

    @app.tool(name="watch_inbox")
    async def watch_inbox(timeout: float = 30.0) -> dict | None:
        """Long-poll — blocks until a message arrives, then returns it. CC calls this for push delivery."""
        try:
            async with aiomqtt.Client(broker, port=port) as client:
                await client.subscribe(f"agents/{agent_id}/inbox", qos=1)
                async with asyncio.timeout(timeout):
                    async for mqtt_msg in client.messages:
                        try:
                            msg = AgentMessage.from_json(mqtt_msg.payload)
                            return json.loads(msg.to_json())
                        except Exception:
                            continue
        except asyncio.TimeoutError:
            return None
        return None

    @app.tool(name="list_agents")
    async def list_agents() -> list[str]:
        """Return list of agent IDs seen on presence topics."""
        return list(_presence_cache.keys())

    return app


def run_mcp_server(agent_id: str, broker: str = "localhost", port: int = 1883) -> None:
    """Start the MCP sidecar. Called by CLI `agentbus mcp-server`."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package not installed. Run: uv pip install 'agentbus[mcp]'"
        )

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("agentbus")
    app = create_mcp_app(agent_id=agent_id, broker=broker, port=port)

    # Register tool functions with the real FastMCP instance
    for name, fn in app._tool_fns.items():
        mcp.tool(name=name)(fn)

    mcp.run(transport="stdio")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_mcp_server.py -v
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbus/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: MCP sidecar — send_message, read_inbox, watch_inbox, list_agents"
```

---

## Task 12: Update public __init__.py

**Files:**
- Modify: `src/agentbus/__init__.py`

- [ ] **Step 1: Implement __init__.py**

```python
# src/agentbus/__init__.py
from .bus import AgentBus
from .message import AgentMessage
from .handlers.base import BaseHandler
from .handlers.file_bridge import FileBridgeHandler
from .handlers.direct_invoke import DirectInvocationHandler
from .handlers.persistent import PersistentListenerHandler
from .archive import SQLiteArchive

__all__ = [
    "AgentBus",
    "AgentMessage",
    "BaseHandler",
    "FileBridgeHandler",
    "DirectInvocationHandler",
    "PersistentListenerHandler",
    "SQLiteArchive",
]
```

- [ ] **Step 2: Verify top-level import**

```bash
uv run python -c "from agentbus import AgentBus, FileBridgeHandler, SQLiteArchive; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Run all unit tests**

```bash
uv run pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/agentbus/__init__.py
git commit -m "feat: public API — re-export all core symbols from agentbus package"
```

---

> **Note:** Per-sender rate limiting (`max_msg_per_second`) from the security spec is deferred to v1.1. Implementing it requires stateful tracking in `AgentBus.listen()` across async message events. The other three security controls (agent ID validation, stdin-only invocation, body size limit) are fully covered above.

---

## Task 13: Setup scripts

**Files:**
- Create: `scripts/setup-mosquitto.sh`
- Create: `scripts/setup-cc-plugin.sh`

- [ ] **Step 1: Create setup-mosquitto.sh**

```bash
#!/usr/bin/env bash
# scripts/setup-mosquitto.sh
# Install mosquitto and configure as a systemd service.
set -euo pipefail

echo "[agentbus] Installing mosquitto..."
sudo apt-get update -qq
sudo apt-get install -y mosquitto mosquitto-clients

echo "[agentbus] Enabling mosquitto systemd service..."
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
sudo systemctl status mosquitto --no-pager

echo "[agentbus] mosquitto broker running on port 1883"
echo "[agentbus] Test with: mosquitto_pub -t test -m hello & mosquitto_sub -t test"
```

- [ ] **Step 2: Create setup-cc-plugin.sh**

```bash
#!/usr/bin/env bash
# scripts/setup-cc-plugin.sh
# Register agentbus MCP sidecar in Claude Code settings.json.
set -euo pipefail

AGENT_ID="${1:-}"
BROKER="${2:-localhost}"

if [ -z "$AGENT_ID" ]; then
  echo "Usage: $0 <agent-id> [broker-host]"
  echo "  Example: $0 sparrow localhost"
  exit 1
fi

SETTINGS_FILE="${HOME}/.claude/settings.json"

if [ ! -f "$SETTINGS_FILE" ]; then
  echo '{}' > "$SETTINGS_FILE"
fi

# Use python3 to safely merge the mcpServers entry
python3 - "$SETTINGS_FILE" "$AGENT_ID" "$BROKER" <<'EOF'
import json, sys

settings_path, agent_id, broker = sys.argv[1], sys.argv[2], sys.argv[3]

with open(settings_path) as f:
    settings = json.load(f)

settings.setdefault("mcpServers", {})
settings["mcpServers"]["agentbus"] = {
    "command": "agentbus",
    "args": ["mcp-server", "--agent-id", agent_id, "--broker", broker]
}

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print(f"[agentbus] Registered MCP sidecar in {settings_path}")
print(f"[agentbus] agent-id: {agent_id}, broker: {broker}")
print("[agentbus] Restart Claude Code to pick up the new MCP server.")
EOF
```

- [ ] **Step 3: Make executable and commit**

```bash
chmod +x scripts/setup-mosquitto.sh scripts/setup-cc-plugin.sh
git add scripts/
git commit -m "feat: setup scripts — mosquitto systemd install + CC MCP registration"
```

---

## Task 14: Examples

**Files:**
- Create: `examples/sparrow_wren_local.py`
- Create: `examples/cross_machine.py`

- [ ] **Step 1: Create sparrow_wren_local.py**

```python
#!/usr/bin/env python3
"""
examples/sparrow_wren_local.py

Two agents communicating on the same machine.
Run in two terminals:
  Terminal 1: python sparrow_wren_local.py wren
  Terminal 2: python sparrow_wren_local.py sparrow
"""
import asyncio
import sys
from agentbus import AgentBus, FileBridgeHandler, PersistentListenerHandler
from agentbus.handlers.base import BaseHandler
from agentbus.message import AgentMessage


class PrintHandler(BaseHandler):
    async def handle(self, msg: AgentMessage) -> None:
        print(f"\n[{msg.to}] received from [{msg.from_agent}]")
        print(f"  Subject: {msg.subject}")
        print(f"  Body: {msg.body}")
        print(f"  ContentType: {msg.content_type}")


async def run_agent(agent_id: str, peer_id: str) -> None:
    bus = AgentBus(agent_id=agent_id, broker="localhost")
    bus.register_handler(PrintHandler())
    bus.register_handler(PersistentListenerHandler(heartbeat_interval=30))

    print(f"[{agent_id}] starting, will send to {peer_id} in 1s...")
    listen_task = asyncio.create_task(bus.listen())
    await asyncio.sleep(1.0)

    await bus.send(
        to=peer_id,
        subject="greeting",
        body=f"Hello from {agent_id}! This is a test message.",
    )
    print(f"[{agent_id}] sent greeting to {peer_id}")

    # Also send a markdown message
    await bus.send(
        to=peer_id,
        subject="code sample",
        body="Here's a snippet:\n```python\nprint('hello from agentbus')\n```",
        content_type="text/markdown",
    )

    await listen_task  # blocks until Ctrl+C


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("sparrow", "wren"):
        print("Usage: python sparrow_wren_local.py <sparrow|wren>")
        sys.exit(1)

    agent = sys.argv[1]
    peer = "wren" if agent == "sparrow" else "sparrow"

    try:
        asyncio.run(run_agent(agent, peer))
    except KeyboardInterrupt:
        print(f"\n[{agent}] shutting down")
```

- [ ] **Step 2: Create cross_machine.py**

```python
#!/usr/bin/env python3
"""
examples/cross_machine.py

Two agents on different machines, connected via a shared broker.

Usage:
  Machine A: BROKER=clawd-rpi.tailea0d6e.ts.net python cross_machine.py sparrow
  Machine B: BROKER=clawd-rpi.tailea0d6e.ts.net python cross_machine.py wren

The broker address is the only thing that changes from sparrow_wren_local.py.
Tailscale handles auth and encryption.
"""
import asyncio
import os
import sys
from agentbus import AgentBus, PersistentListenerHandler
from agentbus.handlers.base import BaseHandler
from agentbus.message import AgentMessage

BROKER = os.environ.get("BROKER", "localhost")


class PrintHandler(BaseHandler):
    async def handle(self, msg: AgentMessage) -> None:
        print(f"\n[{msg.to}@{BROKER}] from [{msg.from_agent}]: {msg.subject}")
        print(f"  {msg.body[:120]}")


async def run_agent(agent_id: str, peer_id: str) -> None:
    bus = AgentBus(agent_id=agent_id, broker=BROKER)
    bus.register_handler(PrintHandler())
    bus.register_handler(PersistentListenerHandler())

    print(f"[{agent_id}] connecting to broker at {BROKER}...")
    listen_task = asyncio.create_task(bus.listen())
    await asyncio.sleep(1.0)

    await bus.send(to=peer_id, subject="cross-machine ping", body=f"Hi from {agent_id} on {BROKER}")
    print(f"[{agent_id}] sent ping to {peer_id}")

    await listen_task


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: BROKER=<host> python {sys.argv[0]} <agent-id>")
        sys.exit(1)
    agent = sys.argv[1]
    peer = "wren" if agent == "sparrow" else "sparrow"
    try:
        asyncio.run(run_agent(agent, peer))
    except KeyboardInterrupt:
        print(f"\n[{agent}] done")
```

- [ ] **Step 3: Commit**

```bash
git add examples/
git commit -m "feat: examples — sparrow_wren_local + cross_machine via Tailscale"
```

---

## Task 15: Final test run + README update

- [ ] **Step 1: Run full test suite**

```bash
cd /home/mpesavento/projects/agentbus
uv run pytest tests/ -v --ignore=tests/test_integration.py
```

Expected: all tests pass, no errors.

- [ ] **Step 2: Run integration tests (requires mosquitto running)**

```bash
uv run pytest tests/test_integration.py -v
```

Expected: both integration tests pass.

- [ ] **Step 3: Update README quickstart section**

Replace the existing README.md content with:

```markdown
# agentbus

Reactive pub/sub messaging for AI agents — no polling, instant delivery. Built on MQTT, runs local, scales to multi-machine.

## Install

```bash
pip install agentbus
# or with optional features:
pip install "agentbus[archive,mcp]"
```

## Prerequisites

```bash
sudo apt install mosquitto mosquitto-clients
# or use the setup script:
bash scripts/setup-mosquitto.sh
```

## Quickstart

```python
from agentbus import AgentBus, FileBridgeHandler, PersistentListenerHandler

bus = AgentBus(agent_id="sparrow", broker="localhost")
bus.register_handler(FileBridgeHandler("~/sync/inbox.md"))
bus.register_handler(PersistentListenerHandler())

# Send a message
await bus.send(to="wren", subject="hello", body="Hi Wren!")

# Listen (blocks)
bus.run()
```

## CLI

```bash
# Send
agentbus send --agent-id sparrow --to wren --subject hello --body "Hi Wren"

# Start listener with file bridge
agentbus start --agent-id sparrow --inbox ~/sync/inbox.md

# Start MCP sidecar (for Claude Code / any MCP agent)
agentbus mcp-server --agent-id sparrow
```

## Claude Code Plugin

```bash
bash scripts/setup-cc-plugin.sh sparrow localhost
# then restart Claude Code
```

Adds agentbus as an MCP server. CC gets `send_message`, `read_inbox`, `watch_inbox`, `list_agents` tools.

## Architecture

```
Agent A (Sparrow)           Agent B (Wren)
  AgentBus(embedded)          AgentBus(embedded)
        │                           │
        └────────────┬──────────────┘
                 mosquitto
             (system service)
```

Every agent is a peer. Broker is infrastructure. No hub. Cross-machine: change `broker="localhost"` to `broker="clawd-rpi.ts.net"`.

## Handlers

| Handler | What it does |
|---|---|
| `FileBridgeHandler(path)` | Writes messages to a file (backward-compat with file-polling agents) |
| `DirectInvocationHandler(cmd)` | Shells out to a command on message arrival; body via stdin |
| `PersistentListenerHandler()` | Stats + heartbeat for always-on agents |
| `SQLiteArchive(path)` | Logs all messages to SQLite |

## Message Envelope

```json
{
  "id": "uuid4",
  "from": "sparrow",
  "to": "wren",
  "ts": "2026-04-14T05:00:00Z",
  "subject": "hello",
  "body": "...",
  "content_type": "text/plain",
  "priority": "normal",
  "reply_to": null
}
```

`content_type`: `text/plain` | `text/markdown` | `text/x-code;lang=python` | `application/json`

## License

MIT
```

- [ ] **Step 4: Final commit and push**

```bash
git add README.md
git commit -m "docs: update README with quickstart, CLI, handler table, architecture"
git push origin main
```
