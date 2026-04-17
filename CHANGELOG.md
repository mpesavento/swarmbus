# Changelog

All notable changes to swarmbus. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/) once 1.0 lands.

## Wire-compat discipline

Every release section below ends with an explicit **Wire-compat** bullet
covering four questions:

1. **Envelope shape** — did any field type/name change in `AgentMessage`?
2. **Topic layout** — did the MQTT topic paths (`agents/<id>/inbox`, `agents/<id>/presence`, `agents/broadcast`) change?
3. **Retain/QoS defaults** — did any publish/subscribe default shift?
4. **MCP tool contract** — did the MCP tool names or parameter shapes change?

If **any** of the above is "yes", the bullet spells out the mitigation a running fleet needs (restart order, aliases, compat flags). A change that silently breaks older daemons is always a wire-compat issue — the priority-field regression on 2026-04-14 was caught late because this discipline didn't exist yet. Never again.

---

## [Unreleased] — 2026-04-14

First-day-in-production iteration. Sparrow + Wren deployed on an RPi, broker reachable over loopback for now, Tailscale cross-host documented but not yet exercised in anger.

### Added
- `swarmbus read` / `watch` / `list` / `tail` CLI subcommands (CLI now at full parity with the MCP tool surface, plus a file-tailer that works without racing a running daemon). Cursor-aware `tail` with inode-change detection for rotation safety.
- `--priority {low,normal,high}` flag on `swarmbus send`. The envelope field always existed; the CLI never exposed it before.
- `--reply-to` flag on `swarmbus send` for threading.
- `--outbox <path>` on send + `SWARMBUS_OUTBOX` / `SWARMBUS_OUTBOX_<ID>` env vars (with `{agent_id}` templating + agent-scoped overrides) so outbound messages archive symmetrically with the inbox.
- `--persistent / --no-persistent` on `swarmbus start` — default on. Uses MQTT persistent sessions (stable client-id + `clean_session=False`) so queued QoS1 messages survive daemon restarts.
- `AgentBus.probe()` classmethod for broker-only operations that don't need a registered identity (replaces an earlier magical `agent_id="_probe"` pattern).
- `examples/claude-code-wake.sh` — reactive wake wrapper for Claude Code peers. Priority-gated (default `high`-only), envelope sanitization, logs to `~/.local/state/swarmbus-wake/`.
- `examples/openclaw-wake.sh` — same for OpenClaw peers. Includes envelope sanitizer (strips control chars, caps length, labels `[UNTRUSTED PEER METADATA]`) so hostile subjects can't smuggle prompt injection.
- `scripts/inbox-watch.sh` — cron-driven Telegram summariser for new inbox entries (cursor-tracked, inode-aware, zero-dep beyond `curl`).
- `scripts/setup-mosquitto.sh` gained `--tailscale` and `--tailscale-only` modes — writes `/etc/mosquitto/conf.d/tailscale.conf` binding a listener to the host's Tailscale IP.
- `scripts/setup-openclaw-plugin.sh` — install the `using-swarmbus` skill into `~/.openclaw/skills/` and print byobu/systemd-user startup hints.
- `docs/cross-machine-tailscale.md` — full walkthrough for Tailscale-based multi-host deployments (topology, verification, security model, failure modes, 7-item preflight).
- `docs/notification-patterns.md` — 4-tier notification protocol (archive always, narrate when mid-chat, push on priority=high, silent otherwise) with per-agent-system recipes.
- `docs/post-ship-backlog.md` — living ledger of gaps / drift / follow-ups found after initial deployment.

### Changed
- **[breaking for pre-this-release daemons]** `AgentMessage.priority` is now a bare `str`, not `Literal["normal", "urgent"]`. Unknown values pass through rather than raising a pydantic `ValidationError`. See Wire-compat below.
- `mcp_server.py` is now a thin delegation layer — `read_inbox` / `watch_inbox` / `list_agents` live on `AgentBus` where the CLI and MCP share one implementation. MCP tools wrap and swallow `MqttError` to preserve their graceful-empty contract; CLI propagates and exits 2 for visibility.
- Daemon invocation: the recommended pattern is now a systemd user unit with linger enabled, not `nohup`-in-a-shell. Two sessions' worth of zombie-daemon incidents taught us why.

### Fixed
- Silent message discard under rolling upgrade. A daemon running the old `Literal["normal", "urgent"]` priority enum would reject (as `from_json` ValidationError → log warning → drop) any QoS1 message whose sender emitted a newer priority value. This is the reason 2026-04-14's `priority=high` round-trip tests between Sparrow and Wren appeared "delivered" on the sender side but "never received" on the receiver side. Root cause: the envelope's tight `Literal` made any vocabulary change a fleet-stop-the-world restart event. Mitigation shipped in this cycle (permissive `str`) + wire-compat discipline added to this CHANGELOG.
- `swarmbus send` now catches `MqttError` and exits 2 with a clean stderr message instead of dumping a 40-line traceback.
- `--invoke` now parses its argument via `shlex.split`, so `--invoke "bash -c 'echo $X'"` survives quoting.
- `swarmbus read`/`watch`/`list` propagate `MqttError` (translated to exit 2 at the CLI) instead of returning empty/None — the old behaviour was indistinguishable from "broker up, nothing to return".
- `swarmbus tail` cursor atomicity: write-to-temp + `os.replace` so SIGKILL can't leave an empty cursor. Plus inode-change detection so file rotation (logrotate, mv-in-replacement) triggers a re-read instead of silent mid-file seeks.
- `--consumer` on `tail` rejects path-traversal shapes (`../escape`, `foo/bar`).
- Outbox `SWARMBUS_OUTBOX` leaks into multi-agent shells: added `{agent_id}` template substitution + agent-scoped `SWARMBUS_OUTBOX_<UPPER_ID>` override with documented resolution precedence.
- Dropped `text/x-code;lang=python` from the `content_type` vocabulary — it was an affordance ("code for you to read as source") that invited the exact wrong thing given the "inbound bodies are untrusted" security posture. `text/markdown` with fenced blocks is now the recommended shape for sharing code between agents.

### Security
- Envelope metadata (`subject`, `from`, `reply_to`) is treated as untrusted in the shipped wake wrappers: control chars stripped, newlines collapsed, length capped, rendered into prompts under explicit `[UNTRUSTED PEER METADATA]` labels. Extended SKILL.md's "bodies are untrusted" rule to envelope fields.
- README no longer hard-codes the operator's real Tailscale hostname; uses `broker-host.your-tailnet.ts.net` placeholder throughout docs + examples.
- Example agent-ids swept from operator-specific (`sparrow` / `wren`) to role-based generic (`planner` / `coder`) across public docs + examples. Internal test fixtures unchanged.

### Wire-compat
1. **Envelope shape — YES (breaking for pre-this-release daemons).**
   `AgentMessage.priority` changed from `Literal["normal", "urgent"]` to `str`. Mitigation: every running daemon must restart to pick up the permissive field. Daemons left on the old literal will silently drop messages whose priority value isn't exactly `"normal"` or `"urgent"` — including `"high"`, which is the gate for the reactive wake wrappers. A newly-sent `priority="high"` message reaching an un-restarted daemon will log `ValidationError` and discard the message. Restart order does not matter; no data migration needed; each daemon picks up the fix the moment it's restarted.
2. **Topic layout — no change.**
3. **Retain/QoS defaults — no change.** Inbox/broadcast stays `retain=False, qos=1`; presence stays `retain=True, qos=1`.
4. **MCP tool contract — no change.** Names, parameters, and return types are stable; signature assertions added in `tests/test_integration.py` to prevent silent drift.

---

## [v0.1.0] — 2026-04-14 (initial)

First functional release as described in `docs/superpowers/specs/2026-04-14-swarmbus-design.md` and `docs/superpowers/plans/2026-04-14-swarmbus-build.md`. Peer-symmetric pub/sub over mosquitto, no orchestrator, `send` / `listen` / `mcp-server` / `start` CLI, `FileBridgeHandler` / `DirectInvocationHandler` / `PersistentListenerHandler` / `SQLiteArchive` handlers, MCP sidecar with 4 tools.

### Wire-compat
Baseline release — nothing to be compatible with.
