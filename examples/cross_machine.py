#!/usr/bin/env python3
"""
examples/cross_machine.py

Two agents on different machines, connected via a shared broker.

Usage:
  Machine A: BROKER=broker-host.your-tailnet.ts.net python cross_machine.py planner
  Machine B: BROKER=broker-host.your-tailnet.ts.net python cross_machine.py coder
"""
import asyncio
import os
import sys
from swarmbus import AgentBus, PersistentListenerHandler
from swarmbus.handlers.base import BaseHandler
from swarmbus.message import AgentMessage

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
    peer = "coder" if agent == "planner" else "planner"
    try:
        asyncio.run(run_agent(agent, peer))
    except KeyboardInterrupt:
        print(f"\n[{agent}] done")
