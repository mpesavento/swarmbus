# Cross-machine agentbus over Tailscale

agentbus was designed to work single-host *or* across hosts unchanged — the wire protocol is identical. This doc walks through the cross-host case end to end: the topology, the broker host setup, the peer-agent setup, the security model, and the failure modes you should know about before you rely on it.

If you only have one machine, skip this doc — the main quickstart covers you.

---

## When to reach for this

- You want an agent on a laptop, phone, or another host to talk to agents running on a central home server.
- You want two servers (home Pi + cloud VM) to coordinate without exposing mosquitto on the public internet.
- You want the same agent identity reachable from multiple physical locations — e.g. a coding assistant you can message from any tailnet-joined device.

If you're only exchanging between agents on the same machine, point at `localhost` and stop reading.

---

## Why Tailscale specifically

MQTT on the public internet needs TLS + auth or you're offering the broker to anyone who can scan port 1883. That's a real ops burden — certs, user lists, rotations. Tailscale collapses all of that to a single decision: *"is this peer in my tailnet?"* — decided by WireGuard identity, with traffic end-to-end-encrypted between peers.

So inside a tailnet:
- Mosquitto can run anonymous without shame — the mesh already authenticated.
- No port-forwarding, no NAT punching, no dynamic DNS, no LetsEncrypt.
- `<host>.<tailnet>.ts.net` MagicDNS names resolve everywhere on the tailnet.

Outside a tailnet (public internet, VPS accessible from anywhere): don't do this. Use mosquitto's TLS + password stack instead.

---

## Topology

```
                             agents/+/inbox
                             agents/broadcast
                                  │
               ┌──────────────────┼──────────────────┐
               │                  │                  │
          ┌────▼────┐        ┌────▼────┐        ┌────▼────┐
          │ Sparrow │        │   Wren  │        │Laptop CC│
          │ (RPi)   │        │  (RPi)  │        │ (Mac)   │
          └────┬────┘        └────┬────┘        └────┬────┘
               │ broker="localhost"│ broker="localhost"│ broker="clawd-rpi.<tn>.ts.net"
               │                   │                   │
               └───────────────────┼───────────────────┘
                                   │
                       ┌───────────▼──────────┐
                       │ mosquitto on RPi     │
                       │ listeners:           │
                       │   127.0.0.1:1883     │
                       │   100.x.y.z:1883     │  ← Tailscale IP
                       └──────────────────────┘
                                   │
                          WireGuard mesh (Tailscale)
```

One broker, many peers. Peers on the broker host reach it via `localhost`; peers elsewhere reach it via the broker's Tailscale IP or MagicDNS hostname. The agentbus wire protocol doesn't change — neither does any message envelope. This is purely a reachability change.

---

## Prerequisites

1. **Tailscale installed and running on all hosts** — broker host, and every host that will run an agent. Verify with `tailscale status`; all relevant nodes should be listed, not "offline".
2. **MagicDNS enabled** on the tailnet (optional but recommended — lets you use `host.<tailnet>.ts.net` hostnames instead of hard-coding IPs).
3. **agentbus installed on all hosts** (`pip install agentbus`). The CLI is what we'll use for the tests below.
4. **mosquitto installed on the broker host only**. The other hosts don't need it.
5. **Firewall**: on Linux, make sure `ufw` / `iptables` isn't blocking 1883 on the Tailscale interface. Default Debian has no rules, so typically nothing to do. On macOS, the system firewall generally allows established tailnet connections automatically.

---

## Broker host setup

One command:

```bash
# On the machine that will run mosquitto:
cd ~/path/to/agentbus
bash scripts/setup-mosquitto.sh --tailscale
```

The script will:

1. Install mosquitto + mosquitto-clients via apt.
2. Read your Tailscale IPv4 with `tailscale ip -4`. Errors clearly if Tailscale isn't up.
3. Write `/etc/mosquitto/conf.d/tailscale.conf`:
   ```
   listener 1883 100.x.y.z
   allow_anonymous true
   ```
   This adds a listener bound to your Tailscale IP. The default `127.0.0.1:1883` listener stays intact so local daemons on the broker host keep working with `broker="localhost"`.
4. Restart mosquitto.
5. Print the broker address to use from peers — both numeric IP and MagicDNS hostname when resolvable.

Use `--tailscale-only` instead if you want to replace the default listener (no LAN exposure at all — local daemons on the broker host must then also point at the Tailscale IP, not localhost). For single-purpose servers that's often preferable.

### Verify the broker is reachable

On the broker host:

```bash
ss -tlnp | grep :1883
# Expect: 127.0.0.1:1883 AND 100.x.y.z:1883
```

From a different tailnet-joined host:

```bash
mosquitto_pub -h <broker-tailnet-hostname-or-ip> -t test -m 'hello from peer'
# In another shell on yet another host (or the broker):
mosquitto_sub -h <broker-tailnet-hostname-or-ip> -t test
```

If the subscriber never sees "hello from peer", the broker isn't reachable. Most common causes:

- Tailscale down on either end (`tailscale status` → shows "offline" for the peer).
- Mosquitto bound wrong (re-check `ss -tlnp`).
- Local firewall on the broker host (`sudo iptables -L INPUT` — look for REJECT/DROP on 1883).

---

## Peer agent setup

Every agentbus invocation on a non-broker host takes `--broker <address>`. That's the only thing that changes compared to the single-host quickstart.

Pick an addressing form:

- **MagicDNS hostname** (`clawd-rpi.tailea0d6e.ts.net`) — preferred. Survives Tailscale renumbering, readable, auto-resolves.
- **Tailscale IPv4** (`100.119.209.81`) — fallback if MagicDNS is off. Fine but brittle if the host's tailnet IP ever changes.
- **Never use the physical LAN IP** (`192.168.x.x`) — works only on the same subnet, and if you bind the broker with `--tailscale-only`, it won't work even there.

### Minimal peer check

```bash
# On the peer:
export AGENTBUS_OUTBOX="$HOME/sync/{agent_id}-outbox.md"
agentbus list --broker clawd-rpi.tailea0d6e.ts.net
# Expect to see whichever agents have listener daemons up on the broker host.
```

If `list` comes back empty but you know Sparrow + Wren are running daemons, the broker connection isn't reaching them — same troubleshooting list as above.

### Run a full listener peer on the laptop

```bash
agentbus start \
  --agent-id laptop-cc \
  --broker clawd-rpi.tailea0d6e.ts.net \
  --inbox ~/sync/laptop-cc-inbox.md
```

Now `laptop-cc` is a first-class peer — Sparrow and Wren can `send_message(to="laptop-cc", ...)` from the Pi and the body lands in `~/sync/laptop-cc-inbox.md` on the laptop.

From the laptop, back the other way:

```bash
agentbus send \
  --agent-id laptop-cc \
  --broker clawd-rpi.tailea0d6e.ts.net \
  --to sparrow --subject "hi" --body "from the laptop"
```

### Environment pattern to avoid repeating `--broker`

Most tailnet-connected agents talk to exactly one broker. Set it once:

```bash
# in the peer host's shell profile
export AGENTBUS_BROKER=clawd-rpi.tailea0d6e.ts.net   # future: honoured by CLI auto-default
# current: wrap agentbus in a small alias
alias agentbus='agentbus --broker clawd-rpi.tailea0d6e.ts.net' # won't work; broker is per-subcommand
```

Today the cleanest route is a tiny wrapper script in `~/bin/ab`:

```bash
#!/usr/bin/env bash
# Redirect agentbus through the tailnet broker.
exec agentbus "${@/#send/send --broker clawd-rpi.tailea0d6e.ts.net}"
# (simpler: just always type --broker explicitly)
```

If `AGENTBUS_BROKER` as a universal env default would be useful to you, it's a small CLI patch — tell the maintainer.

---

## Security model

| Layer | What Tailscale gives you | What you still owe |
|---|---|---|
| **Network auth** | WireGuard identity per node; only tailnet members reach the broker. | Keep the tailnet membership list tight (`tailscale admin`). |
| **Transport encryption** | WireGuard end-to-end between peers. | Nothing on top — mosquitto doesn't need TLS here. |
| **Application auth** | None, and that's fine inside the tailnet. | Do not reuse this broker for clients outside the tailnet. |
| **Message integrity** | Standard MQTT QoS1 delivery guarantees. | Handlers must be idempotent (QoS1 can redeliver). |
| **Agent-level auth** | agentbus has none — the `agent_id` is self-asserted. | If two peers on the tailnet should NOT be able to impersonate each other, this isn't the right layer. Use mosquitto ACLs per-client-id or switch to MQTT auth. |

Bottom line: Tailscale authenticates that your peer is who you added to the tailnet. It does *not* authenticate that the agent running on that peer is the agent it claims to be — any tailnet-joined peer can send as any `agent_id`. That's usually fine between your own agents on your own hosts. It's not fine in a multi-tenant deployment.

### Do not ever

- Expose the Tailscale-bound mosquitto port to the public internet (port-forwarding, VPN bridging, Tailscale Funnel). The whole security argument dissolves the moment a non-tailnet client can reach the broker anonymously.
- Run `--tailscale` and *also* `listener 1883 0.0.0.0` from a separate config file. Double-check `ss -tlnp` after any config change.
- Trust `agent_id` in incoming envelopes as an identity claim. It's a routing label.

---

## Failure modes

### Broker host goes offline

All non-broker peers lose connectivity. Their daemons enter exponential-backoff reconnect (1s, 2s, 4s, ... capped at 60s). As soon as the broker comes back up, every peer reconnects; if `--persistent` is on (default for `agentbus start`), the broker redelivers QoS1 messages that were queued while a peer was disconnected. Messages sent *to* the broker while *it* was down are simply lost — the sender gets an MqttError on `send`, not silent swallow.

### Peer host goes offline mid-conversation

If the peer is running `agentbus start --persistent`, the broker queues QoS1 messages for that peer's agent-id. When the peer reconnects, they redeliver. Bodies are capped at 64KB, so there's no unbounded memory risk; mosquitto will discard old queued messages per its persistence config if a peer is offline for very long.

### Laptop goes to sleep

Tailscale tears down the WireGuard tunnel when the host sleeps. agentbus daemon on the laptop disconnects from the broker. On wake, Tailscale reconnects (a few seconds) and the agentbus reconnect-backoff fires. No action needed.

### MagicDNS resolves the wrong host

Usually because the broker host's MagicDNS name changed (rare — only happens on host rename). Look up the new name with `tailscale status`. Update `--broker` on peers. If you have a lot of peers, prefer the `<tailnet>.ts.net` numeric IP or set up a stable hostname alias.

### Two daemons for the same agent-id on different hosts

**Don't.** With `--persistent` (the default), they share one MQTT client identifier and kick each other in a loop every few seconds. With `--no-persistent`, they race for each QoS1 message and each sees a roughly-even fraction of the traffic. In both cases, the archive you read from either machine is incomplete.

If you legitimately want "reach me at whichever machine I happen to be on," use *different* agent-ids per host (`sparrow-pi` and `sparrow-laptop`) and have whoever's sending to you decide which to target (via `list_agents` or a routing convention).

---

## Extending beyond Tailscale

If you outgrow the tailnet model — e.g. opening agentbus to a collaborator's tailnet, or to a host that can't install Tailscale — switch to mosquitto with TLS + username/password. `scripts/setup-mosquitto.sh` doesn't cover that today. The mosquitto docs are the canonical source: https://mosquitto.org/documentation/authentication-methods/.

The agentbus CLI doesn't expose TLS flags yet. Two options in the meantime:

- Use the Python API, which hands through to aiomqtt and supports TLS natively.
- Run `agentbus` behind a local mosquitto client config (`default.conf`) that does the TLS wrap, and point agentbus at a localhost bridge.

Neither is quite as clean as the Tailscale path. If you need proper TLS + auth on the CLI, open an issue.

---

## Quick checklist

Before considering cross-machine agentbus "set up":

- [ ] `tailscale status` on all hosts shows each other as online.
- [ ] Broker host has a `--tailscale` listener (`ss -tlnp | grep :1883` shows both `127.0.0.1` and the tailnet IP).
- [ ] `mosquitto_pub`/`mosquitto_sub` work between two tailnet hosts using the broker's tailnet hostname.
- [ ] `agentbus list --broker <tailnet-host>` from a peer returns the expected agent-ids.
- [ ] `agentbus send` from a peer and verify it arrives in the recipient's inbox file.
- [ ] All peers have `AGENTBUS_OUTBOX` set (scoped or `{agent_id}` template) so outbound messages archive cleanly.
- [ ] Every real agent identity runs under exactly one host — one daemon per agent-id across the whole tailnet.
