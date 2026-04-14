#!/usr/bin/env bash
# examples/openclaw-wake.sh
#
# DirectInvocationHandler wrapper that turns an agentbus message into a
# real OpenClaw agent turn. Use this to get reactive push delivery:
# every inbound message wakes the agent, no polling, no cron.
#
# Wire it in via --invoke on the listener daemon:
#
#   agentbus start \
#     --agent-id wren \
#     --inbox ~/sync/wren-inbox.md \
#     --invoke "$HOME/projects/agentbus/examples/openclaw-wake.sh wren"
#
# Arguments:
#   $1  OpenClaw agent id (e.g. "main", "wren", "ops"). Required.
#
# Env vars (set by DirectInvocationHandler):
#   AGENTBUS_FROM, AGENTBUS_SUBJECT, AGENTBUS_REPLY_TO, AGENTBUS_CONTENT_TYPE, …
#
# The message body arrives on stdin. We pass it into `openclaw agent --message`
# so the agent takes a real turn (reasoning, tool use, memory, the whole thing).

set -euo pipefail

OPENCLAW_AGENT="${1:?Usage: openclaw-wake.sh <openclaw-agent-id>}"

body=$(cat)

# Prepend envelope metadata so the agent has context about who sent what.
prompt="[agentbus message from ${AGENTBUS_FROM:-?}"
if [ -n "${AGENTBUS_REPLY_TO:-}" ]; then
  prompt+=", reply_to=${AGENTBUS_REPLY_TO}"
fi
prompt+="]
subject: ${AGENTBUS_SUBJECT:-?}

${body}"

openclaw agent --agent "$OPENCLAW_AGENT" --message "$prompt"
