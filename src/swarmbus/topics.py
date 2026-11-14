"""Build and parse swarmbus topics and per-root runtime names.

A non-empty root prefixes the agent and registry trees. Broadcast remains
bus-wide. An agent whose id is ``<a>__<b>`` may use the two-level address
``<a>/<b>``. Parsers raise ``ValueError`` when a topic has the wrong shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .message import _validate_agent_id

#: Tree holding per-agent inbox and presence topics.
AGENTS_SEGMENT = "agents"
#: Tree holding the retained agent registry.
REGISTRY_SEGMENT = "swarmbus/registry"
#: Leaf of a per-agent inbox topic.
INBOX_LEAF = "inbox"
#: Leaf of a per-agent presence topic.
PRESENCE_LEAF = "presence"
#: Separates an agent id from its root in an agent scope. Cannot occur in a
#: valid agent id, so a scope splits back into its two parts one way only.
SCOPE_SEPARATOR = "@"
#: Prefix of a swarmbus systemd user unit name.
UNIT_PREFIX = "swarmbus-"
#: Suffix of a swarmbus systemd user unit name.
UNIT_SUFFIX = ".service"
#: Separates agent id from escaped root in a systemd-safe token. Unlike
#: ``@`` and ``%``, it has no special meaning in a unit or its directives.
UNIT_SEPARATOR = ":"
#: systemd's maximum computed unit-name length, in bytes.
SYSTEMD_UNIT_NAME_MAX_BYTES = 255
#: swarmbus's own MQTT client-id bound; separate from systemd's limit.
SWARMBUS_CLIENT_ID_MAX_BYTES = 255
#: Suffix of the file a swarmbus systemd unit appends its output to.
LOG_SUFFIX = ".log"
#: Suffix of the default inbox file the listener daemon file-bridges to.
INBOX_SUFFIX = "-inbox.md"
#: What a root keeps verbatim in a unit name. Everything else is escaped.
_UNIT_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-"
)
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
#: Joins the two levels of a two-level topic address into one agent id.
NAMESPACE_SEPARATOR = "__"

_MQTT_WILDCARDS = (SINGLE_LEVEL_WILDCARD, "#")


def _unit_escape(root: str) -> str:
    """Encode ``root`` injectively as one systemd-safe token."""
    out: list[str] = []
    for char in root:
        if char in _UNIT_SAFE:
            out.append(char)
        else:
            out.extend(
                f"{UNIT_SEPARATOR}{byte:02x}" for byte in char.encode("utf-8")
            )
    return "".join(out)


def agent_id_for_topic_address(topic_address: str) -> str:
    """Agent id that owns a one- or two-level topic address."""
    levels = topic_address.split("/")
    if len(levels) not in (1, 2) or not all(levels):
        raise ValueError(
            f"topic address {topic_address!r} must have one or two levels"
        )
    for level in levels:
        _validate_agent_id(level)
    agent_id = _validate_agent_id(NAMESPACE_SEPARATOR.join(levels))
    if len(levels) == 2 and agent_id.split(NAMESPACE_SEPARATOR) != levels:
        raise ValueError(
            f"topic address {topic_address!r} is ambiguous: agent id "
            f"{agent_id!r} does not split back into its two levels at "
            f"{NAMESPACE_SEPARATOR!r}"
        )
    return agent_id


def two_level_topic_address(agent_id: str) -> str | None:
    """The two-level address an agent id may live at, or None."""
    levels = agent_id.split(NAMESPACE_SEPARATOR)
    if len(levels) != 2 or not all(levels):
        return None
    return "/".join(levels)


def _bound_topic_address(agent_id: str, topic_address: str | None) -> str:
    """The topic levels an agent's topics are built from."""
    if topic_address is None:
        return agent_id
    if agent_id_for_topic_address(topic_address) != agent_id:
        raise ValueError(
            f"topic address {topic_address!r} does not belong to agent "
            f"{agent_id!r}"
        )
    return topic_address


def live_session_client_id(
    agent_id: str, topic_address: str | None = None
) -> str | None:
    """MQTT client identifier a non-persistent session must use, or None."""
    address = _bound_topic_address(agent_id, topic_address)
    if "/" in address:
        return address.rsplit("/", 1)[1]
    return None


def _agent_topic_address(candidate: str, *, prefix: str, topic: str) -> str:
    """Extract the agent address sitting directly under ``prefix``.

    ``candidate`` is ``topic`` with any trailing leaf already stripped;
    ``topic`` is carried through only so error messages name the real topic.

    Raises ValueError unless ``candidate`` is ``<prefix>/<address>`` with a
    valid one- or two-level address. A passthrough here (returning the input
    when the prefix is absent) is how the old presence parser silently
    invented agent ids -- keep it raising.
    """
    head = f"{prefix}/"
    if not candidate.startswith(head):
        raise ValueError(f"topic {topic!r} is not under {prefix!r}")
    address = candidate[len(head):]
    try:
        agent_id_for_topic_address(address)
    except ValueError as exc:
        raise ValueError(
            f"topic {topic!r} does not identify one agent: {exc}"
        ) from exc
    return address


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
    # A supplied ``topic_address`` IS validated: it must be the agent id's
    # own address, or a caller could build another agent's topics.
    # ------------------------------------------------------------------

    def inbox(
        self, agent_id: str, *, topic_address: str | None = None
    ) -> str:
        """Directed-message topic for one agent."""
        address = _bound_topic_address(agent_id, topic_address)
        return f"{self.agents_prefix}/{address}/{INBOX_LEAF}"

    def presence(
        self, agent_id: str, *, topic_address: str | None = None
    ) -> str:
        """Retained presence topic for one agent (also used as the LWT)."""
        address = _bound_topic_address(agent_id, topic_address)
        return f"{self.agents_prefix}/{address}/{PRESENCE_LEAF}"

    def registry(
        self, agent_id: str, *, topic_address: str | None = None
    ) -> str:
        """Retained registry/heartbeat topic for one agent."""
        address = _bound_topic_address(agent_id, topic_address)
        return f"{self.registry_prefix}/{address}"

    @property
    def broadcast(self) -> str:
        """Bus-wide broadcast topic.

        Ignores ``root`` on purpose: broadcasts cross namespaces so agents
        under different roots still hear each other. See module docstring.
        """
        return BROADCAST_TOPIC

    def route(self, to: str, *, topic_address: str | None = None) -> str:
        """Topic a message addressed to ``to`` should be published on.

        ``to="broadcast"`` is the reserved sentinel for fan-out; anything
        else is a directed inbox delivery.
        """
        if to == BROADCAST_AGENT_ID:
            return self.broadcast
        return self.inbox(to, topic_address=topic_address)

    # ------------------------------------------------------------------
    # agent names
    # ------------------------------------------------------------------

    def agent_scope(self, agent_id: str) -> str:
        """Name of one agent on this bus, for anything keyed per agent.

        The bare ``agent_id`` under the empty root, so an unrooted
        deployment keeps every name it has today. Otherwise the root is
        percent-encoded with nothing left safe, which keeps the result a
        single token -- usable as a file name and as one MQTT topic level --
        and keeps the split back into agent id and root unambiguous.
        """
        if not self.root:
            return agent_id
        return f"{agent_id}{SCOPE_SEPARATOR}{quote(self.root, safe='')}"

    def session_client_id(self, agent_id: str) -> str:
        """MQTT client identifier of this agent's persistent session here.

        One identifier is one broker session: without the root, the same
        agent id persistent under two roots would be a single session, and
        each receiver would disconnect the other.

        Raises ValueError past ``SWARMBUS_CLIENT_ID_MAX_BYTES``. That bound
        is swarmbus's own, not something MQTT or a broker imposes, so the
        error says so -- a message blaming the protocol would send a reader
        looking for a rule that is not in it.
        """
        client_id = f"swarmbus-{self.agent_scope(agent_id)}"
        encoded = len(client_id.encode("utf-8"))
        if encoded > SWARMBUS_CLIENT_ID_MAX_BYTES:
            raise ValueError(
                f"client id for agent {agent_id!r} under topic root "
                f"{self.root!r} is {encoded} bytes, past swarmbus's own "
                f"limit of {SWARMBUS_CLIENT_ID_MAX_BYTES} bytes for a client "
                f"id: {client_id}"
            )
        return client_id

    def unit_scope(self, agent_id: str) -> str:
        """Name of one agent on this root, as one systemd-safe token.

        The bare ``agent_id`` under the empty root; otherwise the escaped
        root after ``UNIT_SEPARATOR``, which no agent id can contain. The
        unit name, the unit's log file and the default inbox file are all
        built from this one token, so each splits back into (agent id, root)
        the same way and none of them carries a character systemd expands.
        """
        if not self.root:
            return agent_id
        return f"{agent_id}{UNIT_SEPARATOR}{_unit_escape(self.root)}"

    def systemd_unit(self, agent_id: str) -> str:
        """Return the root-specific unit name, refusing names systemd cannot hold."""
        name = f"{UNIT_PREFIX}{self.unit_scope(agent_id)}{UNIT_SUFFIX}"
        encoded = len(name.encode("utf-8"))
        if encoded > SYSTEMD_UNIT_NAME_MAX_BYTES:
            raise ValueError(
                f"systemd unit name for agent {agent_id!r} under topic root "
                f"{self.root!r} is {encoded} bytes, past systemd's maximum "
                f"unit name length of {SYSTEMD_UNIT_NAME_MAX_BYTES} bytes, "
                f"which systemd itself refuses: {name}"
            )
        return name

    def unit_log_name(self, agent_id: str) -> str:
        """Return the log file name keyed to this root's unit."""
        return f"{UNIT_PREFIX}{self.unit_scope(agent_id)}{LOG_SUFFIX}"

    def default_inbox_name(self, agent_id: str) -> str:
        """Return the default inbox file name keyed to this root."""
        return f"{self.unit_scope(agent_id)}{INBOX_SUFFIX}"

    def unit_agent(self, unit_name: str) -> str:
        """Agent id owning ``unit_name``, which must be a unit on this root.

        Raises ValueError for a unit of another root or another program. A
        daemon on another root is another swarm's daemon, and an agent id
        taken out of its unit name says nothing about which swarm that is.
        """
        if not unit_name.startswith(UNIT_PREFIX) or not unit_name.endswith(
            UNIT_SUFFIX
        ):
            raise ValueError(f"unit {unit_name!r} is not a swarmbus unit")
        body = unit_name[len(UNIT_PREFIX): -len(UNIT_SUFFIX)]
        agent_id, separator, root_token = body.partition(UNIT_SEPARATOR)
        if bool(separator) != bool(self.root) or root_token != (
            _unit_escape(self.root) if self.root else ""
        ):
            raise ValueError(
                f"unit {unit_name!r} is not under topic root "
                f"{self.root or '(none)'!r}"
            )
        return _validate_agent_id(agent_id)

    # ------------------------------------------------------------------
    # subscribe filters
    # ------------------------------------------------------------------

    def any_presence_filter(self) -> str:
        """Subscription matching every one-level agent's presence topic."""
        return f"{self.agents_prefix}/{SINGLE_LEVEL_WILDCARD}/{PRESENCE_LEAF}"

    def any_registry_filter(self) -> str:
        """Subscription matching every one-level agent's registry topic."""
        return f"{self.registry_prefix}/{SINGLE_LEVEL_WILDCARD}"

    def presence_filters(self) -> tuple[str, str]:
        """Subscriptions matching presence at one- and two-level addresses."""
        return (
            self.any_presence_filter(),
            f"{self.agents_prefix}/{SINGLE_LEVEL_WILDCARD}/"
            f"{SINGLE_LEVEL_WILDCARD}/{PRESENCE_LEAF}",
        )

    def registry_filters(self) -> tuple[str, str]:
        """Subscriptions matching registry at one- and two-level addresses."""
        return (
            self.any_registry_filter(),
            f"{self.registry_prefix}/{SINGLE_LEVEL_WILDCARD}/"
            f"{SINGLE_LEVEL_WILDCARD}",
        )

    # ------------------------------------------------------------------
    # predicates
    #
    # Deliberately looser than the parsers: they answer "which tree did
    # this arrive on" for dispatch, and preserve the exact startswith /
    # endswith semantics the runtime used before this module existed. A
    # topic can therefore pass a predicate and still fail its parser (e.g.
    # `agents/a/b/c/presence`, which no subscription here can actually
    # deliver) -- that raise is intended.
    # ------------------------------------------------------------------

    def is_registry_topic(self, topic: str) -> bool:
        """True when ``topic`` lives in this map's registry tree."""
        return topic.startswith(f"{self.registry_prefix}/")

    def is_presence_topic(self, topic: str) -> bool:
        """True when ``topic`` is a presence topic in this map's agent tree."""
        return topic.startswith(f"{self.agents_prefix}/") and topic.endswith(
            f"/{PRESENCE_LEAF}"
        )

    def is_message_topic(
        self,
        topic: str,
        *,
        agent_id: str,
        topic_address: str | None = None,
    ) -> bool:
        """True when ``topic`` carries a message envelope for ``agent_id``."""
        return topic in (
            self.inbox(agent_id, topic_address=topic_address),
            self.broadcast,
        )

    # ------------------------------------------------------------------
    # parsers -- all of these RAISE ValueError on a shape mismatch
    # ------------------------------------------------------------------

    def registry_address(self, topic: str) -> str:
        """Agent address owning ``topic``, which must be a registry topic."""
        return _agent_topic_address(
            topic, prefix=self.registry_prefix, topic=topic
        )

    def registry_agent(self, topic: str) -> str:
        """Agent id owning ``topic``, which must be a registry topic.

        Raises ValueError otherwise. Callers compare this against the
        agent_id inside the payload, so a lenient parse here would let a
        record claim an identity the topic never granted it.
        """
        return agent_id_for_topic_address(self.registry_address(topic))

    def presence_address(self, topic: str) -> str:
        """Agent address owning ``topic``, which must be a presence topic.

        Raises ValueError otherwise -- both when the leaf is wrong and when
        the topic is outside this map's agent tree. The prefix check is the
        half that used to be missing: ``removeprefix`` let `x/presence` and
        `<root>/agents/x/presence` through with garbage agent ids.
        """
        leaf = f"/{PRESENCE_LEAF}"
        if not topic.endswith(leaf):
            raise ValueError(f"topic {topic!r} is not a presence topic")
        return _agent_topic_address(
            topic[: -len(leaf)], prefix=self.agents_prefix, topic=topic
        )

    def presence_agent(self, topic: str) -> str:
        """Agent id owning ``topic``, which must be a presence topic."""
        return agent_id_for_topic_address(self.presence_address(topic))


#: Shared root-less map. Importing this keeps today's topics byte-identical;
#: a rooted deployment replaces it with ``TopicMap(root=...)`` at the seam in
#: each client's constructor.
DEFAULT_TOPICS = TopicMap()
