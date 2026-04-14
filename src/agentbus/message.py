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
