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

## Common failure modes

| Symptom                                        | Probable cause                                                                                              |
|------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `aiomqtt.MqttError: Not authorized`            | Wrong username/password, or the broker is configured `allow_anonymous false` and you're connecting without creds. |
| `ssl.SSLCertVerificationError: …unable to get local issuer certificate` | `--ca-cert` not set and the broker cert isn't in the system trust store. Pass `--ca-cert` or install the CA. |
| `ssl.SSLCertVerificationError: …Hostname mismatch`  | Broker cert's CN/SAN doesn't match the hostname you're connecting to. Re-issue with the right SAN.           |
| Connect succeeds, all SUBSCRIBE return SUBACK, no messages arrive | Mosquitto 2.1+ grants SUBACK at the protocol level; ACLs filter at message **delivery**. Check the broker ACL file. |
| `swarmbus start` exits immediately with `ValueError: client_cert and client_key must be set together` | mTLS half-configured. Either set both or set neither.                                                       |
| Broker rate-limits / drops your connection     | Out of scope of swarmbus today. Tracked separately from issue #7. Configure broker-side limits.             |
