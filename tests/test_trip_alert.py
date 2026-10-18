import asyncio
import logging
import sys
import time

import pytest


def _trip(sender="wren", origin="wren", message_id="m-1"):
    from swarmbus.archive import TripEvent

    return TripEvent(
        sender=sender, kind="hop-limit", message_id=message_id, hops=5,
        origin=origin, source_topic="agents/sparrow/inbox",
        tripped_at="2026-10-16T00:00:00+00:00",
    )


def test_trip_env_is_sanitized_and_drops_wake_variables(monkeypatch):
    from swarmbus.trip_alert import trip_env

    monkeypatch.setenv("SWARMBUS_ID", "parent-id")
    monkeypatch.setenv("SWARMBUS_HOPS", "3")
    monkeypatch.setenv("SWARMBUS_TRIP_UNTIL", "stale")
    monkeypatch.setenv("SWARMBUS_STATE_DIR", "/tmp/state")
    hostile = "evil\nSWARMBUS_TRIP_KIND=x\x00 \ud800" + "y" * 600

    env = trip_env(_trip(origin=hostile, message_id="id\r\n2"), agent_id="sparrow", listener="start")

    assert "SWARMBUS_ID" not in env and "SWARMBUS_HOPS" not in env
    assert env["SWARMBUS_STATE_DIR"] == "/tmp/state"
    assert env["SWARMBUS_TRIP_UNTIL"] == ""
    assert (env["SWARMBUS_TRIP_KIND"], env["SWARMBUS_TRIP_SENDER"]) == ("hop-limit", "wren")
    assert (env["SWARMBUS_TRIP_AGENT"], env["SWARMBUS_TRIP_LISTENER"]) == ("sparrow", "start")
    assert env["SWARMBUS_TRIP_HOPS"] == "5"
    assert env["SWARMBUS_TRIP_RESET"] == "swarmbus held --agent-id sparrow --reset wren"
    assert "wren" in env["SWARMBUS_TRIP_SUMMARY"]
    for key, value in env.items():
        if key.startswith("SWARMBUS_TRIP_"):
            assert len(value) <= 256
            assert not any(c in value for c in "\n\r\x00 ")
            value.encode("utf-8")  # no lone surrogate


@pytest.mark.asyncio
async def test_alerter_runs_the_command_once_per_trip(tmp_path, monkeypatch):
    from swarmbus.trip_alert import TripAlerter

    monkeypatch.setenv("ALERT_OUT", str(tmp_path))
    script = (
        "import os, pathlib; "
        "pathlib.Path(os.environ['ALERT_OUT'], os.environ['SWARMBUS_TRIP_SENDER'])"
        ".write_text(os.environ['SWARMBUS_TRIP_KIND'])"
    )
    alerter = TripAlerter([sys.executable, "-c", script], agent_id="sparrow", listener="watch")
    alerter.submit(_trip("wren"))
    alerter.submit(_trip("robin"))
    await alerter.aclose()

    assert sorted(p.name for p in tmp_path.iterdir()) == ["robin", "wren"]
    assert (tmp_path / "wren").read_text() == "hop-limit"


@pytest.mark.asyncio
async def test_hung_or_failing_alert_never_blocks_and_is_logged(caplog):
    from swarmbus.trip_alert import TripAlerter

    caplog.set_level(logging.WARNING, logger="swarmbus.trip_alert")
    hung = TripAlerter(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        agent_id="sparrow", listener="start", timeout=0.5,
    )
    started = time.monotonic()
    hung.submit(_trip())
    assert time.monotonic() - started < 0.2
    await asyncio.sleep(0)
    await hung.aclose()
    assert time.monotonic() - started < 10
    assert "timed out" in caplog.text

    failing = TripAlerter(
        [sys.executable, "-c", "raise SystemExit(3)"], agent_id="sparrow", listener="start",
    )
    failing.submit(_trip())
    missing = TripAlerter(["/nonexistent/swarmbus-alert"], agent_id="sparrow", listener="start")
    missing.submit(_trip())
    await failing.aclose()
    await missing.aclose()
    assert "exited 3" in caplog.text
    assert "/nonexistent/swarmbus-alert" in caplog.text


@pytest.mark.asyncio
async def test_alert_queue_is_bounded(caplog):
    from swarmbus.trip_alert import TripAlerter

    caplog.set_level(logging.WARNING, logger="swarmbus.trip_alert")
    alerter = TripAlerter(
        [sys.executable, "-c", "pass"], agent_id="sparrow", listener="start", max_pending=2,
    )
    for i in range(5):
        alerter.submit(_trip(sender=f"peer-{i}"))
    assert alerter.dropped == 3
    assert "queue full" in caplog.text
    await alerter.aclose()
