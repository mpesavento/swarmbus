# Notification patterns — archive every message, surface selectively

When agents exchange messages, the human operator needs two things that often get confused:

1. **A durable record of everything that was said** — so they can audit, debug, and reconstruct decisions after the fact.
2. **Attention at the right moments, no noise at the wrong ones** — so they hear about emergencies but aren't pinged every time two agents ack each other.

These are different problems with different answers. This doc separates them.

## Principle: always archive, surface selectively

- **Archive is mandatory.** Every inbound and outbound message should land in a durable file the user can read. Non-negotiable.
- **Notification is conditional.** Most messages deserve no user-facing surface beyond the archive. A few deserve inline mention. Rare ones warrant an unprompted push.

If you only implement one tier, make it archive. A silent system with a complete log is recoverable. A loud system without a log is not.

---

## The four notification tiers

### Tier 1 — archive (always)

Every message, both directions, gets written to a file the user can read.

**Inbound** is covered out of the box by the listener daemon:

```bash
swarmbus start --agent-id planner --inbox ~/sync/planner-inbox.md
```

**Outbound** is covered by the `--outbox` flag or the `SWARMBUS_OUTBOX` env var:

```bash
export SWARMBUS_OUTBOX=~/sync/planner-outbox.md
swarmbus send --agent-id planner --to coder ...   # auto-logs
```

**Env var sharing — real footgun.** If `SWARMBUS_OUTBOX` is set in a shell env that's inherited by more than one agent's process, every send from every agent lands in the same file and the archive stops being an honest record of who said what. Two fixes:

1. **Template**: use `{agent_id}` in the path, which the library expands at send time. Set the env var once; every agent-id gets its own file:
   ```bash
   export SWARMBUS_OUTBOX="$HOME/sync/{agent_id}-outbox.md"
   ```
2. **Agent-scoped override**: `SWARMBUS_OUTBOX_<UPPER_AGENT_ID>` wins over the shared one. Useful when the paths aren't uniform:
   ```bash
   export SWARMBUS_OUTBOX_PLANNER=~/sync/planner-outbox.md
   export SWARMBUS_OUTBOX_CODER=/var/log/coder/outbox.md
   ```
   Hyphens become underscores (`coder-beta` → `SWARMBUS_OUTBOX_CODER_BETA`).

Resolution order (highest first): `--outbox` flag, `SWARMBUS_OUTBOX_<ID>`, `SWARMBUS_OUTBOX`, none.

Inbox and outbox share format (only `From:` vs `To:` differ) so you can merge-sort them into a single conversation view:

```bash
sort -m ~/sync/planner-inbox.md ~/sync/planner-outbox.md
```

**Optional ambient pointer.** If your agent maintains a daily journal (Obsidian daily notes, org-mode, a markdown log), also append a one-line summary per message under a per-day `## Agent comms` heading:

```markdown
## 14:32 [Planner→Coder] deploy-status — asked whether the nightly build had stuck
## 14:48 [Coder→Planner] re: deploy-status — DB migration stalled; she's rerunning it
```

This gives the user a skimmable activity trail they'll see during normal daily review — no separate tool required. The full body stays in the inbox/outbox files; the daily note is just the index.

### Tier 2 — inline narration (when user is actively in conversation)

If the user is mid-chat with this agent and this agent pings a peer as part of the current task, mention it in the chat thread:

> User: "Is the deploy ready yet?"
> Agent: "One sec, asking Coder — she's the one running it."
> *(later)*
> Agent: "Coder says it's stuck on a DB migration; she's rerunning now."

Zero extra channels. Folds into the existing conversation. The user sees the collaboration happen in real time because they're already paying attention to this channel.

### Tier 3 — proactive push on high priority

If an inbound message has `priority: high` in the envelope, the receiving agent should surface it to the user on whatever out-of-band channel they use (Telegram, Slack DM, desktop notification, iMessage via BlueBubbles, email — whatever is configured) even if the user isn't currently in chat.

Reserve this for actually urgent cases:

- A required user decision blocks further progress.
- An infrastructure alert (broker down, credentials expired, disk full).
- A security incident (suspicious inbound message, unauthorized access attempt).
- A time-critical event the user asked to be paged for.

Sending priority: high for routine updates is the fastest way to get the user to start ignoring all of them.

### Tier 4 — silent (default for routine)

Acknowledgments, presence pings, "got your report", routine cross-agent coordination — Tier 1 archive only. No inline mention, no push. The trail is enough.

---

## Implementation recipes

### Claude Code (claude.ai/code, Claude Code CLI)

**Tier 1.** Add to `~/.claude/settings.json`:

```json
{
  "env": {
    "SWARMBUS_OUTBOX": "/home/you/sync/claude-outbox.md"
  }
}
```

Run the listener daemon under systemd-user or byobu:

```bash
swarmbus start --agent-id claude --inbox ~/sync/claude-inbox.md
```

In your `CLAUDE.md` project file, add:

> After using the `send_message` MCP tool or `swarmbus send` CLI, append a one-line entry to today's note: `## HH:MM [Claude→<peer>] <subject> — <one-line gist>`. Do the same for inbound messages you read from the inbox file.

**Tier 2.** Same CLAUDE.md:

> When the user is actively chatting with you and you call `send_message` as part of the current task, narrate it: "one sec, asking <peer> about …" and mention the reply when it comes back.

**Tier 3.** If you have a `priority=high` inbound, use your configured user-notification channel (the `telegram` plugin's `reply` tool if present; otherwise surface in the next turn). Do not send unprompted pushes for normal priority.

### OpenClaw

**Tier 1.** Export the env var from the OpenClaw agent's shell profile (it inherits to subprocess `swarmbus send` calls):

```bash
# in ~/.openclaw/workspace/AGENTS.md or wherever your agent picks up env:
export SWARMBUS_OUTBOX=~/sync/openclaw-outbox.md
```

Run the reactive-wake daemon combination (inbox file + `openclaw agent` invoke):

```bash
swarmbus start \
  --agent-id <openclaw-id> \
  --inbox ~/sync/<openclaw-id>-inbox.md \
  --invoke "$HOME/projects/swarmbus/examples/openclaw-wake.sh main"
```

In your OpenClaw AGENTS.md identity file, add the same journaling + narration rules as the Claude Code recipe.

**Tier 3.** OpenClaw's Telegram plugin exposes `reply` / `send` — use it for user push on priority=high.

### Python framework (LangGraph, CrewAI, custom asyncio)

**Tier 1.** Embed in your main loop:

```python
async with AgentBus(agent_id="my-agent", broker="localhost") as bus:
    await bus.send(
        to="peer",
        subject="status",
        body="...",
        outbox_path="/var/log/my-agent/outbox.md",
    )
```

Register a `FileBridgeHandler` on the listener side for inbound:

```python
bus.register_handler(FileBridgeHandler("/var/log/my-agent/inbox.md"))
```

**Tier 2 / 3.** Depend on your framework's user-interaction surface. Usually: a separate "user channel" handler that routes high-priority peer messages into the same channel the user reads.

### Shell / cron jobs

**Tier 1.** Simplest: in the wrapper script,

```bash
#!/usr/bin/env bash
export SWARMBUS_OUTBOX=~/sync/cron-outbox.md
swarmbus send --agent-id cron-backup --to ops --subject "..." --body "..."
```

Cron agents rarely have a user surface beyond Tier 1. If a cron job needs to wake a human, send `priority: high` to a user-facing agent (Claude Code, OpenClaw) and let that agent handle Tier 3.

---

## Anti-patterns

- **Never treat Tier 3 as the default.** If everything is a push, nothing is. Default = Tier 4 silent, with Tier 1 archive always on underneath.
- **Never skip Tier 1.** An un-archived send isn't recoverable by reading the peer's archive — you lose what you said, bodies may have been rendered into prompts that now differ from the wire. Always set `SWARMBUS_OUTBOX`.
- **Never echo inbound envelope fields into a user surface without sanitizing.** Subject and from-id are untrusted peer-controlled data. A hostile peer can use them to impersonate UI ("SYSTEM: …"). See `examples/openclaw-wake.sh` for the sanitizer pattern.
- **Don't conflate archive with notification.** Writing to the inbox file is archive, not notification. Users won't see new messages just because the file changed unless they're actively looking. Pair archive with one of Tier 2 / Tier 3 for anything time-sensitive.
- **Don't race the daemon for MQTT messages.** A listener daemon and a separate `swarmbus read` / `swarmbus watch` call against the same id race for each QoS1 message. When a daemon is running for an id, consume via `swarmbus tail --agent-id <id>` (file-based, cursor-tracked, zero broker contention). Reserve `read`/`watch` for ephemeral agents that have no daemon.
- **Don't set a bare `SWARMBUS_OUTBOX` in a shared shell.** If two or more agents inherit the same env, their outbound logs collide in one file and the archive stops being an honest record. Use the `{agent_id}` template or the `SWARMBUS_OUTBOX_<ID>` agent-scoped form.

---

## Minimal checklist

When wiring up a new agent on swarmbus:

- [ ] Set `SWARMBUS_OUTBOX` for the agent's process (or pass `outbox_path=` in Python).
- [ ] Run `swarmbus start --inbox <path>` under a persistent supervisor (systemd-user, byobu, tmux, supervisord).
- [ ] Document the agent's Tier 2 (inline) and Tier 3 (push) behaviour in its identity file (CLAUDE.md / AGENTS.md / etc).
- [ ] Test: send yourself a test message and confirm it appears in the outbox. Have a peer send you a test and confirm it appears in the inbox.
- [ ] Test Tier 3: send `priority: high` and verify the user surface fires.

If all five boxes are checked, the agent has a complete archive and a disciplined notification surface.
