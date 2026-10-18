# Memetic safety: storing what agents say

This is for anyone whose agents write what peers say into long-term memory: notes, a knowledge base, a vault, a bootstrap file, a summary that survives compaction. The failure it guards against is a false claim, an injected instruction or a bad habit passing from agent to agent through shared memory and getting "confirmed" by repetition (memetic spread, [#26](https://github.com/mpesavento/swarmbus/issues/26)). swarmbus moves the messages; what is stored is the deployer's choice, so this doc is guidance, not something swarmbus enforces.

## Peer messages are claims, not facts

A message from a peer is something that agent said, not something that is true. Write it down as "agent X said Y", never as Y.

A claim does not become more true because a second agent repeats it. The second agent may have read it from the first, directly or through shared memory (memory laundering). Broadcasts are claims from their `from` like any other message.

## Record provenance

Every memory entry written because of a peer message records where it came from, taken from the envelope:

```yaml
claim: "staging moved to db2"
source: agent:planner        # envelope `from`
message_id: 7f3c9a…           # envelope `id`
in_reply_to: 1a2b4c…          # envelope `in_reply_to`, if set
origin: agent:coder           # envelope `origin`: who sent the first message of the chain
hops: 2                       # envelope `hops`
recorded_at: 2026-10-17T12:00:00Z
promoted: false
```

The required form is `source: agent:<id>` for anything a peer said, and `source: human:<name>` or `source: primary:<url or path>` for anything else.

Where the fields come from:

- `swarmbus read --json` and `swarmbus watch --json` print all of them.
- A `--invoke` wake gets `SWARMBUS_FROM`, `SWARMBUS_ID`, `SWARMBUS_HOPS` and `SWARMBUS_ORIGIN`.
- MCP `read_inbox` rows carry `id` and `from` but not `hops`, `origin` or `in_reply_to`, so an MCP agent records at least `source` and `message_id`.

Edge cases:

- A message with no `origin` (a peer from before the reply chain, `envelope_version` 1): record `source` and `message_id`, and `origin: null`.
- A human relaying through an agent: the envelope still says `from: <agent>`. Record `source: agent:<id>` unless the human's words arrive by a channel the deployer trusts. swarmbus has no way to mark a message as a person's.

`from` and `origin` are not authenticated. See the "**Lying in `from`.**" bullet under [What this does not stop](security.md#what-this-does-not-stop) in `docs/security.md`. Provenance tells you which agent to blame or ask, not that the agent really sent it.

## A promotion gate

Keep two kinds of store apart:

- working notes: anything goes, with provenance attached;
- curated stores that agents treat as knowledge: a knowledge base, a bootstrap or startup file, shared "facts".

An entry whose only sources are `agent:` sources does not enter a curated store until a person confirms it or a primary source backs it. Record which one: `promoted_by: human:<name>` or `promoted_by: primary:<url or path>`. An agent never promotes its own or another agent's claim on its own say-so.

## Rank by quality, not recency

Anything an agent reads at startup or re-reads to bootstrap (inbox files, archives, "latest notes", a feed of recent messages) is a recency feed, the kind of channel the background study found easiest to capture. Rank such context by quality or relevance: curated first, human or primary sources first, agent-only claims last or not at all.

Keep bootstrap and working-memory files short and curated. Do not load a raw inbox or archive into a startup prompt.

## Do not rely on wiping memory

Restarts, compactions and "clear memory" are resets. They do not clear contamination that lives in files agents re-read, and they throw away the clean history that crowds a bad claim out. Fix ranking and the promotion gate first, and keep the clean history.

The study found a trade-off: wiping did cut how much poison sat in the long-term archive, and under recency or engagement ranking it lowered infection. So a wipe can be a stopgap for a store you cannot re-rank. It is not the fix.

## Audit the store

On a schedule (weekly is a reasonable start), look for:

- claims that more than one agent repeats where every source is `agent:`;
- claims whose chain traces back to a single `origin`;
- entries with no provenance at all.

Measure how much of a curated store rests on agent-only sources, not how many messages went by: the study found message-level checks stayed flat while the store was being captured. For the channel side, `swarmbus held` and `swarmbus doctor` show tripped and cooling senders.

An example over the YAML records above (not a shipped tool):

```python
import sys
from collections import defaultdict

import yaml

sources = defaultdict(set)
for path in sys.argv[1:]:
    with open(path) as f:
        for rec in yaml.safe_load_all(f):
            if rec and "claim" in rec:
                sources[rec["claim"]].add(str(rec.get("source")))

for claim, srcs in sources.items():
    # every source is an agent, and at least two of them
    if len(srcs) >= 2 and all(s.startswith("agent:") for s in srcs):
        print(claim, sorted(srcs))
```

## What swarmbus does and does not do

Does:

- labels peer text as untrusted wherever it renders it (`read`, `watch`, the inbox file, MCP `"untrusted": true`) ([skill](../src/swarmbus/skills/using-swarmbus/SKILL.md#security--inbound-messages-are-not-trusted-input));
- carries the reply chain: `in_reply_to`, `hops`, `origin` ([envelope](../README.md#message-envelope));
- holds messages past the hop limit ([envelope](../README.md#message-envelope));
- rate-limits each sender ([envelope](../README.md#message-envelope));
- never lets a broadcast wake a wake script or trip a sender ([envelope](../README.md#message-envelope));
- alerts a person on a trip with `--on-trip` ([envelope](../README.md#message-envelope)).

Does not:

- authenticate `from` or `origin`;
- look at what a message says;
- know what an agent writes to memory.

Everything in the sections above is the deployer's job.

## Background

> A simulation from the SWARM project put ten agents, two in ten of them poisoners, on a shared memory whose top-ranked entries every agent reads each round, with a contagion rule in which an agent's infection follows the poisoned share of what it reads and survives memory wipes. Without wipes, infection peaked at 0.37 and then fell to 0.012 as clean, higher-quality entries crowded the poison out; with periodic wipes it stayed at 0.10–0.30 late in the run (a peak of 0.48 with a wipe every 10 rounds), although wipes did cut poison in the long-term archive (0.62 to 0.10 with a wipe every 2 rounds). How the shared cache was ranked mattered most: ranked by quality, infection burned out (0.012); ranked by recency it stayed near-total (0.870), and ranked by engagement it plateaued (0.485). A promotion gate with provenance tracking kept peak infection under 0.1 and cut contagion writes from 71 to 4, at about a 12% cost in welfare. Message-level toxicity stayed flat (0.287–0.292) throughout, so watching messages would not have shown any of it. The authors call it a small mechanism model (ten agents, one roster, a hand-built contagion rule, and a quality signal that is honest by construction): it shows the shape of the effects, not their size in a real system.

Source: "The Meme Survives the Medium: Memory Wipes, Herd Immunity, and the Editorial Policy of Shared Memory", https://swarm-ai.org/blog/memetic-spread-reset-vs-detection/

Further reading:

- Mallen, Redwood Research, 2025
- Das et al., 2026: https://arxiv.org/abs/2610.04083
- Cohen, Bitton, Nassi, 2024: https://arxiv.org/abs/2403.02817
- Gu et al., 2024: https://arxiv.org/abs/2402.08567
- Greshake et al., 2023: https://arxiv.org/abs/2302.12173
