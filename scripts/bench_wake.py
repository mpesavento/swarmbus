#!/usr/bin/env python3
"""Bench wake-script latency: gateway-bridge default vs CLI fallback.

Measures the time spent in `openclaw-wake.sh` *before* the receiving
agent starts producing output. Both wake paths (default bridge and
`OPENCLAW_WAKE_USE_CLI=1` fallback) are invoked against an agent id
that does not exist in the OpenClaw config — the gateway/CLI rejects
the call early, so we measure bootstrap + handshake + dispatch
overhead without spending tokens or waking a real agent.

Usage:
    python scripts/bench_wake.py [--runs N] [--bogus-agent ID]

Prereqs:
    - OpenClaw gateway daemon running (~/.openclaw/openclaw.json present).
    - examples/openclaw-wake.sh + examples/openclaw-bridge.mjs present.
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run_once(script: Path, agent_id: str, *, use_cli: bool = False,
             timeout_s: int = 30) -> float:
    """Invoke the wake script with a small body; return elapsed seconds.

    Exit codes are ignored — the bench measures wall time regardless
    of whether the wake path accepts or rejects the (deliberately bogus)
    agent id.
    """
    env = os.environ.copy()
    env.update({
        "SWARMBUS_FROM": "bench",
        "SWARMBUS_TO": "bench-target",
        "SWARMBUS_ID": f"bench-{int(time.time() * 1000)}",
        "SWARMBUS_SUBJECT": "bench",
        "SWARMBUS_CONTENT_TYPE": "text/plain",
        "SWARMBUS_PRIORITY": "normal",
        "SWARMBUS_TS": str(int(time.time())),
        "SWARMBUS_REPLY_TO": "",
        "OPENCLAW_BRIDGE_TIMEOUT_MS": str(timeout_s * 1000),
    })
    if use_cli:
        env["OPENCLAW_WAKE_USE_CLI"] = "1"
    t0 = time.perf_counter()
    subprocess.run(
        [str(script), agent_id],
        input=b"bench probe\n",
        env=env,
        timeout=timeout_s,
        check=False,
        capture_output=True,
    )
    return time.perf_counter() - t0


def summarise(label: str, samples: list[float]) -> None:
    mean = statistics.mean(samples)
    median = statistics.median(samples)
    lo, hi = min(samples), max(samples)
    print(
        f"{label:>16}  n={len(samples)}  "
        f"mean={mean*1000:7.0f}ms  med={median*1000:7.0f}ms  "
        f"min={lo*1000:7.0f}ms  max={hi*1000:7.0f}ms"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=3,
                   help="samples per wake path (default 3)")
    p.add_argument("--bogus-agent", default="bench-nonexistent-agent",
                   help="agent id that will be rejected by both paths")
    args = p.parse_args()

    root = repo_root()
    wake_script = root / "examples" / "openclaw-wake.sh"
    bridge_helper = root / "examples" / "openclaw-bridge.mjs"
    for s in (wake_script, bridge_helper):
        if not s.exists():
            print(f"missing wake artifact: {s}", file=sys.stderr)
            return 2

    print(f"bench: bogus agent='{args.bogus_agent}', runs={args.runs}\n")

    print("warmup (1 run each, discarded)")
    run_once(wake_script, args.bogus_agent, use_cli=True)
    run_once(wake_script, args.bogus_agent, use_cli=False)
    print()

    cli_samples = [run_once(wake_script, args.bogus_agent, use_cli=True)
                   for _ in range(args.runs)]
    bridge_samples = [run_once(wake_script, args.bogus_agent, use_cli=False)
                      for _ in range(args.runs)]

    summarise("CLI fallback", cli_samples)
    summarise("bridge (WS)", bridge_samples)

    cli_med = statistics.median(cli_samples)
    bridge_med = statistics.median(bridge_samples)
    if bridge_med > 0:
        speedup = cli_med / bridge_med
        savings_ms = (cli_med - bridge_med) * 1000
        print(f"\nspeedup: {speedup:.1f}x  (saves ~{savings_ms:.0f}ms per wake)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
