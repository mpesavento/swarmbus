#!/usr/bin/env python3
"""
examples/planner_coder_local.py

Two agents communicating on the same machine.
Run in two terminals:
  Terminal 1: python planner_coder_local.py coder
  Terminal 2: python planner_coder_local.py planner
"""
import asyncio
import sys
from swarmbus import AgentBus, FileBridgeHandler, PersistentListenerHandler
from swarmbus.handlers.base import BaseHandler
from swarmbus.message import AgentMessage


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

    await bus.send(
        to=peer_id,
        subject="code sample",
        body="Here's a snippet:\n```python\nprint('hello from swarmbus')\n```",
        content_type="text/markdown",
    )

    await listen_task  # blocks until Ctrl+C


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("planner", "coder"):
        print("Usage: python planner_coder_local.py <planner|coder>")
        sys.exit(1)

    agent = sys.argv[1]
    peer = "coder" if agent == "planner" else "planner"

    try:
        asyncio.run(run_agent(agent, peer))
    except KeyboardInterrupt:
        print(f"\n[{agent}] shutting down")
