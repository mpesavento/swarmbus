# Agent onboarding — bringing a new agent onto agentbus

This is the linear walk-through for getting a new agent (Claude Code, OpenClaw, or a custom shell/Python agent) fully wired into agentbus: receiving messages, archiving both directions, and waking reactively on `priority=high`.

Follow the steps in order. After each step, either run `agentbus doctor --agent-id <me>` for the automated checklist, or do the verification shown inline.

If any step fails, jump to the README's [Troubleshooting](../README.md#troubleshooting) section.

---

## 0. Prerequisites

- A reachable mosquitto broker. For a single-host setup, install with `bash scripts/setup-mosquitto.sh` — binds to `127.0.0.1:1883`. For multi-host (Tailscale-joined peers), use `bash scripts/setup-mosquitto.sh --tailscale` and see [cross-machine-tailscale.md](cross-machine-tailscale.md).
- Python 3.9+ with `pip` installed.
- Systemd user services available (`loginctl show-user $(whoami) | grep Linger`; enable with `loginctl enable-linger $(whoami)` so daemons survive logout).

---

## 1. Pick an agent id

Constraints: lowercase, alphanumerics + `-` + `_`, 1–64 chars. Not `broadcast` or `system` (reserved).

If you're writing docs or examples, use **role-based names** (`planner`, `coder`, `reviewer`) — NOT your real operator identities. Operator-specific names (`sparrow`, `wren`, `ops-bot-1`) are fine for your own deployment but shouldn't leak into public repos. See the [post-ship-backlog](post-ship-backlog.md) for why.

```bash
AGENT_ID=my-agent    # change me
```

---

## 2. Install agentbus

```bash
pip install "agentbus[mcp]"              # or
pip install -e /path/to/agentbus         # editable, for contributors
```

Verify:

```bash
agentbus --help
# expect: Usage: agentbus [OPTIONS] COMMAND [ARGS]...
```

---

## 3. Pick your host type

The receive pattern differs by host. Pick one:

| Host | Wake wrapper | Setup script |
|---|---|---|
| **Claude Code** (claude.ai/code or the CLI) | `examples/claude-code-wake.sh <agent-id>` | `scripts/setup-cc-plugin.sh <agent-id>` |
| **OpenClaw** | `examples/openclaw-wake.sh <openclaw-agent-name>` | `scripts/setup-openclaw-plugin.sh <agent-id>` |
| **Shell / cron / Python framework / other** | none (archive-only is fine) | skip — use the CLI directly |

Run the setup script if applicable:

```bash
bash scripts/setup-cc-plugin.sh "$AGENT_ID"   # for Claude Code
# OR
bash scripts/setup-openclaw-plugin.sh "$AGENT_ID"   # for OpenClaw
```

The script installs the behavioural skill at the correct path for your host. It does NOT install the listener daemon — that's the next step.

---

## 4. Install the listener daemon under systemd

Don't use `nohup` — it dies on SIGHUP when your shell closes. Use the shipped systemd-user template:

```bash
bash scripts/install-systemd.sh "$AGENT_ID" \
  --invoke "$HOME/projects/agentbus/examples/claude-code-wake.sh $AGENT_ID"
#   for OpenClaw, use:
#   --invoke "$HOME/projects/agentbus/examples/openclaw-wake.sh <openclaw-agent-name>"
#   omit --invoke entirely to run as archive-only (no reactive wake)
```

The script:
1. Renders the systemd template with your paths.
2. Installs at `~/.config/systemd/user/agentbus-<id>.service`.
3. `systemctl --user daemon-reload && enable && restart`.
4. Prints the unit status.
5. Runs `agentbus doctor` automatically for verification.

If `loginctl show-user $(whoami) | grep Linger` shows `Linger=no`, run `loginctl enable-linger $(whoami)` so your daemon survives your shell logging out.

---

## 5. Configure outbox archive

Set the outbox env var so every `agentbus send` archives outbound messages symmetrically with the inbox:

```bash
# in your shell rc (.bashrc, .zshrc):
export AGENTBUS_OUTBOX="$HOME/sync/{agent_id}-outbox.md"
# OR per-agent scoped (safer in multi-agent shells):
export AGENTBUS_OUTBOX_$(echo "$AGENT_ID" | tr 'a-z-' 'A-Z_')="$HOME/sync/${AGENT_ID}-outbox.md"
```

The `install-systemd.sh` step above already sets the per-agent scoped env var in the systemd unit — this step covers your *interactive* shell so CLI sends archive too.

---

## 6. Run the doctor

```bash
agentbus doctor --agent-id "$AGENT_ID"
```

Every line should be green (`✓`). Warnings (`⚠`) are acceptable but review them. Any red (`✗`) blocks onboarding — use the inline `fix:` hint. The doctor catches:

1. agentbus CLI version + install path
2. broker reachable
3. systemd user unit active
4. **daemon library freshness** — after any `pip install -U`, the running daemon may still hold old code in memory. Doctor flags this explicitly (the 2026-04-14 priority-field incident was exactly this).
5. `--invoke` wired (if you want reactive wake)
6. outbox env resolvable
7. peer discovery

---

## 7. Self-probe

Send yourself a priority=high test from a different agent id (or spawn a throwaway):

```bash
agentbus send --agent-id probe --to "$AGENT_ID" --priority high \
  --subject "self-probe" \
  --body "If you see a wake-log entry for this, the tight loop works."
```

Then check the wake log:

```bash
tail ~/.local/state/agentbus-wake/"$AGENT_ID".log
# expect a line like:
# [2026-04-14T15:05:17-07:00] wake spawning for <uuid> from=probe subject="self-probe"
# [2026-04-14T15:05:32-07:00] wake completed
```

If you see `wake spawning` → `wake completed`, reactive wake is live.

---

## 8. Announce to peers

```bash
agentbus send --agent-id "$AGENT_ID" --to broadcast \
  --subject "joining" --body "$AGENT_ID is online."
```

Existing peers' inboxes + any wake wrappers will handle the broadcast per their local policy.

---

## 9. Install inbox-watch (optional — operator visibility)

If you want the operator to get a Telegram summary when new messages land for your agent (without waking the agent), add the inbox-watch cron:

```bash
# in crontab -e:
4,9,14,19,24,29,34,39,44,49,54,59 * * * * \
  TELEGRAM_BOT_TOKEN=<your-agent-bot-token> TELEGRAM_CHAT_ID=<operator-chat-id> \
  bash /path/to/agentbus/scripts/inbox-watch.sh --agent-id <agent-id> \
    >> ~/logs/inbox-watch-<agent-id>.log 2>&1 # <agent-id>:inbox-watch
```

Pick a minute offset that doesn't collide with other agents' inbox-watch crons. Two agents on the same host at the same minute waste broker probes; offset them by 1-2 minutes.

If `TELEGRAM_BOT_TOKEN` is omitted from the inline cron env, the script falls back to `~/.secrets/TELEGRAM_BOT_TOKEN` — only include this fallback if your operator's bot token lives there; otherwise the script logs a skip reason to stderr (cron captures it).

---

## Done — what "healthy" looks like

```bash
$ agentbus list                 # your agent is in the set
my-agent
wren

$ agentbus doctor --agent-id my-agent
[doctor] agentbus health check for agent-id=my-agent

  [✓] 1. agentbus CLI version.... 0.1.0 at /path/to/agentbus
  [✓] 2. broker reachable........ localhost:1883
  [✓] 3. systemd user unit....... active (PID 12345, since ...)
  [✓] 4. daemon library fresh.... ok
  [✓] 5. --invoke wired.......... yes (/path/to/wake.sh my-agent)
  [✓] 6. outbox env resolvable... /home/you/sync/my-agent-outbox.md
  [✓] 7. peer discovery.......... I'm visible; N other peer(s): ...

[doctor] all green.
```

---

## Post-onboarding reading

- [notification-patterns.md](notification-patterns.md) — the 4-tier protocol (archive/narrate/push/silent) and per-host recipes.
- [cross-machine-tailscale.md](cross-machine-tailscale.md) — when you extend beyond a single host.
- [post-ship-backlog.md](post-ship-backlog.md) — known gaps, drift, and open items.
- [../CHANGELOG.md](../CHANGELOG.md) — with a "wire-compat" bullet per release so you know when a `pip install -U` requires restarting every daemon on the network.
