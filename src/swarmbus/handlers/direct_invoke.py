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
            "SWARMBUS_FROM": msg.from_agent,
            "SWARMBUS_TO": msg.to,
            "SWARMBUS_ID": msg.id,
            "SWARMBUS_SUBJECT": msg.subject,
            "SWARMBUS_CONTENT_TYPE": msg.content_type,
            "SWARMBUS_PRIORITY": msg.priority,
            "SWARMBUS_TS": msg.ts.isoformat(),
        }
        if msg.reply_to:
            env["SWARMBUS_REPLY_TO"] = msg.reply_to

        loop = asyncio.get_running_loop()
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
