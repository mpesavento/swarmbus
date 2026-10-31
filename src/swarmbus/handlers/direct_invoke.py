import asyncio
import logging
import os
import signal
import subprocess
from typing import Dict, List, Optional

from .base import BaseHandler
from ..message import AgentMessage, sanitize_field

logger = logging.getLogger(__name__)

# Seconds a wake may run before the daemon kills it and moves on. A hung
# wake (a model that never answers, a gateway that never replies) would
# otherwise block the bus's sequential handler loop for every later message.
DEFAULT_INVOKE_TIMEOUT = 900.0


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the command and everything it spawned.

    The command runs in its own session (``start_new_session=True``), so
    its process group id is its pid and one ``killpg`` reaches the wake
    script and the agent process it piped the prompt into.
    """
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows has no process groups
            proc.kill()
    except OSError:
        pass  # already gone


class DirectInvocationHandler(BaseHandler):
    """Invokes a command on message arrival. Body passed via stdin only — never shell-interpolated.

    ``timeout`` bounds each invocation in seconds (default 900). When it
    expires the command's whole process group is killed, a warning is
    logged, and the daemon goes on to the next message. ``None`` or ``0``
    waits forever.
    """

    def __init__(
        self,
        command: Optional[List[str]] = None,
        timeout: Optional[float] = DEFAULT_INVOKE_TIMEOUT,
    ) -> None:
        # Default: invoke claude -p reading from stdin
        self.command = command or ["claude", "-p", "/dev/stdin"]
        self.timeout = float(timeout) if timeout else None

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
            "SWARMBUS_HOPS": str(msg.hops),
            "SWARMBUS_ORIGIN": msg.origin or msg.from_agent,
        }
        if msg.reply_to:
            env["SWARMBUS_REPLY_TO"] = msg.reply_to

        loop = asyncio.get_running_loop()
        returncode = await loop.run_in_executor(
            None, self._run, msg.body.encode("utf-8"), env,
        )
        if returncode is None:
            logger.warning(
                "DirectInvocationHandler: command %s timed out after %ss for message %s; killed",
                self.command, self.timeout, sanitize_field(msg.id),
            )
        elif returncode != 0:
            logger.warning(
                "DirectInvocationHandler: command %s exited %d for message %s",
                self.command, returncode, sanitize_field(msg.id),
            )

    def _run(self, body: bytes, env: Dict[str, str]) -> Optional[int]:
        """Run the command to completion.

        Returns its exit status, or ``None`` when it was killed on timeout.
        """
        proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            env=env,
            shell=False,            # NEVER shell=True — prevents injection
            start_new_session=True,  # own process group, so a timeout kills children too
        )
        try:
            proc.communicate(input=body, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=10)  # reap; a dead reader just closes the pipe
            except subprocess.TimeoutExpired:  # pragma: no cover - a stray grandchild holds stdin
                pass
            return None
        return proc.returncode
