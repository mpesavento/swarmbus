"""Topic construction and parsing.

The builder tests pin the wire format: with no root, every string here is
byte-identical to the literals that used to be scattered across bus.py.
The parser tests are the ones that matter --
they pin that a topic of the wrong shape RAISES instead of yielding an
invented agent id, which is what `str.removeprefix` used to do.
"""

from itertools import combinations

import pytest

from swarmbus.topics import (
    BROADCAST_TOPIC,
    DEFAULT_TOPICS,
    EMPTY_SEGMENT,
    RESERVED_PREFIX,
    SCOPE_SEPARATOR,
    SINGLE_LEVEL_WILDCARD,
    TopicMap,
    agent_id_for_topic_address,
    live_session_client_id,
    two_level_topic_address,
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


# ---------------------------------------------------------------------------
# agent names -- per-agent scope and persistent-session client identifier
# ---------------------------------------------------------------------------


def test_unrooted_agent_names_are_the_bare_agent_id():
    topics = TopicMap()

    assert topics.agent_scope("wren") == "wren"
    assert topics.session_client_id("wren") == "swarmbus-wren"


def test_rooted_agent_names_carry_the_root():
    topics = TopicMap(root="fleet")

    assert topics.agent_scope("wren") == "wren@fleet"
    assert topics.session_client_id("wren") == "swarmbus-wren@fleet"


def test_a_multi_level_root_stays_one_token():
    topics = TopicMap(root="fleet/prod")

    assert topics.agent_scope("wren") == "wren@fleet%2Fprod"
    assert topics.session_client_id("wren") == "swarmbus-wren@fleet%2Fprod"


def test_a_root_cannot_forge_the_scope_separator():
    """An encoded ``@`` keeps the split back into (agent id, root) unique."""
    topics = TopicMap(root="x@y")

    assert topics.agent_scope("wren") == "wren@x%40y"
    assert topics.session_client_id("wren") == "swarmbus-wren@x%40y"


#: Roots that a substitution-based sanitizer folds together. ``a/b`` against
#: ``a%2Fb`` defeats ``replace("/", "%2F")``; against ``a_b`` and ``a b`` it
#: defeats the ``_`` substitutions. Percent-encoding escapes ``%`` itself, so
#: the encoding stays injective over all of them.
_COLLIDING_ROOTS = ("a/b", "a%2Fb", "a_b", "a b", "a%5Fb", "a/b/")


def test_distinct_roots_never_share_a_scope_or_a_session():
    """Two roots that differ at all must not land on one db, socket or session.

    A collision here is the bug rooting the paths exists to remove: it would
    merge two swarms' inbox, holds and rate-limit state back into one file and
    put two receivers back on one broker session.
    """
    roots = [r for r in _COLLIDING_ROOTS if TopicMap(root=r).root]
    normalized = {TopicMap(root=r).root for r in roots}
    # "a/b" and "a/b/" normalize to the same namespace, so they are the same
    # swarm and SHOULD share state. Only genuinely distinct roots are compared.
    assert len(normalized) < len(roots)

    scopes = {root: TopicMap(root=root).agent_scope("wren") for root in normalized}
    sessions = {
        root: TopicMap(root=root).session_client_id("wren") for root in normalized
    }

    for left, right in combinations(sorted(normalized), 2):
        assert scopes[left] != scopes[right], (
            f"roots {left!r} and {right!r} collide on scope {scopes[left]!r}"
        )
        assert sessions[left] != sessions[right], (
            f"roots {left!r} and {right!r} collide on session "
            f"{sessions[left]!r}"
        )

    # No root may reach the unrooted names an existing deployment owns.
    assert "wren" not in scopes.values()
    assert "swarmbus-wren" not in sessions.values()


# ---------------------------------------------------------------------------
# client identifier length -- swarmbus's own bound, not the protocol's
# ---------------------------------------------------------------------------


def test_a_client_id_past_swarmbuss_255_byte_limit_is_refused():
    """Refused where it is built, with its real byte count and the limit."""
    with pytest.raises(ValueError) as excinfo:
        TopicMap(root="x" * 300).session_client_id("wren")

    message = str(excinfo.value)
    # swarmbus- (9) + wren (4) + @ (1) + 300 encoded bytes
    assert "314" in message, message
    assert "255" in message, message


def test_the_client_id_limit_is_stated_as_swarmbuss_own():
    """255 here is a swarmbus sanity bound with NO basis in MQTT.

    MQTT 3.1.1 puts the protocol's own ceiling at 65535 bytes (§1.5.3), and
    [MQTT-3.1.3-5]'s "between 1 and 23 UTF-8 encoded bytes ... 0-9a-zA-Z" is
    a FLOOR on what a server must ALLOW, not a ceiling on an id. swarmbus
    passes that floor deliberately -- ``swarmbus-<id>`` is longer than 23
    for most agent ids and ``-`` is not in its character list at all -- so
    an error implying the protocol or the broker refuses this would send a
    reader hunting for a rule that does not exist.
    """
    with pytest.raises(ValueError) as excinfo:
        TopicMap(root="x" * 300).session_client_id("wren")
    message = str(excinfo.value)

    assert "swarmbus's own" in message, message
    # "23" is the portability floor this error must not invoke; the byte
    # counts in this message do not contain it, so its absence is the
    # absence of a reference to the floor.
    for misleading in ("protocol", "broker", "MQTT limit", "MQTT maximum", "23"):
        assert misleading not in message, (misleading, message)


def test_the_client_id_budget_leaves_181_bytes_for_an_encoded_root():
    """``swarmbus-`` (9) + agent id (64) + ``@`` (1) = 74 of the 255.

    Worst case percent-encoding is 3 bytes per source byte, so roughly 60
    characters of a root that encodes in full.
    """
    agent_id = "a" * 64
    assert len("swarmbus-" + agent_id + SCOPE_SEPARATOR) == 74

    # "{" is outside the unreserved set, so each one encodes to "%7B".
    fits = TopicMap(root="{" * 60).session_client_id(agent_id)
    assert len(fits.encode("utf-8")) == 254

    with pytest.raises(ValueError) as excinfo:
        TopicMap(root="{" * 61).session_client_id(agent_id)
    assert "257" in str(excinfo.value), str(excinfo.value)


def test_no_unrooted_client_id_can_reach_the_limit():
    """Bounded at 73 bytes by the agent-id charset, so no existing
    deployment's session can be refused."""
    assert len(TopicMap().session_client_id("a" * 64).encode("utf-8")) == 73


# ---------------------------------------------------------------------------
# systemd unit names
# ---------------------------------------------------------------------------

#: Everything systemd permits in a unit name. ``@`` is absent on purpose: it
#: separates a template from its instance, so ``swarmbus-wren@fleet.service``
#: is an instance of a template that does not exist and `systemctl enable`
#: fails on it. ``%`` is absent because it introduces a specifier, which
#: expands inside ExecStart, Environment= and StandardOutput=append:.
_UNIT_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    ":-_.\\"
)

#: Agent ids spanning the whole agent-id charset, including the ``-`` and
#: ``_`` that make those two characters unusable as the root separator.
_AGENT_IDS = ("wren", "a", "a-b", "a_b", "wren-fleet", "laptop-cc")


def test_the_unrooted_unit_name_is_the_one_already_installed():
    """The literal: an existing install must not need renaming."""
    assert TopicMap().systemd_unit("wren") == "swarmbus-wren.service"
    assert TopicMap().systemd_unit("laptop-cc") == "swarmbus-laptop-cc.service"


def test_rooted_unit_names_carry_the_root():
    assert TopicMap(root="fleet").systemd_unit("wren") == "swarmbus-wren:fleet.service"


def test_a_multi_level_root_stays_one_unit_name():
    """A unit name is also a file name, and ``/`` is legal in neither."""
    assert (
        TopicMap(root="fleet/prod").systemd_unit("wren")
        == "swarmbus-wren:fleet:2fprod.service"
    )


def test_a_unit_name_holds_only_characters_systemd_permits():
    """A root carries anything MQTT allows; a unit name cannot.

    ``@``, ``%`` and ``\\`` are the three that do damage rather than fail:
    the first makes a template instance, the second expands as a
    specifier, and the third is systemd's own escape marker (and sed's
    replacement escape, which is how the unit gets rendered).
    """
    for root in (*_COLLIDING_ROOTS, "x@y", "100%", "a\\b", "fle€t", "a:b"):
        name = TopicMap(root=root).systemd_unit("wren")
        assert set(name) <= _UNIT_NAME_CHARS, (
            root, name, sorted(set(name) - _UNIT_NAME_CHARS)
        )
        for dangerous in ("@", "%", "\\"):
            assert dangerous not in name, (root, name)


def test_distinct_roots_never_share_a_systemd_unit_name():
    """Sibling of the scope/session test: one unit name is one daemon.

    Two roots landing on one unit name means installing the second swarm
    overwrites the first swarm's unit, and the next `systemctl restart`
    moves a running daemon into the other namespace.
    """
    roots = [r for r in _COLLIDING_ROOTS if TopicMap(root=r).root]
    normalized = {TopicMap(root=r).root for r in roots}
    # "a/b" and "a/b/" normalize to the same namespace, so they are the same
    # swarm and SHOULD share a unit. Only genuinely distinct roots compare.
    assert len(normalized) < len(roots)

    units = {root: TopicMap(root=root).systemd_unit("wren") for root in normalized}

    for left, right in combinations(sorted(normalized), 2):
        assert units[left] != units[right], (
            f"roots {left!r} and {right!r} collide on unit {units[left]!r}"
        )

    # No rooted unit name may equal an unrooted one, for ANY agent id. Both
    # "-" and "_" are in the agent-id charset, so either as the separator
    # would let agent id "a-b" forge agent "a" under root "b".
    installed = {TopicMap().systemd_unit(agent_id) for agent_id in _AGENT_IDS}
    for agent_id in _AGENT_IDS:
        for root in normalized | {"b", "fleet"}:
            rooted = TopicMap(root=root).systemd_unit(agent_id)
            assert rooted not in installed, (agent_id, root, rooted)


def test_no_agent_id_can_forge_a_rooted_unit_name():
    """``--`` was the obvious separator and is forgeable from the left."""
    assert TopicMap().systemd_unit("a-b") != TopicMap(root="b").systemd_unit("a")
    assert TopicMap().systemd_unit("a_b") != TopicMap(root="b").systemd_unit("a")
    assert TopicMap().systemd_unit("a--b") != TopicMap(root="b").systemd_unit("a-")


# ---------------------------------------------------------------------------
# systemd unit name length -- systemd's documented maximum is 255 bytes
# ---------------------------------------------------------------------------


def test_a_unit_name_past_systemds_255_byte_maximum_is_refused():
    """The COMPUTED NAME is what systemd rejects, so that is what is measured.

    A root inside any budget of its own still makes a name systemd will not
    load once the prefix, the agent id and the suffix are on it. The error
    names the real byte count and the real limit, so an operator can see how
    far over they are instead of guessing.
    """
    with pytest.raises(ValueError) as excinfo:
        TopicMap(root="x" * 300).systemd_unit("wren")

    message = str(excinfo.value)
    # swarmbus- (9) + wren (4) + : (1) + 300 escaped bytes + .service (8)
    assert "322" in message, message
    assert "255" in message, message


def test_an_over_long_unit_name_is_neither_truncated_nor_hashed():
    """Either would put two distinct roots on one unit name.

    Injectivity is the property root-aware naming exists to protect, so a
    name that will not fit has no answer other than an error.
    """
    for root in ("x" * 300, "x" * 301, "y" * 300):
        with pytest.raises(ValueError):
            TopicMap(root=root).systemd_unit("wren")


def test_the_unit_name_budget_leaves_173_bytes_for_an_escaped_root():
    """``swarmbus-`` (9) + agent id (64) + ``:`` (1) + ``.service`` (8) = 82.

    That leaves 173 of systemd's 255 bytes for the escaped root, and worst
    case escaping is 3 bytes per source byte -- 57 source characters that
    escape in full.
    """
    agent_id = "a" * 64            # the longest id _validate_agent_id accepts
    assert len("swarmbus-" + agent_id + ":" + ".service") == 82

    # "~" is outside the unit-name charset, so every one of its bytes
    # escapes: 57 characters -> 171 bytes, 2 inside the budget.
    fits = TopicMap(root="~" * 57).systemd_unit(agent_id)
    assert len(fits.encode("utf-8")) == 253

    with pytest.raises(ValueError) as excinfo:
        TopicMap(root="~" * 58).systemd_unit(agent_id)
    assert "256" in str(excinfo.value), str(excinfo.value)


def test_no_unrooted_unit_name_can_reach_the_limit():
    """An unrooted name is bounded at 81 bytes by the agent-id charset.

    So the cap cannot refuse a name an existing deployment already has.
    """
    assert len(TopicMap().systemd_unit("a" * 64).encode("utf-8")) == 81


def test_unit_names_round_trip_their_builder():
    for topics in (TopicMap(), ROOTED, TopicMap(root="fleet/prod")):
        for agent_id in _AGENT_IDS:
            assert topics.unit_agent(topics.systemd_unit(agent_id)) == agent_id


@pytest.mark.parametrize(
    "topics, unit",
    [
        # Another root's unit, and the unrooted unit, are not this map's: a
        # daemon under another root is another swarm's daemon.
        (ROOTED, "swarmbus-wren.service"),
        (ROOTED, "swarmbus-wren:bar.service"),
        (ROOTED, "swarmbus-wren:foo:extra.service"),
        (TopicMap(), "swarmbus-wren:foo.service"),
        # Not a swarmbus unit at all.
        (TopicMap(), "sshd.service"),
        (TopicMap(), "swarmbus-wren.timer"),
        (TopicMap(), "swarmbus-wren"),
        (TopicMap(), "swarmbus-.service"),
        # Not an agent id.
        (TopicMap(), "swarmbus-Wren.service"),
        (TopicMap(), ""),
    ],
)
def test_unit_parser_raises_on_a_unit_that_is_not_this_maps(topics, unit):
    with pytest.raises(ValueError):
        topics.unit_agent(unit)


# ---------------------------------------------------------------------------
# per-root file names: the daemon's log and the default inbox
# ---------------------------------------------------------------------------


def test_the_unrooted_log_and_inbox_names_are_the_ones_already_installed():
    """The literals: an existing install must not need a migration."""
    topics = TopicMap()

    assert topics.unit_log_name("wren") == "swarmbus-wren.log"
    assert topics.unit_log_name("laptop-cc") == "swarmbus-laptop-cc.log"
    assert topics.default_inbox_name("wren") == "wren-inbox.md"
    assert topics.default_inbox_name("laptop-cc") == "laptop-cc-inbox.md"


def test_rooted_log_and_inbox_names_carry_the_root():
    """Two units for one agent id must not interleave into one log, nor
    contend on one inbox file."""
    fleet = TopicMap(root="fleet")
    assert fleet.unit_log_name("wren") == "swarmbus-wren:fleet.log"
    assert fleet.default_inbox_name("wren") == "wren:fleet-inbox.md"

    deep = TopicMap(root="fleet/prod")
    assert deep.unit_log_name("wren") == "swarmbus-wren:fleet:2fprod.log"
    assert deep.default_inbox_name("wren") == "wren:fleet:2fprod-inbox.md"


def test_the_log_and_the_inbox_share_the_unit_names_escaped_token():
    """One token for all three, so a root reads the same off any of them.

    That token is also what makes the log name safe in
    ``StandardOutput=append:``: it emits no ``%`` for systemd to expand and
    no ``\\`` for sed to eat while the unit is rendered.
    """
    for root in ("", *_COLLIDING_ROOTS, "x@y", "100%", "a\\b", "fle€t", "a:b"):
        topics = TopicMap(root=root)
        for agent_id in _AGENT_IDS:
            scope = topics.unit_scope(agent_id)
            assert topics.systemd_unit(agent_id) == f"swarmbus-{scope}.service"
            assert topics.unit_log_name(agent_id) == f"swarmbus-{scope}.log"
            assert topics.default_inbox_name(agent_id) == f"{scope}-inbox.md"
            for name in (
                topics.unit_log_name(agent_id),
                topics.default_inbox_name(agent_id),
            ):
                assert set(name) <= _UNIT_NAME_CHARS, (
                    root, name, sorted(set(name) - _UNIT_NAME_CHARS)
                )
                for dangerous in ("@", "%", "\\", "/"):
                    assert dangerous not in name, (root, name)


def test_distinct_roots_never_share_a_log_or_a_default_inbox():
    """Sibling of the unit-name test: one log is one daemon's output, and
    one inbox file is one swarm's mail."""
    roots = [r for r in _COLLIDING_ROOTS if TopicMap(root=r).root]
    normalized = {TopicMap(root=r).root for r in roots}
    # "a/b" and "a/b/" are the same namespace, so they are the same swarm
    # and SHOULD share both files. Only genuinely distinct roots compare.
    assert len(normalized) < len(roots)

    logs = {r: TopicMap(root=r).unit_log_name("wren") for r in normalized}
    inboxes = {r: TopicMap(root=r).default_inbox_name("wren") for r in normalized}

    for left, right in combinations(sorted(normalized), 2):
        assert logs[left] != logs[right], (
            f"roots {left!r} and {right!r} collide on log {logs[left]!r}"
        )
        assert inboxes[left] != inboxes[right], (
            f"roots {left!r} and {right!r} collide on inbox {inboxes[left]!r}"
        )

    # No rooted name may equal an unrooted one, for ANY agent id: "-" and
    # "_" are both in the agent-id charset.
    installed_logs = {TopicMap().unit_log_name(a) for a in _AGENT_IDS}
    installed_inboxes = {TopicMap().default_inbox_name(a) for a in _AGENT_IDS}
    for agent_id in _AGENT_IDS:
        for root in normalized | {"b", "fleet"}:
            topics = TopicMap(root=root)
            assert topics.unit_log_name(agent_id) not in installed_logs
            assert topics.default_inbox_name(agent_id) not in installed_inboxes


def test_the_unit_name_is_the_longest_name_a_root_produces():
    """So systemd's 255-byte cap on the unit name covers the files too."""
    for root in ("", "fleet", "fleet/prod", "x@y"):
        topics = TopicMap(root=root)
        unit = len(topics.systemd_unit("wren").encode("utf-8"))
        assert len(topics.unit_log_name("wren").encode("utf-8")) < unit
        assert len(topics.default_inbox_name("wren").encode("utf-8")) < unit


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
        # Right tree, wrong depth: an address has one or two levels.
        "agents/wren/extra/deep/presence",
        # Two levels that do not make one unambiguous agent id.
        "agents/flock__a/wren/presence",
        "agents/flock_/wren/presence",
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
        "swarmbus/registry/wren/extra/deep",
        "swarmbus/registry/flock__a/wren",
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


# ---------------------------------------------------------------------------
# two-level addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address, agent_id",
    [
        ("wren", "wren"),
        ("flock/wren", "flock__wren"),
        # One level may hold the separator: that is an ordinary agent id.
        ("flock__wren", "flock__wren"),
        ("a/_b", "a___b"),
    ],
)
def test_an_address_names_one_agent_id(address, agent_id):
    assert agent_id_for_topic_address(address) == agent_id


@pytest.mark.parametrize(
    "address",
    [
        "",
        "a/b/c",
        "a//b",
        "/a",
        "a/",
        "Flock/wren",
        "flock/Wren",
        "flock__a/wren",
        "flock/a__wren",
    ],
)
def test_a_malformed_address_is_refused(address):
    with pytest.raises(ValueError):
        agent_id_for_topic_address(address)


def test_only_one_two_level_address_owns_an_agent_id():
    """``a_/b`` and ``a/_b`` both join to ``a___b``.

    If both were accepted, two different broker logins could publish the
    registry record of one agent id, and the topic would stop saying whose
    record it is.
    """
    assert agent_id_for_topic_address("a/_b") == "a___b"
    with pytest.raises(ValueError, match="ambiguous"):
        agent_id_for_topic_address("a_/b")
    assert two_level_topic_address("a___b") == "a/_b"


@pytest.mark.parametrize(
    "agent_id, address",
    [
        ("flock__wren", "flock/wren"),
        ("wren", None),
        ("flock__a__wren", None),
        ("flock____wren", None),
        ("__wren", None),
        ("flock__", None),
    ],
)
def test_only_an_id_with_one_separator_has_a_two_level_address(
    agent_id, address
):
    assert two_level_topic_address(agent_id) == address
    if address is not None:
        assert agent_id_for_topic_address(address) == agent_id


def test_a_two_level_address_replaces_the_agent_id_in_its_topics():
    for topics, prefix in ((TopicMap(), ""), (ROOTED, "foo/")):
        build = {"topic_address": "flock/wren"}
        assert (
            topics.inbox("flock__wren", **build)
            == f"{prefix}agents/flock/wren/inbox"
        )
        assert (
            topics.presence("flock__wren", **build)
            == f"{prefix}agents/flock/wren/presence"
        )
        assert (
            topics.registry("flock__wren", **build)
            == f"{prefix}swarmbus/registry/flock/wren"
        )
        assert (
            topics.route("flock__wren", **build)
            == f"{prefix}agents/flock/wren/inbox"
        )


def test_the_same_id_without_an_address_stays_at_one_level():
    """The address is opt-in: ``__`` in an id does not move its topics."""
    topics = TopicMap()

    assert topics.inbox("flock__wren") == "agents/flock__wren/inbox"
    assert topics.presence("flock__wren") == "agents/flock__wren/presence"
    assert topics.registry("flock__wren") == "swarmbus/registry/flock__wren"


def test_an_address_cannot_build_another_agents_topics():
    topics = TopicMap()

    for build in (topics.inbox, topics.presence, topics.registry, topics.route):
        with pytest.raises(ValueError, match="does not belong"):
            build("flock__wren", topic_address="flock/robin")
        with pytest.raises(ValueError, match="does not belong"):
            build("wren", topic_address="flock/wren")


def test_broadcast_ignores_an_address():
    assert ROOTED.route("broadcast", topic_address="flock/wren") == BROADCAST_TOPIC


def test_filters_cover_both_address_depths():
    assert TopicMap().presence_filters() == (
        "agents/+/presence",
        "agents/+/+/presence",
    )
    assert TopicMap().registry_filters() == (
        "swarmbus/registry/+",
        "swarmbus/registry/+/+",
    )
    assert ROOTED.presence_filters() == (
        "foo/agents/+/presence",
        "foo/agents/+/+/presence",
    )
    assert ROOTED.registry_filters() == (
        "foo/swarmbus/registry/+",
        "foo/swarmbus/registry/+/+",
    )


def test_parsers_round_trip_a_two_level_address():
    for topics in (TopicMap(), ROOTED):
        presence = topics.presence("flock__wren", topic_address="flock/wren")
        registry = topics.registry("flock__wren", topic_address="flock/wren")

        assert topics.presence_address(presence) == "flock/wren"
        assert topics.presence_agent(presence) == "flock__wren"
        assert topics.registry_address(registry) == "flock/wren"
        assert topics.registry_agent(registry) == "flock__wren"


def test_parsers_report_a_one_level_address_as_the_agent_id():
    topics = TopicMap()

    assert topics.presence_address("agents/flock__wren/presence") == "flock__wren"
    assert topics.registry_address("swarmbus/registry/wren") == "wren"


def test_message_topic_predicate_follows_the_address():
    topics = TopicMap()
    own = {"agent_id": "flock__wren", "topic_address": "flock/wren"}

    assert topics.is_message_topic("agents/flock/wren/inbox", **own) is True
    assert topics.is_message_topic("agents/broadcast", **own) is True
    # Once the agent lives at two levels, the one-level inbox is not its own.
    assert topics.is_message_topic("agents/flock__wren/inbox", **own) is False
    assert topics.is_message_topic("agents/flock/robin/inbox", **own) is False


def test_only_a_two_level_address_names_a_live_session_client_id():
    """One level leaves the client id to the MQTT library, as it always was."""
    assert live_session_client_id("wren") is None
    assert live_session_client_id("flock__wren") is None
    assert live_session_client_id("flock__wren", "flock__wren") is None
    assert live_session_client_id("flock__wren", "flock/wren") == "wren"
    with pytest.raises(ValueError, match="does not belong"):
        live_session_client_id("wren", "flock/wren")
