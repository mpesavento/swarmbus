"""Run a person's ``--on-trip`` command once per newly tripped sender.

The hold store reports each new trip once (``pop_trips``); a listener hands
it to a ``TripAlerter``, which runs the command in the background so the
receive loop never waits on it. The command gets sanitized
``SWARMBUS_TRIP_*`` variables, no stdin, and is killed after a timeout.
"""

import asyncio
import logging
import os
import subprocess
import threading

from .archive import TripEvent
from .handlers.direct_invoke import _kill_process_group
from .message import sanitize_field

logger = logging.getLogger(__name__)

# Seconds an alert command may run before it is killed.
TRIP_ALERT_TIMEOUT = 30.0
# Alerts that may wait for the worker; more are dropped (every trip is still
# recorded in the hold store and shown by `swarmbus held`).
MAX_PENDING_ALERTS = 64

_MAX_FIELD = 256

# Set by the wake handler for the message it delivers; an alert is not a wake.
_WAKE_VARS = (
    "SWARMBUS_FROM", "SWARMBUS_TO", "SWARMBUS_ID", "SWARMBUS_SUBJECT",
    "SWARMBUS_CONTENT_TYPE", "SWARMBUS_PRIORITY", "SWARMBUS_TS",
    "SWARMBUS_HOPS", "SWARMBUS_ORIGIN", "SWARMBUS_REPLY_TO",
)


def _clean(value: object) -> str:
    """One line, at most 256 characters, valid UTF-8."""
    return sanitize_field(value)[:_MAX_FIELD].encode("utf-8", "replace").decode("utf-8")


def trip_env(trip: TripEvent, *, agent_id: str, listener: str) -> dict[str, str]:
    """The alert command's environment: ours, minus wake variables, plus the trip."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in _WAKE_VARS and not k.startswith("SWARMBUS_TRIP_")
    }
    agent = _clean(agent_id)
    sender = _clean(trip.sender)
    kind = _clean(trip.kind)
    message_id = _clean(trip.message_id)
    until = _clean(trip.until)
    reset = _clean(f"swarmbus held --agent-id {agent} --reset {sender}")
    summary = (
        f"[swarmbus] {agent}: {kind.replace('-', ' ')} tripped by {sender} "
        f"(message {message_id}, hops {_clean(trip.hops)}); "
    )
    if trip.until is None:
        summary += f"held until a person runs: {reset}"
    else:
        summary += f"held until {until} (or until: {reset})"
    env.update({
        "SWARMBUS_TRIP_AGENT": agent,
        "SWARMBUS_TRIP_LISTENER": _clean(listener),
        "SWARMBUS_TRIP_KIND": kind,
        "SWARMBUS_TRIP_SENDER": sender,
        "SWARMBUS_TRIP_MESSAGE_ID": message_id,
        "SWARMBUS_TRIP_HOPS": _clean(trip.hops),
        "SWARMBUS_TRIP_ORIGIN": _clean(trip.origin),
        "SWARMBUS_TRIP_TOPIC": _clean(trip.source_topic),
        "SWARMBUS_TRIP_AT": _clean(trip.tripped_at),
        "SWARMBUS_TRIP_UNTIL": until,
        "SWARMBUS_TRIP_RESET": reset,
        "SWARMBUS_TRIP_SUMMARY": _clean(summary),
    })
    return env


class TripAlerter:
    """Runs the alert command for each submitted trip, one at a time, off the loop."""

    def __init__(
        self,
        command: list[str],
        *,
        agent_id: str,
        listener: str,
        timeout: float = TRIP_ALERT_TIMEOUT,
        max_pending: int = MAX_PENDING_ALERTS,
    ) -> None:
        self.command = list(command)
        self.agent_id = agent_id
        self.listener = listener
        self.timeout = float(timeout)
        self.max_pending = max_pending
        self.dropped = 0
        # Created on first submit: the CLI builds the alerter before asyncio.run.
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._current: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._closing = threading.Event()

    def submit(self, trip: TripEvent) -> None:
        """Queue one alert. Never blocks, never raises."""
        try:
            if self._queue is None:
                self._queue = asyncio.Queue(maxsize=self.max_pending)
                self._worker = asyncio.get_running_loop().create_task(self._work())
            try:
                self._queue.put_nowait(trip)
            except asyncio.QueueFull:
                self.dropped += 1
                logger.warning(
                    "trip alert queue full: dropped the alert for %s (%d dropped so far); "
                    "every trip is still recorded: swarmbus held --agent-id %s",
                    _clean(trip.sender), self.dropped, _clean(self.agent_id),
                )
        except Exception as exc:
            logger.warning("could not queue a trip alert: %s", _clean(exc))

    async def _work(self) -> None:
        queue = self._queue
        loop = asyncio.get_running_loop()
        while True:
            trip = await queue.get()
            try:
                if not self._closing.is_set():
                    await loop.run_in_executor(None, self._run, trip)
            except Exception as exc:
                logger.warning("trip alert failed: %s", _clean(exc))
            finally:
                queue.task_done()

    def _run(self, trip: TripEvent) -> None:
        with self._lock:
            if self._closing.is_set():
                return
            try:
                proc = subprocess.Popen(
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=2,  # the alert's stdout goes to our stderr, never into MCP stdio or --json output
                    env=trip_env(trip, agent_id=self.agent_id, listener=self.listener),
                    shell=False,
                    start_new_session=True,
                )
            except Exception as exc:
                logger.warning("trip alert %s could not start: %s", self.command, _clean(exc))
                return
            self._current = proc
        try:
            try:
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                proc.wait()
                logger.warning(
                    "trip alert %s timed out after %gs (sender %s)",
                    self.command, self.timeout, _clean(trip.sender),
                )
                return
            if proc.returncode != 0:
                logger.warning(
                    "trip alert %s exited %s (sender %s)",
                    self.command, proc.returncode, _clean(trip.sender),
                )
        finally:
            with self._lock:
                self._current = None

    async def aclose(self) -> None:
        """Let queued alerts finish (at most timeout + 5 s), then stop the worker."""
        queue, worker = self._queue, self._worker
        if queue is None or worker is None:
            return
        try:
            try:
                await asyncio.wait_for(queue.join(), self.timeout + 5)
            except asyncio.TimeoutError:
                with self._lock:
                    self._closing.set()
                    running = self._current
                if running is not None:
                    _kill_process_group(running)
                unsent = queue.qsize() + (1 if running is not None else 0)
                await asyncio.wait({worker}, timeout=2)
                logger.warning(
                    "trip alerts still running at shutdown: %d alert(s) not sent", unsent,
                )
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            self._queue = None
            self._worker = None
            self._closing.clear()
