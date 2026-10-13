"""Topic construction and parsing.

The builder tests pin the wire format: with no root, every string here is
byte-identical to the literals that used to be scattered across bus.py.
The parser tests are the ones that matter --
they pin that a topic of the wrong shape RAISES instead of yielding an
invented agent id, which is what `str.removeprefix` used to do.
"""

import pytest

from swarmbus.topics import (
    BROADCAST_TOPIC,
    DEFAULT_TOPICS,
    EMPTY_SEGMENT,
    RESERVED_PREFIX,
    SINGLE_LEVEL_WILDCARD,
    TopicMap,
)

ROOTED = TopicMap(root="foo")


# ---------------------------------------------------------------------------
# builders -- no root: byte-identical to the pre-refactor literals
# ---------------------------------------------------------------------------


def test_unrooted_builders_match_the_legacy_literals():
    topics = TopicMap()

    assert topics.inbox("wren") == "agents/wren/inbox"
    assert topics.presence("wren") == "agents/wren/presence"
    assert topics.registry("wren") == "swarmbus/registry/wren"
    assert topics.broadcast == "agents/broadcast"


def test_unrooted_filters_match_the_legacy_subscriptions():
    topics = TopicMap()

    assert topics.any_presence_filter() == "agents/+/presence"
    assert topics.any_registry_filter() == "swarmbus/registry/+"


def test_default_topics_is_rootless():
    assert DEFAULT_TOPICS == TopicMap()
    assert DEFAULT_TOPICS.root == ""


def test_route_picks_broadcast_only_for_the_reserved_sentinel():
    topics = TopicMap()

    assert topics.route("broadcast") == "agents/broadcast"
    assert topics.route("wren") == "agents/wren/inbox"


# ---------------------------------------------------------------------------
# builders -- with a root
# ---------------------------------------------------------------------------


def test_rooted_builders_prefix_the_agent_and_registry_trees():
    assert ROOTED.inbox("wren") == "foo/agents/wren/inbox"
    assert ROOTED.presence("wren") == "foo/agents/wren/presence"
    assert ROOTED.registry("wren") == "foo/swarmbus/registry/wren"
    assert ROOTED.any_presence_filter() == "foo/agents/+/presence"
    assert ROOTED.any_registry_filter() == "foo/swarmbus/registry/+"


def test_broadcast_never_picks_up_the_root():
    """Broadcasts are bus-wide by design and must cross namespaces."""
    assert ROOTED.broadcast == "agents/broadcast"
    assert ROOTED.broadcast == BROADCAST_TOPIC
    assert ROOTED.broadcast == TopicMap().broadcast
    assert ROOTED.route("broadcast") == "agents/broadcast"


def test_root_normalisation_strips_slashes_and_whitespace():
    for raw in ("foo", "/foo", "foo/", "/foo/", "  foo  "):
        assert TopicMap(root=raw).inbox("wren") == "foo/agents/wren/inbox"


def test_root_rejects_mqtt_wildcards():
    for wildcard_root in ("+", "foo/+", "#", "foo/#"):
        with pytest.raises(ValueError, match="wildcard"):
            TopicMap(root=wildcard_root)


def test_root_rejects_the_broker_reserved_dollar_prefix():
    """``$SYS`` and friends belong to the broker.

    mosquitto accepts a publish under ``$`` and drops it without telling
    the client, so a root that starts with it produces a fleet that
    publishes into nothing and reports no error anywhere.
    """
    for reserved_root in ("$", "$SYS", "$SYS/foo", "/$SYS/", "  $foo  "):
        with pytest.raises(ValueError, match="reserved"):
            TopicMap(root=reserved_root)


def test_root_rejects_an_empty_segment():
    """``foo//agents`` is legal MQTT and a DIFFERENT topic.

    Nothing rejects it on the wire, so a stray double slash splits the
    fleet into two namespaces that can never hear each other and never
    complain about it.
    """
    for doubled_root in ("foo//deep", "a//b/c", "foo///x"):
        with pytest.raises(ValueError, match="empty segment"):
            TopicMap(root=doubled_root)


def test_leading_and_trailing_slashes_are_stripped_not_treated_as_empty_segments():
    """The empty-segment rejection must not swallow the normal forms.

    ``//foo//`` strips down to ``foo``; only an INNER doubled slash is
    a second namespace.
    """
    assert TopicMap(root="//foo//").root == "foo"
    assert TopicMap(root="//foo//").inbox("wren") == "foo/agents/wren/inbox"


@pytest.mark.parametrize(
    "bad_root, construct",
    [
        ("foo/+", SINGLE_LEVEL_WILDCARD),
        ("foo/#", "#"),
        ("$SYS/foo", RESERVED_PREFIX),
        ("foo//deep", EMPTY_SEGMENT),
    ],
)
def test_root_rejection_message_names_the_offending_construct(bad_root, construct):
    """An operator reading the traceback must see WHICH character broke it.

    "invalid topic root" sends someone hunting; naming the construct and
    quoting the root they passed does not. Expectations come from the
    module's own constants, so renaming one moves the test with it.
    """
    with pytest.raises(ValueError) as excinfo:
        TopicMap(root=bad_root)
    message = str(excinfo.value)
    assert repr(construct) in message, message
    assert repr(bad_root) in message, message


def test_root_normalisation_is_one_strip_pass_regression_inner_space_survived_chained_strips():
    """REGRESSION (found 2026-09-26): chained strips left an inner space.

    The original normalisation was ``.strip().strip("/")``, which cannot
    remove whitespace that slash-stripping only just exposed:
    ``" / foo / "`` -> ``"/ foo /"`` -> ``" foo "``. MQTT permits
    spaces in topic names, so that surviving space yielded a namespace
    silently DIFFERENT from ``foo`` -- ``" foo/agents/wren/inbox"`` --
    with no error at the publisher or the subscriber. Exactly the class
    of failure this normalisation exists to prevent.

    One strip pass over both character classes is the fix, so the assert
    is on the absence of any surviving whitespace, not just on equality.
    """
    for raw in (" / foo / ", "/ foo /", "\t/ foo /\n", "  //  foo  //  "):
        root = TopicMap(root=raw).root
        assert root == "foo", (raw, root)
        assert " " not in root, (raw, root)
        assert root == root.strip(), (raw, root)
        assert TopicMap(root=raw).inbox("wren") == "foo/agents/wren/inbox"


# ---------------------------------------------------------------------------
# parsers -- the happy path
# ---------------------------------------------------------------------------


def test_parsers_round_trip_their_builders():
    for topics in (TopicMap(), ROOTED):
        assert topics.presence_agent(topics.presence("wren")) == "wren"
        assert topics.registry_agent(topics.registry("wren")) == "wren"


# ---------------------------------------------------------------------------
# parsers -- the regression that matters: mismatches RAISE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic",
    [
        # The removeprefix passthrough bug: no `agents/` prefix at all.
        # This used to parse to "wren".
        "wren/presence",
        # A root that this map does not know about. This used to parse to
        # "foo/agents/wren" -- silently, forever.
        "foo/agents/wren/presence",
        # Right tree, wrong leaf.
        "agents/wren/inbox",
        "agents/wren",
        # Right tree, wrong depth.
        "agents/wren/extra/presence",
        # Empty agent segment.
        "agents//presence",
        "agents/presence",
        # Registry topic offered to the presence parser.
        "swarmbus/registry/wren",
        # Not an agent id at all.
        "agents/Wren/presence",
        "",
    ],
)
def test_presence_parser_raises_on_a_mismatched_topic(topic):
    with pytest.raises(ValueError):
        TopicMap().presence_agent(topic)


@pytest.mark.parametrize(
    "topic",
    [
        "wren",
        "foo/swarmbus/registry/wren",
        "swarmbus/registry/wren/extra",
        "swarmbus/registry/",
        "swarmbus/registry",
        "agents/wren/presence",
        "swarmbus/registry/Wren",
        "",
    ],
)
def test_registry_parser_raises_on_a_mismatched_topic(topic):
    with pytest.raises(ValueError):
        TopicMap().registry_agent(topic)


def test_rooted_parsers_reject_unrooted_topics():
    """A rooted map must not accept the bus-wide legacy shape."""
    with pytest.raises(ValueError, match="is not under"):
        ROOTED.presence_agent("agents/wren/presence")
    with pytest.raises(ValueError, match="is not under"):
        ROOTED.registry_agent("swarmbus/registry/wren")


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------


def test_predicates_classify_the_legacy_trees():
    topics = TopicMap()

    assert topics.is_presence_topic("agents/wren/presence") is True
    assert topics.is_presence_topic("agents/wren/inbox") is False
    assert topics.is_presence_topic("swarmbus/registry/wren") is False

    assert topics.is_registry_topic("swarmbus/registry/wren") is True
    assert topics.is_registry_topic("agents/wren/presence") is False


def test_predicates_follow_the_root():
    assert ROOTED.is_presence_topic("foo/agents/wren/presence") is True
    assert ROOTED.is_presence_topic("agents/wren/presence") is False
    assert ROOTED.is_registry_topic("foo/swarmbus/registry/wren") is True
    assert ROOTED.is_registry_topic("swarmbus/registry/wren") is False


def test_message_topic_predicate_covers_own_inbox_and_broadcast():
    topics = TopicMap()

    assert topics.is_message_topic("agents/wren/inbox", agent_id="wren") is True
    assert topics.is_message_topic("agents/broadcast", agent_id="wren") is True
    assert topics.is_message_topic("agents/foo/inbox", agent_id="wren") is False
    assert (
        topics.is_message_topic("agents/wren/presence", agent_id="wren") is False
    )


def test_rooted_message_predicate_still_accepts_unrooted_broadcast():
    assert ROOTED.is_message_topic("agents/broadcast", agent_id="wren") is True
    assert ROOTED.is_message_topic("foo/agents/wren/inbox", agent_id="wren") is True
