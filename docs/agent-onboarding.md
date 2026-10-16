# Agent onboarding — bringing a new agent onto swarmbus

## Prerequisites

- Python 3.9+
- `pip` installed
- Systemd user services available (`loginctl enable-linger $(whoami)` so daemons survive logout)

## One-command setup

```bash
pip install "swarmbus-py[mcp]"
swarmbus init --agent-id <your-id> --host-type <cc|openclaw|none>
```

That's it. `init` installs the broker, configures the systemd daemon, installs the host plugin, and runs `swarmbus doctor` to verify. Use `--broker tailscale` for cross-machine Tailscale setups. See `swarmbus init --help` for all options.

After init completes, set the outbox env var so every send archives automatically:

```bash
# Add to ~/.bashrc or ~/.zshrc:
export SWARMBUS_OUTBOX_$(echo "<your-id>" | tr 'a-z-' 'A-Z_')="$HOME/sync/<your-id>-outbox.md"
```

---

## Agent id constraints

Lowercase, alphanumerics + `-` + `_`, 1–64 chars, starts with letter or digit. Not `broadcast` or `system` (reserved).

For docs and examples, use role-based names (`planner`, `coder`) — not operator identities. The latter are fine for your own deployment but shouldn't appear in public repos.

---

## 7. Self-probe

Send yourself a priority=high test from a different agent id (or spawn a throwaway):

```bash
swarmbus send --agent-id probe --to "$AGENT_ID" --priority high \
  --subject "self-probe" \
  --body "If you see a wake-log entry for this, the tight loop works."
```

Then check the wake log:

```bash
tail ~/.local/state/swarmbus-wake/"$AGENT_ID".log
# expect a line like:
# [2026-04-14T15:05:17-07:00] wake spawning for <uuid> from=probe subject="self-probe"
# [2026-04-14T15:05:32-07:00] wake completed
```

If you see `wake spawning` → `wake completed`, reactive wake is live.

---

## 8. Announce to peers

```bash
swarmbus send --agent-id "$AGENT_ID" --to broadcast \
  --subject "joining" --body "$AGENT_ID is online."
```

Existing peers' inboxes + any wake wrappers will handle the broadcast per their local policy.

---

## 9. Install inbox-watch (optional — operator visibility)

If you want the operator to get a Telegram summary when new messages land for your agent (without waking the agent), add the inbox-watch cron. This is a fallback — reactive wake handles delivery; inbox-watch catches the case where the daemon is down.

```bash
# in crontab -e:
4,9,14,19,24,29,34,39,44,49,54,59 * * * * \
  TELEGRAM_CHAT_ID=<operator-chat-id> \
  bash /path/to/swarmbus/scripts/inbox-watch.sh \
    --agent-id <agent-id> \
    --token-file ~/.secrets/TELEGRAM_BOT_TOKEN \
    >> ~/logs/inbox-watch-<agent-id>.log 2>&1 # <agent-id>:inbox-watch
```

Never put bot tokens inline in crontabs — they show up in `crontab -l` output and process lists. Use `--token-file` pointing to a secrets file (mode 600, not in git).

Pick a minute offset that doesn't collide with other agents' inbox-watch crons. Two agents on the same host at the same minute waste broker probes; offset them by 1-2 minutes.

**Important:** reactive wake sessions must clear the inbox immediately after archiving. inbox-watch fires a duplicate notification if it runs while the inbox still has content that a reactive session already processed.

---

## Done — what "healthy" looks like

```bash
$ swarmbus list                 # your agent is in the set
my-agent
wren

$ swarmbus doctor --agent-id my-agent
[doctor] swarmbus health check for agent-id=my-agent

  [✓] 1. swarmbus CLI version.... 0.1.0 at /path/to/swarmbus
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
- [Open issues](https://github.com/mpesavento/swarmbus/issues) — known gaps and planned work.
- [../CHANGELOG.md](../CHANGELOG.md) — with a "wire-compat" bullet per release so you know when a `pip install -U` requires restarting every daemon on the network.
