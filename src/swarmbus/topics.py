"""Single source of truth for swarmbus MQTT topic strings.

Every topic the bus publishes to, subscribes to, or parses is built and
parsed here.

Layout (with the default empty root):

    agents/<agent_id>/inbox        directed messages
    agents/<agent_id>/presence     retained online/offline, also the LWT
    agents/broadcast               bus-wide fan-out
    swarmbus/registry/<agent_id>   retained registry record + heartbeat

A non-empty ``root`` prefixes the agent and registry trees, e.g.
``TopicMap(root="foo")`` yields ``foo/agents/alice/inbox`` and
``foo/swarmbus/registry/alice``.

``broadcast`` is DELIBERATELY NOT ROOTED. Broadcasts are bus-wide by
design: every agent on the broker sees them regardless of which namespace
it lives under. Do not "fix" this asymmetry.

Parsing rules
-------------
Every parser here RAISES ``ValueError`` on a topic that does not match the
expected shape. This is not defensive padding -- it is the whole point of
the module. The code this replaced used
``topic.removeprefix("agents/").removesuffix("/presence")``, and
``str.removeprefix`` returns the string UNCHANGED when the prefix is
absent, so a non-matching topic yielded a plausible-looking wrong agent id
instead of an error. With a root in play, ``foo/agents/x/presence`` would
have parsed to an agent id of ``foo/agents/x`` -- silently, forever.
See the comments on the parsers below before touching them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .message import _validate_agent_id

#: Tree holding per-agent inbox and presence topics.
AGENTS_SEGMENT = "agents"
#: Tree holding the retained agent registry.
REGISTRY_SEGMENT = "swarmbus/registry"
#: Leaf of a per-agent inbox topic.
INBOX_LEAF = "inbox"
#: Leaf of a per-agent presence topic.
PRESENCE_LEAF = "presence"
#: The ``to`` sentinel on a message envelope that means "everyone".
BROADCAST_AGENT_ID = "broadcast"
#: Bus-wide broadcast topic. Never carries a root -- see the module docstring.
BROADCAST_TOPIC = f"{AGENTS_SEGMENT}/{BROADCAST_AGENT_ID}"
#: MQTT single-level wildcard, matching exactly one topic segment.
SINGLE_LEVEL_WILDCARD = "+"
#: Broker-reserved topic prefix ($SYS and friends). Publishes to it are
#: dropped by the broker without an error to the client.
RESERVED_PREFIX = "$"
#: An empty topic segment. Legal MQTT, and a DIFFERENT topic from the
#: same path without it -- which is exactly why a root must not contain one.
EMPTY_SEGMENT = "//"

_MQTT_WILDCARDS = (SINGLE_LEVEL_WILDCARD, "#")


def _one_agent_segment(candidate: str, *, prefix: str, topic: str) -> str:
    """Extract exactly one agent-id segment sitting directly under ``prefix``.

    ``candidate`` is ``topic`` with any trailing leaf already stripped;
    ``topic`` is carried through only so error messages name the real topic.

    Raises ValueError unless ``candidate`` is ``<prefix>/<one-segment>``.
    A passthrough here (returning the input when the prefix is absent) is
    how the old presence parser silently invented agent ids -- keep it
    raising.
    """
    head = f"{prefix}/"
    if not candidate.startswith(head):
        raise ValueError(f"topic {topic!r} is not under {prefix!r}")
    agent_id = candidate[len(head):]
    if "/" in agent_id or not agent_id:
        raise ValueError(f"topic {topic!r} does not identify one agent")
    return _validate_agent_id(agent_id)


@dataclass(frozen=True)
class TopicMap:
    """Builds and parses every swarmbus topic, under an optional root.

    ``root`` is the namespace prefix for the agent and registry trees. It
    defaults to the empty string, which reproduces today's topics
    byte-for-byte. Leading/trailing slashes and surrounding whitespace are
    stripped; MQTT wildcards are rejected, because a wildcard in a root
    would turn every publish into an invalid topic.

    Instances are frozen and cheap -- hold one per client and reuse it.
    """

    root: str = ""

    def __post_init__(self) -> None:
        # ONE strip pass over BOTH character classes, not .strip() then
        # .strip("/"). Chained strips never remove the whitespace that
        # slash-stripping exposes: " / foo / " -> "/ foo /" -> " foo ".
        # MQTT permits spaces in topic names, so that surviving space makes
        # a namespace that is silently DIFFERENT from "foo" with no error
        # at either end -- the exact class this normalisation exists to
        # prevent. Found 2026-09-26 by a subagent reviewing the original
        # chained form.
        normalized = self.root.strip(" \t\n\r/")
        for wildcard in _MQTT_WILDCARDS:
            if wildcard in normalized:
                raise ValueError(
                    f"topic root {self.root!r} may not contain the MQTT "
                    f"wildcard {wildcard!r}"
                )
        # Every rejection below is a SILENT failure if allowed through --
        # the broker accepts the publish and simply routes it somewhere
        # nobody is listening, which is the whole failure class this
        # class exists to make impossible.
        if normalized.startswith(RESERVED_PREFIX):
            # $SYS and friends are broker-reserved. mosquitto drops
            # publishes there without telling the client.
            raise ValueError(
                f"topic root {self.root!r} may not start with "
                f"{RESERVED_PREFIX!r}: that namespace is reserved for the "
                f"broker and publishes to it are silently discarded"
            )
        if EMPTY_SEGMENT in normalized:
            # `foo//agents` is LEGAL MQTT and is a different topic from
            # `foo/agents`, so a stray double slash splits the fleet in
            # a way no error will ever surface.
            raise ValueError(
                f"topic root {self.root!r} may not contain an empty "
                f"segment ({EMPTY_SEGMENT!r}): it is legal MQTT but "
                f"addresses a different namespace than the same root "
                f"without it"
            )
        object.__setattr__(self, "root", normalized)

    # ------------------------------------------------------------------
    # prefixes
    # ------------------------------------------------------------------

    @property
    def agents_prefix(self) -> str:
        """``agents`` tree, rooted. Parent of inbox and presence topics."""
        return self._rooted(AGENTS_SEGMENT)

    @property
    def registry_prefix(self) -> str:
        """``swarmbus/registry`` tree, rooted."""
        return self._rooted(REGISTRY_SEGMENT)

    def _rooted(self, segment: str) -> str:
        return f"{self.root}/{segment}" if self.root else segment

    # ------------------------------------------------------------------
    # builders
    #
    # These do NOT validate the agent id. Every current caller already
    # validated it (AgentBus.__init__ and AgentMessage's `to`/`from`
    # validators run _validate_agent_id), and adding a
    # second gate here would change behaviour rather than centralise it.
    # ------------------------------------------------------------------

    def inbox(self, agent_id: str) -> str:
        """Directed-message topic for one agent."""
        return f"{self.agents_prefix}/{agent_id}/{INBOX_LEAF}"

    def presence(self, agent_id: str) -> str:
        """Retained presence topic for one agent (also used as the LWT)."""
        return f"{self.agents_prefix}/{agent_id}/{PRESENCE_LEAF}"

    def registry(self, agent_id: str) -> str:
        """Retained registry/heartbeat topic for one agent."""
        return f"{self.registry_prefix}/{agent_id}"

    @property
    def broadcast(self) -> str:
        """Bus-wide broadcast topic.

        Ignores ``root`` on purpose: broadcasts cross namespaces so agents
        under different roots still hear each other. See module docstring.
        """
        return BROADCAST_TOPIC

    def route(self, to: str) -> str:
        """Topic a message addressed to ``to`` should be published on.

        ``to="broadcast"`` is the reserved sentinel for fan-out; anything
        else is a directed inbox delivery.
        """
        return self.broadcast if to == BROADCAST_AGENT_ID else self.inbox(to)

    # ------------------------------------------------------------------
    # subscribe filters
    # ------------------------------------------------------------------

    def any_presence_filter(self) -> str:
        """Subscription matching every agent's presence topic."""
        return f"{self.agents_prefix}/{SINGLE_LEVEL_WILDCARD}/{PRESENCE_LEAF}"

    def any_registry_filter(self) -> str:
        """Subscription matching every agent's registry topic."""
        return f"{self.registry_prefix}/{SINGLE_LEVEL_WILDCARD}"

    # ------------------------------------------------------------------
    # predicates
    #
    # Deliberately looser than the parsers: they answer "which tree did
    # this arrive on" for dispatch, and preserve the exact startswith /
    # endswith semantics the runtime used before this module existed. A
    # topic can therefore pass a predicate and still fail its parser (e.g.
    # `agents/a/b/presence`, which the `agents/+/presence` subscription
    # cannot actually deliver) -- that raise is intended.
    # ------------------------------------------------------------------

    def is_registry_topic(self, topic: str) -> bool:
        """True when ``topic`` lives in this map's registry tree."""
        return topic.startswith(f"{self.registry_prefix}/")

    def is_presence_topic(self, topic: str) -> bool:
        """True when ``topic`` is a presence topic in this map's agent tree."""
        return topic.startswith(f"{self.agents_prefix}/") and topic.endswith(
            f"/{PRESENCE_LEAF}"
        )

    def is_message_topic(self, topic: str, *, agent_id: str) -> bool:
        """True when ``topic`` carries a message envelope for ``agent_id``."""
        return topic in (self.inbox(agent_id), self.broadcast)

    # ------------------------------------------------------------------
    # parsers -- all of these RAISE ValueError on a shape mismatch
    # ------------------------------------------------------------------

    def registry_agent(self, topic: str) -> str:
        """Agent id owning ``topic``, which must be a registry topic.

        Raises ValueError otherwise. Callers compare this against the
        agent_id inside the payload, so a lenient parse here would let a
        record claim an identity the topic never granted it.
        """
        return _one_agent_segment(
            topic, prefix=self.registry_prefix, topic=topic
        )

    def presence_agent(self, topic: str) -> str:
        """Agent id owning ``topic``, which must be a presence topic.

        Raises ValueError otherwise -- both when the leaf is wrong and when
        the topic is outside this map's agent tree. The prefix check is the
        half that used to be missing: ``removeprefix`` let `x/presence` and
        `<root>/agents/x/presence` through with garbage agent ids.
        """
        leaf = f"/{PRESENCE_LEAF}"
        if not topic.endswith(leaf):
            raise ValueError(f"topic {topic!r} is not a presence topic")
        return _one_agent_segment(
            topic[: -len(leaf)], prefix=self.agents_prefix, topic=topic
        )


#: Shared root-less map. Importing this keeps today's topics byte-identical;
#: a rooted deployment replaces it with ``TopicMap(root=...)`` at the seam in
#: each client's constructor.
DEFAULT_TOPICS = TopicMap()
