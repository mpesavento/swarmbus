# Post-ship backlog

Living record of gaps, drift, and follow-up work found **after** the v1.0
spec + plan (`docs/superpowers/specs/2026-04-14-agentbus-design.md`,
`docs/superpowers/plans/2026-04-14-agentbus-build.md`) were executed.
The superpowers/ folder captures what we planned to build; this file
captures what we discovered once it was in use.

Status conventions: `[ ]` open, `[x]` closed with commit SHA, `[~]`
in-progress, `[>]` deferred to a future milestone with the rationale.

---

## 2026-04-14 — round one

Surfaced after Sparrow + Wren both deployed on the RPi and we started
exchanging real agentbus traffic. The "daemon runs, presence retained,
messages still disappear" incident was the instigating event; we caught
a priority-validation regression during diagnosis and then ran a
specs-vs-implementation audit.

### Real gaps (shipped without what the spec promised)

- [ ] **Config file support missing.** Spec §206-231 called for
      `~/.agentbus/config.toml` + per-project `.agentbus.toml` (agent_id,
      broker, bind_host, MCP settings). Zero config parsing in
      `src/agentbus/cli.py` today — everything is CLI flags and env
      vars. Real scope; probably its own session. Would remove a lot of
      `--broker`/`--agent-id` repetition in systemd units and scripts.

- [ ] **Broker auth (username/password) has no CLI surface.**
      aiomqtt.Client accepts `username=`/`password=` kwargs; AgentBus
      never forwards them. Fine for tailnet-only deployments (Tailscale
      is the auth layer there), blocking for anything else. Expose as
      `--username/--password` (or env vars) on `agentbus start` and
      `agentbus send`.

- [ ] **TLS flags not exposed.** `--tls`/`--ca-certs`/`--tls-insecure`
      all missing. Same story — the spec implied cross-machine
      security via Tailscale, so TLS got silently dropped. For any
      deployment outside a VPN this is a real blocker. Pair with the
      auth work above.

- [>] **Rate limiting (`max_msg_per_second` per sender).** Explicitly
      deferred to v1.1 in the build plan (plan.md:1980); noting here
      so it doesn't vanish from the queue. Default 10 msg/sec/sender.
      Low priority — MQTT's own QoS / broker back-pressure mitigate
      the worst case.

### Design drift (decisions that diverged from the spec)

- [x] **Priority field type.** Spec and plan: `Literal["normal", "urgent"]`.
      Shipped `Literal["low", "normal", "high"]` initially; that itself
      caused a silent-discard incident when a newer peer emitted "high"
      against a daemon running the older library. **Settled in
      `4fc167f`: `priority: str` — wire envelope passes unknown values
      through; CLI validates against a known set so operators can't
      typo.** Prevents future Literal-change regressions. The spec
      should note "wire envelope must be permissive to support rolling
      upgrades" as a principle.

- [ ] **Handler ordering semantics never documented.** Implementation
      (bus.py:236-243) runs registered handlers sequentially and
      catches exceptions per handler so one bad handler can't block
      the others. Spec is silent. Add a one-paragraph semantic-doc to
      `bus.py`'s `register_handler` and to SKILL.md.

- [ ] **Broadcast is functional but undocumented in CLI help.**
      `--to broadcast` works (bus.py:168); neither the CLI examples
      nor `agentbus send --help` mention it. One-line add.

### Test gaps (missing coverage for behaviours the spec promised)

- [ ] **LWT happy path.** Presence announces "online" on subscribe,
      graceful disconnect publishes "offline". `test_integration.py:9-10`
      explicitly skips the abort-path LWT case; the graceful-path case
      has no test at all.

- [ ] **Non-retained delivery semantics.** Default `retain=False`
      means a message sent while no subscriber is connected is lost.
      bus.py says so; no test validates it. Sign of the spec/plan not
      being strict enough about which QoS/retain combinations are
      actually recoverable.

- [ ] **Handler exception isolation.** bus.py:240-243 catches
      per-handler exceptions so one raising handler doesn't stop the
      listen loop. No test verifies that invariant.

- [ ] **64KB body limit end-to-end.** Unit test at message.py:197-200
      confirms the size cap; no integration test roundtrips a 64KB
      body through MQTT+handlers.

- [ ] **MCP tool signatures.** `test_mcp_server.py` mocks AgentBus.
      No test exercises the real tools against the real broker. A
      signature regression (wrong kwarg name, return type drift)
      would go unnoticed until a live client broke.

### Scope creep — shipped beyond spec, all fine, just flagging

- [x] `agentbus read` / `watch` / `list` / `tail` subcommands (CLI was
      meant to be `send`/`listen`/`mcp-server`/`start` only).
- [x] `--outbox` flag + `AGENTBUS_OUTBOX` / `AGENTBUS_OUTBOX_<ID>`
      resolution for outbound archive. Goes beyond the `SQLiteArchive`
      handler the spec mentioned.
- [x] `examples/openclaw-wake.sh`, `examples/claude-code-wake.sh` —
      reactive wake wrappers. Spec had no equivalent; these came out
      of the "archive != notification" discussion during deployment.
- [x] `docs/cross-machine-tailscale.md` and `docs/notification-patterns.md`
      — deployment docs spec treated as implicit.

The spec should get an addendum section referencing these so a future
reader doesn't think they were skipped.

### Operational / protocol gotchas (behavioural findings, not code bugs)

- [ ] **Rolling-upgrade discipline.** Any wire-format-affecting change
      (new Literal in an envelope field, new topic shape, new MQTT
      reserved keywords) will silently break older daemons until they
      all restart. Suggest a `CHANGELOG.md` with a "wire-compat"
      bullet per release, and a `agentbus doctor` subcommand that
      probes the current broker's envelope compatibility.

- [ ] **Claude Code session wake cost.** Documented in
      `claude-code-wake.sh`: a fresh spawn is ~100k tokens bootstrap.
      Future work: `claude --print --resume <session-id>` for
      prompt-caching, bringing the wake cost down by an order of
      magnitude. Needs careful handling of concurrent-session races.

- [ ] **Agent identity is self-asserted.** Any tailnet peer can send as
      any `agent_id`. Fine for operator-controlled fleets; not OK for
      multi-tenant. Flagged in docs/cross-machine-tailscale.md §Security
      Model; no code to enforce. Would require mosquitto ACLs keyed on
      client-id.

---

## Backlog discipline

When we find something in normal operation, **add it here** and date
the section. When we close it, flip `[ ]` → `[x]` with the commit SHA.
When we defer, mark `[>]` and put the rationale in-line.

Don't let the list go stale by just adding items — review before each
release and either close, defer, or explicitly drop (with a note). The
spec and plan under `docs/superpowers/` are frozen historical artefacts;
this file is the live ledger.
