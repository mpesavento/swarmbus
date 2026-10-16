# Broker auth + TLS for swarmbus

While solutions such as Tailscale can provide out of the box protection for a MQTT deployment, this is not always an option. This doc covers hardening swarmbus with mTLS and/or user credentials (env vars, flags, what to put in a systemd unit).

If you're on a tailnet, see [cross-machine-tailscale.md](cross-machine-tailscale.md) instead — it's a strictly easier model.

---

## What swarmbus exposes

Every CLI subcommand that opens a broker connection (`send`, `start`, `read`, `watch`, `list`, `mcp-server`, `doctor`) accepts the same six options. Each maps to an env-var fallback so you can set credentials once in a systemd unit's `Environment=` block instead of typing flags everywhere.

| CLI flag         | Env var                       | Purpose                                                                                |
|------------------|-------------------------------|----------------------------------------------------------------------------------------|
| `--username`     | `SWARMBUS_BROKER_USERNAME`    | MQTT username.                                                                         |
| `--password`     | `SWARMBUS_BROKER_PASSWORD`    | MQTT password.                                                                         |
| `--ca-cert`      | `SWARMBUS_BROKER_CA_CERT`     | CA bundle that signed the broker cert. Implies TLS.                                    |
| `--client-cert`  | `SWARMBUS_BROKER_CLIENT_CERT` | Client certificate for mTLS. Must be set together with `--client-key`.                 |
| `--client-key`   | `SWARMBUS_BROKER_CLIENT_KEY`  | Client key for mTLS. Must be set together with `--client-cert`.                        |
| `--tls/--no-tls` | `SWARMBUS_BROKER_TLS`         | Enable TLS without mTLS (CA from system trust). Implied automatically by any cert flag. |

Set `SWARMBUS_BROKER_TLS=1` (or `true` / `yes`) when the broker uses a cert that chains to a CA already in the system trust store and you don't want to ship a separate `--ca-cert`. If `--ca-cert`, `--client-cert`, or `--client-key` is set, TLS engages whether or not `--tls` is also passed.

The Python API mirrors the CLI:

```python
from swarmbus.bus import AgentBus

bus = AgentBus(
    agent_id="laptop-cc",
    broker="mqtt.example.com",
    port=8883,
    username="laptop-cc",
    password="…",
    ca_cert="/etc/ssl/certs/internal-ca.crt",  # or tls=True for system trust
)

# mTLS
bus = AgentBus(
    agent_id="laptop-cc",
    broker="mqtt.example.com",
    port=8883,
    username="laptop-cc",
    password="…",
    ca_cert="/etc/ssl/certs/internal-ca.crt",
    client_cert="/etc/ssl/private/laptop-cc.crt",
    client_key="/etc/ssl/private/laptop-cc.key",
)
```

Misconfigurations (e.g. `client_cert` without `client_key`, unreadable cert files) raise at construction time, not on the first network call — so a broken `swarmbus start` fails loudly at boot rather than silently sitting in a reconnect loop.

---

## systemd unit example

Once the broker is hardened, the cleanest way to keep credentials out of process arguments is to set them in the unit's `Environment=` block:

```ini
# ~/.config/systemd/user/swarmbus-laptop-cc.service
[Unit]
Description=swarmbus listener (laptop-cc)
After=network-online.target

[Service]
Type=simple
Environment=SWARMBUS_BROKER_USERNAME=laptop-cc
Environment=SWARMBUS_BROKER_PASSWORD=…
Environment=SWARMBUS_BROKER_TLS=1
# Or, if the broker cert isn't in the system trust store:
# Environment=SWARMBUS_BROKER_CA_CERT=/etc/ssl/certs/internal-ca.crt
ExecStart=/usr/local/bin/swarmbus start \
    --agent-id laptop-cc \
    --broker mqtt.example.com --port 8883 \
    --inbox %h/sync/laptop-cc-inbox.md \
    --persistent
Restart=on-failure

[Install]
WantedBy=default.target
```

The same `SWARMBUS_BROKER_*` vars work for the MCP sidecar registered with Claude Code or any other MCP host — set them in the host's MCP server `env` block.

### Persisting credentials via `swarmbus init`

`swarmbus init` accepts the same six broker auth flags. When any are supplied (or read from the matching `SWARMBUS_BROKER_*` env var), they're written into a systemd drop-in at `~/.config/systemd/user/swarmbus-<agent-id>.service.d/auth.conf` (mode 0600), which systemd merges with the main unit at daemon-reload time. The main `ExecStart` line stays clean.

```bash
swarmbus init --agent-id laptop-cc \
  --broker mqtt.example.com \
  --username laptop-cc \
  --password "$(pass mqtt/laptop-cc)" \
  --tls
```

Re-running `init` without auth flags **does not** delete an existing drop-in — that's a deliberate guard against accidentally wiping credentials with a bare `init` re-run. Delete `auth.conf` manually if you intend to drop the credentials.

---

## One login per agent on a single box

On one host every agent talks to the same broker on `127.0.0.1`. With anonymous access, any local process can read every agent's inbox and publish as any agent. Give each agent id its own mosquitto login and let the broker limit what that login can do.

### Password file

Use `mosquitto_passwd`. The username is exactly the agent id. `-c` creates the file and overwrites any existing one, so use it for the first agent only.

```bash
sudo mosquitto_passwd -c /etc/mosquitto/swarmbus.passwd planner   # first agent only: -c overwrites
sudo mosquitto_passwd /etc/mosquitto/swarmbus.passwd coder        # each further agent
```

### ACL file

```
# /etc/mosquitto/swarmbus.acl
# Every login: read its own inbox; publish and read its own presence.
# %u is the MQTT username, which must equal the agent id.
pattern read agents/%u/inbox
pattern readwrite agents/%u/presence

# One block per agent id: send to any inbox, see every agent's
# presence, send and receive broadcasts.
user planner
topic write agents/+/inbox
topic read agents/+/presence
topic readwrite agents/broadcast

user coder
topic write agents/+/inbox
topic read agents/+/presence
topic readwrite agents/broadcast
```

- **`%u`, not `%c`.** swarmbus sets the client id to `swarmbus-<id>` only in persistent mode. Every other connection gets a random client id from the MQTT library, so a `%c` pattern would match nothing. The username is the one thing every connection carries, which is why it must equal the agent id.
- **Shared rules sit under `user`.** In an acl_file, `topic` lines before the first `user` line apply only to anonymous clients. mosquitto 2.1 logs a startup warning for every `pattern` line that has no `%u` or `%c`. So the rules every agent shares are repeated in one `user` block per agent id. Add a block when you add an agent.

### Broker config

```
# /etc/mosquitto/conf.d/swarmbus-auth.conf
listener 1883 127.0.0.1
allow_anonymous false
password_file /etc/mosquitto/swarmbus.passwd
acl_file /etc/mosquitto/swarmbus.acl
```

```bash
sudo chown mosquitto:mosquitto /etc/mosquitto/swarmbus.passwd /etc/mosquitto/swarmbus.acl
sudo chmod 0700 /etc/mosquitto/swarmbus.passwd /etc/mosquitto/swarmbus.acl
sudo systemctl restart mosquitto
```

mosquitto 2.x warns about a password or ACL file it does not own or that others can read, and says future versions will refuse such files.

- Omit the `listener` line if the config already declares `listener 1883 127.0.0.1`; a duplicate fails.
- `allow_anonymous` is one global setting. If `scripts/setup-mosquitto.sh --tailscale` or `--tailscale-only` was run, `/etc/mosquitto/conf.d/tailscale.conf` sets `allow_anonymous true`. Delete that line, or the result depends on which file mosquitto reads last. Agents on other machines that connect over the tailnet then need a login too.
- On macOS with Homebrew the files live under `/opt/homebrew/etc/mosquitto/`, and mosquitto runs as your user.

### Giving each swarmbus surface its login

| Surface | How it gets the login |
|---|---|
| `swarmbus start` (the daemon) | `Environment=SWARMBUS_BROKER_USERNAME=<id>` and `SWARMBUS_BROKER_PASSWORD=…` in the unit (see "systemd unit example" above), or `swarmbus init --username <id> --password …`, which writes the `auth.conf` drop-in. |
| the `--invoke` wake command | Inherits the daemon's environment (`src/swarmbus/handlers/direct_invoke.py:20-30`), so a `swarmbus send` run from the woken session uses the daemon's login with no extra setup. |
| `swarmbus send`, `read`, `watch` | `SWARMBUS_BROKER_USERNAME` / `SWARMBUS_BROKER_PASSWORD`, or `--username` / `--password`. The username must equal `--agent-id`. Prefer the env vars, because flags show in `ps`. |
| `swarmbus mcp-server` | The same two env vars in the MCP host's `env` block for the server, or the flags in its `args`. |
| `swarmbus list`, `swarmbus doctor` | Any agent's login. They only read presence. |

### What this does not stop

- **Writing into another agent's inbox.** Every login may publish to `agents/+/inbox`, because that is how `send` works. What the ACL stops is a process with no login connecting at all, an agent reading another agent's inbox, and an agent publishing another agent's presence.
- **Lying in `from`.** The broker checks topics, not message bodies. A process holding the `coder` login can still send an envelope with `"from": "planner"` and it is delivered. A swarmbus-side check that `from` matches the login is planned separately. A spoofed `from` on a receiver's inbox topic can trip that sender there or start its cool-down; `--on-trip` tells a person when it happens, and alerts are capped (one per trip, 64 queued).
- **Floods.** Any login can publish as fast as it likes, including to `agents/broadcast`. See "Broker limits" in the README and [#26](https://github.com/mpesavento/swarmbus/issues/26).
- **Anyone who can read the credentials.** That means the unit file, the drop-in, the MCP host config, or root on the box. Passwords cross loopback in plain text; add TLS (above) if the broker listens anywhere but `127.0.0.1`.
- **Silent denials.** mosquitto drops a denied publish without telling the client, so `swarmbus send` still prints `Sent to …`. A login that does not match `--agent-id` connects without error but never receives anything, and its presence never shows in `swarmbus list`.

---

## Common failure modes

| Symptom                                        | Probable cause                                                                                              |
|------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `aiomqtt.MqttError: Not authorized`            | Wrong username/password, or the broker is configured `allow_anonymous false` and you're connecting without creds. |
| `ssl.SSLCertVerificationError: …unable to get local issuer certificate` | `--ca-cert` not set and the broker cert isn't in the system trust store. Pass `--ca-cert` or install the CA. |
| `ssl.SSLCertVerificationError: …Hostname mismatch`  | Broker cert's CN/SAN doesn't match the hostname you're connecting to. Re-issue with the right SAN.           |
| Connect succeeds, all SUBSCRIBE return SUBACK, no messages arrive | Mosquitto 2.1+ grants SUBACK at the protocol level; ACLs filter at message **delivery**. Check the broker ACL file. |
| Connects without error, but `read`/`watch` never receive anything, or the agent is missing from `swarmbus list` | The broker login is not the agent id, so the ACL drops its deliveries and its presence publishes without telling the client. Use the agent id as the username. |
| `swarmbus start` exits immediately with `ValueError: client_cert and client_key must be set together` | mTLS half-configured. Either set both or set neither.                                                       |
| Broker rate-limits / drops your connection     | Out of scope of swarmbus today; tracked in #26. See "Broker limits" in the README for the queue and size caps mosquitto does have. |
