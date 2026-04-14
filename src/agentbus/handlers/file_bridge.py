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
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._append, entry)

    def _append(self, entry: str) -> None:
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox_path.open("a", encoding="utf-8") as f:
            f.write(entry)
