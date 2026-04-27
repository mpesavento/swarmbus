#!/usr/bin/env node
// examples/openclaw-bridge.mjs
//
// Node helper that delivers a swarmbus message to a running OpenClaw
// agent by speaking the gateway WebSocket protocol directly, instead of
// shelling out to the full `openclaw agent --message ...` CLI.
//
// On a Raspberry Pi 5, the full CLI bootstrap is ~11s before the message
// even reaches the gateway. Importing the gateway client alone is ~700ms,
// so this helper saves ~10s of wake latency per message.
//
// Reads:
//   stdin            — message body (sanitised prompt, built by the wake script)
//   $1               — OpenClaw agent id (e.g. "main")
//   ~/.openclaw/openclaw.json
//                    — gateway.port, gateway.auth.token
//   env SWARMBUS_ID  — used as the gateway idempotencyKey
//   env OPENCLAW_BRIDGE_TIMEOUT_MS
//                    — overall request timeout (default 600_000)
//
// Resolution order for the OpenClaw install:
//   $OPENCLAW_GATEWAY_RUNTIME_PATH — explicit override (file path)
//   $OPENCLAW_INSTALL_DIR/dist/plugin-sdk/gateway-runtime.js
//   <dir of this script>/../../openclaw/dist/plugin-sdk/gateway-runtime.js (dev)
//   ~/.local/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js
//   /usr/local/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js
//
// Exit codes:
//   0  — gateway accepted, agent responded
//   2  — config / install missing or unreadable
//   3  — gateway request failed (timeout, auth, transport)
//   4  — bad invocation (missing agent id, etc.)

import { readFile } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const t_start = Date.now();
const HERE = dirname(fileURLToPath(import.meta.url));

function logTimed(label) {
  if (process.env.OPENCLAW_BRIDGE_VERBOSE) {
    process.stderr.write(`[bridge ${Date.now() - t_start}ms] ${label}\n`);
  }
}

function fail(code, msg) {
  process.stderr.write(`openclaw-bridge: ${msg}\n`);
  process.exit(code);
}

function findGatewayRuntimeModule() {
  const candidates = [
    process.env.OPENCLAW_GATEWAY_RUNTIME_PATH,
    process.env.OPENCLAW_INSTALL_DIR
      ? join(process.env.OPENCLAW_INSTALL_DIR, "dist/plugin-sdk/gateway-runtime.js")
      : null,
    resolve(HERE, "../../openclaw/dist/plugin-sdk/gateway-runtime.js"),
    join(homedir(), ".local/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js"),
    "/usr/local/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js",
  ].filter(Boolean);
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

function loadOpenClawConfig() {
  const path = process.env.OPENCLAW_CONFIG_PATH ?? join(homedir(), ".openclaw/openclaw.json");
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch (err) {
    fail(2, `cannot read ${path}: ${err.message}`);
  }
  let cfg;
  try {
    cfg = JSON.parse(raw);
  } catch (err) {
    fail(2, `${path} is not valid JSON: ${err.message}`);
  }
  const port = cfg?.gateway?.port;
  const token = cfg?.gateway?.auth?.token;
  if (typeof port !== "number") fail(2, `${path} missing gateway.port`);
  if (typeof token !== "string" || token.length === 0) {
    fail(2, `${path} missing gateway.auth.token (token-mode auth required)`);
  }
  return { url: `ws://127.0.0.1:${port}`, token, configPath: path };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function parseArgs(argv) {
  const args = argv.slice(2);
  if (args.length < 1 || !args[0] || args[0].startsWith("-")) {
    fail(4, "usage: openclaw-bridge.mjs <openclaw-agent-id>");
  }
  return { agentId: args[0] };
}

async function main() {
  const { agentId } = parseArgs(process.argv);
  const message = await readStdin();
  if (!message.trim()) fail(4, "empty message body on stdin");
  logTimed("stdin read");

  const runtimePath = findGatewayRuntimeModule();
  if (!runtimePath) {
    fail(
      2,
      "could not locate openclaw plugin-sdk gateway-runtime.js; " +
        "set OPENCLAW_GATEWAY_RUNTIME_PATH or OPENCLAW_INSTALL_DIR",
    );
  }
  logTimed(`resolved runtime: ${runtimePath}`);

  const { GatewayClient } = await import(runtimePath);
  if (typeof GatewayClient !== "function") {
    fail(2, `loaded ${runtimePath} but GatewayClient is not a constructor`);
  }
  logTimed("imported GatewayClient");

  const { url, token } = loadOpenClawConfig();
  const idempotencyKey = process.env.SWARMBUS_ID || randomUUID();
  const timeoutMs = Number(process.env.OPENCLAW_BRIDGE_TIMEOUT_MS) || 600_000;

  const params = {
    message,
    idempotencyKey,
    agentId,
    deliver: false,
    timeout: Math.max(1, Math.floor(timeoutMs / 1000)),
  };

  // Forward the swarmbus envelope as a label so it shows up in run history.
  const subject = process.env.SWARMBUS_SUBJECT;
  if (subject) params.label = `swarmbus: ${subject}`.slice(0, 128);

  await new Promise((resolveCall, rejectCall) => {
    let settled = false;
    let ignoreClose = false;

    const finish = (err, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (err) rejectCall(err);
      else resolveCall(value);
    };

    // Note: deviceIdentity is intentionally omitted. GatewayClient's constructor
    // calls loadOrCreateDeviceIdentity() when the field is `undefined`, which
    // is what binds the operator scopes. Passing `null` would explicitly
    // disable auto-load and the gateway would strip "operator.write" via
    // clearUnboundScopes(), rejecting the request.
    const client = new GatewayClient({
      url,
      token,
      clientName: "gateway-client",
      mode: "backend",
      role: "operator",
      scopes: ["operator.write"],
      onHelloOk: async () => {
        logTimed("hello ok");
        try {
          const result = await client.request("agent", params, {
            expectFinal: true,
            timeoutMs,
          });
          logTimed("agent response received");
          ignoreClose = true;
          client.stop();
          finish(undefined, result);
        } catch (err) {
          ignoreClose = true;
          client.stop();
          finish(err);
        }
      },
      onClose: (code, reason) => {
        if (settled || ignoreClose) return;
        ignoreClose = true;
        client.stop();
        finish(new Error(`gateway closed: code=${code} reason=${reason || "(none)"}`));
      },
    });

    const timer = setTimeout(() => {
      ignoreClose = true;
      client.stop();
      finish(new Error(`gateway request timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    client.start();
    logTimed("client started");
  })
    .then((result) => {
      // Best-effort: print a one-line summary so wake-script logs are useful.
      const summary = result?.summary || result?.status || "ok";
      const runId = result?.runId ? ` runId=${result.runId}` : "";
      process.stdout.write(`agent ok: ${summary}${runId}\n`);
      process.exit(0);
    })
    .catch((err) => {
      fail(3, err?.message || String(err));
    });
}

main().catch((err) => fail(3, err?.message || String(err)));
