# Post-ship backlog

Living record of gaps, drift, and follow-up work found **after** the v1.0
spec + plan (`docs/superpowers/specs/2026-04-14-swarmbus-design.md`,
`docs/superpowers/plans/2026-04-14-swarmbus-build.md`) were executed.
The superpowers/ folder captures what we planned to build; this file
captures what we discovered once it was in use.

Status conventions: `[ ]` open, `[x]` closed with commit SHA, `[~]`
in-progress, `[>]` deferred to a future milestone with the rationale.

---

## 2026-04-14 — round one

Surfaced after Sparrow + Wren both deployed on the RPi and we started
exchanging real swarmbus traffic. The "daemon runs, presence retained,
messages still disappear" incident was the instigating event; we caught
a priority-validation regression during diagnosis and then ran a
specs-vs-implementation audit.

### Real gaps (shipped without what the spec promised)

- [ ] **Config file support missing.** Spec §206-231 called for
      `~/.swarmbus/config.toml` + per-project `.swarmbus.toml` (agent_id,
      broker, bind_host, MCP settings). Zero config parsing in
      `src/swarmbus/cli.py` today — everything is CLI flags and env
      vars. Real scope; probably its own session. Would remove a lot of
      `--broker`/`--agent-id` repetition in systemd units and scripts.

- [ ] **Broker auth (username/password) has no CLI surface.**
      aiomqtt.Client accepts `username=`/`password=` kwargs; AgentBus
      never forwards them. Fine for tailnet-only deployments (Tailscale
      is the auth layer there), blocking for anything else. Expose as
      `--username/--password` (or env vars) on `swarmbus start` and
      `swarmbus send`.

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

- [x] **Handler ordering semantics documented.** `register_handler`
      docstring now spells out order = registration order, sequential
      (not parallel), per-handler exception isolation. Test added
      (`test_listen_preserves_handler_registration_order`).

- [x] **Broadcast now called out in CLI help.** `--to` help text
      includes the `broadcast` sentinel.

### Test gaps (missing coverage for behaviours the spec promised)

- [x] **LWT happy path** (closed in this series). Presence announces
      "online" on subscribe, graceful disconnect publishes "offline".
      `test_integration.py` previously skipped the abort-path LWT case;
      graceful-path case is now covered by
      `test_presence_lifecycle_online_then_offline`.

- [x] **Non-retained delivery semantics** (closed). Covered by
      `test_non_retained_message_lost_when_no_subscriber` — fails if
      the default retain flag is ever flipped.

- [x] **Handler exception isolation** — noted as a gap by the audit but
      turned out to already be tested: `test_listen_continues_after_handler_exception`
      in `tests/test_bus.py:311`. The audit missed it. Noting here so
      future audits don't re-open.

- [x] **Handler ordering** — new test
      `test_listen_preserves_handler_registration_order`. Registration
      order → dispatch order invariant is also now documented in
      `AgentBus.register_handler`'s docstring.

- [x] **64KB body limit end-to-end** (closed). Covered by
      `test_send_receive_large_body_at_limit` — full roundtrip through
      MQTT + handler at exactly the body cap.

- [x] **MCP tool signatures** (closed). Covered by
      `test_mcp_tools_expose_expected_signatures` — asserts the full
      public surface (tool names, arg names, defaults) against the
      expected contract. Any rename/removal fails loudly.

### Scope creep — shipped beyond spec, all fine, just flagging

- [x] `swarmbus read` / `watch` / `list` / `tail` subcommands (CLI was
      meant to be `send`/`listen`/`mcp-server`/`start` only).
- [x] `--outbox` flag + `SWARMBUS_OUTBOX` / `SWARMBUS_OUTBOX_<ID>`
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

- [x] **Rolling-upgrade discipline.** Closed via `CHANGELOG.md` with a
      "Wire-compat" bullet per release (commit `cec46aa`) + `swarmbus
      doctor` subcommand (commit `eab328e`) that includes a "daemon
      library fresh" check — compares running daemon start time to
      source-on-disk mtime and flags stale in-memory Python as
      "STALE — restart recommended".

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

## 2026-04-16 — competitive landscape review

Surfaced after comparing against Kanevry/agentbus (webhook router, TypeScript, early-stage)
and agentbus.org (SaaS REST polling, agent registry). Different products, former name.
Our key technical gap vs the SaaS:

- [ ] **Persistent agent registry.** `swarmbus list` only shows currently-online peers
      (MQTT retained presence). If Wren is offline, she's invisible. agentbus.org
      maintains a persistent directory queryable regardless of online status. Fix:
      SQLite table in `~/.swarmbus/registry.db` written on `swarmbus start` (upsert
      agent_id + last_seen + broker), queryable via `swarmbus registry list`/`show`.
      `swarmbus doctor` could also check registry health. Low code lift; high UX value
      for multi-agent fleets.

- [ ] **Zero-friction setup script ("one command to bus").** Current onboarding requires:
      install mosquitto, pip install, run setup script, configure systemd, `swarmbus doctor`.
      That's 4-5 distinct human steps. A single `curl | bash` (or `swarmbus init`) that
      detects the platform, installs mosquitto, configures it, installs the Python package,
      and prints the `swarmbus start` command would cut onboarding to 1 human step.
      Critical for any public adoption play.

- [>] **Human-facing web UI.** agentbus.org has a browser UI for conversation threads.
      Nice-to-have; our Telegram channel covers this for us. Deferred — not worth building
      for internal use, only relevant if we go public with a hosted tier.

---

## Backlog discipline

When we find something in normal operation, **add it here** and date
the section. When we close it, flip `[ ]` → `[x]` with the commit SHA.
When we defer, mark `[>]` and put the rationale in-line.

Don't let the list go stale by just adding items — review before each
release and either close, defer, or explicitly drop (with a note). The
spec and plan under `docs/superpowers/` are frozen historical artefacts;
this file is the live ledger.
