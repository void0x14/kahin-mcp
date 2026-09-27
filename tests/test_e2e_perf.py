"""Faz 4 Task 3: engine stats tool + per-identity profile pre-warm.

The pure unit tests (tracker aggregates, engine-unavailable shape, pre-warm
hash/cache bounds) run without a browser. The live tests drive the same
async functions the MCP server exposes over real Camoufox: the stats tool
must report monotonic uptime and real per-tool durations, and a saved
identity must get stable pre-warm metadata with a memory cache hit on the
second boot — while the metadata file stays bounded and payload-free.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kahin import _state
from kahin._healer import ErrorEntry, ErrorTracker
from kahin.tools import agent_mirage, engine as engine_mod, pilot
from kahin.tools.agent_mirage import _identity_path
from kahin.the_twins import mirage as mirage_mod


def _real_available() -> bool:
    """True when the sidecar binary and a real Camoufox are both present."""
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


def _loads(text: str) -> Any:
    return json.loads(text)


async def _stop_engine() -> None:
    """Stop whatever engine is registered without asserting its state."""
    engine = _state._current_engine
    if engine is not None:
        await engine.stop()
        _state._current_engine = None


def test_tracker_aggregates_are_bounded_and_deterministic() -> None:
    """Per-tool calls/errors/total_ms/max_ms stay exact and bounded."""
    tracker = ErrorTracker()
    tracker.record_success("kahin_a", 12.5)
    tracker.record_success("kahin_a", 7.5)
    tracker.record_error(ErrorEntry(tool="kahin_a", duration_ms=3.0, message="boom"))
    stats = tracker.engine_stats()
    assert stats["tool_calls"] == 2
    assert stats["tool_errors"] == 1
    top = stats["top_slow"][0]
    assert top["tool"] == "kahin_a"
    assert top["count"] == 2
    assert top["avg_ms"] == 10.0
    assert top["max_ms"] == 12.5
    assert stats["last_error"] is not None
    assert stats["last_error"]["tool"] == "kahin_a"
    assert stats["last_error"]["message"] == "boom"

    # Distinct-tool bound: extra tools beyond the cap are not tracked, and
    # top_slow never exceeds its own cap.
    for i in range(400):
        tracker.record_success(f"kahin_extra_{i}", 1.0)
    assert len(tracker._tool_stats) <= 256
    assert len(tracker.engine_stats()["top_slow"]) <= 10
    # Existing healer stats shape is preserved.
    legacy = tracker.get_stats()
    assert legacy["total_successes"] == 2 + 400
    assert legacy["avg_timing_ms"]["kahin_a"] == 10.0
    assert set(legacy) >= {
        "total_errors",
        "total_successes",
        "total_recoveries",
        "errors_by_code",
        "errors_by_tool",
        "avg_timing_ms",
        "recent_errors",
    }


@pytest.mark.asyncio
async def test_engine_stats_unavailable_shape() -> None:
    """No engine: structured engine_unavailable, never an exception."""
    previous = _state._current_engine
    _state._current_engine = None
    try:
        resp = _loads(await engine_mod.engine_stats())
    finally:
        _state._current_engine = previous
    assert resp["engine"] is None
    assert resp["code"] == "engine_unavailable"
    assert resp["uptime_s"] == 0.0
    assert isinstance(resp["tool_calls"], int)
    assert isinstance(resp["tool_errors"], int)
    assert isinstance(resp["top_slow"], list)
    assert "last_error" in resp


def test_prewarm_hash_and_cache_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Identity hashing is stable/safe and the metadata store stays bounded."""
    monkeypatch.setenv("KAHIN_PROFILE_CACHE_DIR", str(tmp_path))
    config_a = {"navigator.userAgent": "UA-A", "screen.width": 1920}
    config_b = {"navigator.userAgent": "UA-B", "screen.width": 1920}
    hash_a = mirage_mod._identity_hash(config_a)
    assert hash_a == mirage_mod._identity_hash(config_a)  # deterministic
    assert hash_a != mirage_mod._identity_hash(config_b)  # config-sensitive
    assert len(hash_a) == 16
    assert hash_a.isalnum()

    # Disk round-trip: record -> file -> load with source=disk.
    mirage_mod._prewarm_record(
        hash_a, {"identity_hash": hash_a, "options_ms": 1.0, "profile_ms": 2.0, "hits": 0, "starts": 1}
    )
    assert (tmp_path / f"{hash_a}.json").is_file()
    # Memory lookup is preferred; drop the in-process entry to exercise the
    # bounded on-disk path.
    mirage_mod._PREWARM_CACHE.pop(hash_a, None)
    loaded = mirage_mod._prewarm_load(hash_a)
    assert loaded is not None and loaded["source"] == "disk"
    assert loaded["hits"] == 0
    assert "config" not in loaded  # no identity payload in metadata
    assert (tmp_path / f"{hash_a}.json").stat().st_size <= 4096

    # Oversized/corrupt metadata is refused, never trusted.
    bad = tmp_path / "deadbeef00000000.json"
    bad.write_bytes(b"x" * 9000)
    assert mirage_mod._prewarm_load("deadbeef00000000") is None

    # In-process cache is capped by eviction of the oldest entries.
    saved = dict(mirage_mod._PREWARM_CACHE)
    mirage_mod._PREWARM_CACHE.clear()
    try:
        for i in range(mirage_mod._PREWARM_CACHE_MAX + 5):
            h = f"{i:016x}"
            mirage_mod._prewarm_record(h, {"identity_hash": h, "hits": 0, "starts": 1})
        assert len(mirage_mod._PREWARM_CACHE) == mirage_mod._PREWARM_CACHE_MAX
        # Memory lookup is preferred over disk.
        first = next(iter(mirage_mod._PREWARM_CACHE))
        assert mirage_mod._prewarm_load(first)["source"] == "memory"
    finally:
        mirage_mod._PREWARM_CACHE.clear()
        mirage_mod._PREWARM_CACHE.update(saved)


@pytest.mark.skipif(not _real_available(), reason="sidecar binary or Camoufox missing")
@pytest.mark.asyncio
async def test_engine_stats_live_monotonic() -> None:
    """Real engine: uptime is monotonic and real tool durations appear."""
    await _stop_engine()
    try:
        start = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, headless=True))
        assert start.get("status") == "started"
        _loads(await engine_mod.engine_health())
        await asyncio.sleep(0.05)
        first = _loads(await engine_mod.engine_stats())
        assert first["engine"] == "mirage"
        assert first["uptime_s"] > 0.0
        assert first["tool_calls"] >= 1
        # The healer tracker is process-global and top_slow is capped at 10
        # entries ranked by average duration, so a specific fast tool (e.g.
        # engine_health) is not guaranteed to appear in a full-suite run.
        # What is guaranteed: non-empty, shape-valid rollup entries carrying
        # real recorded durations (the recording itself is proven by the
        # tool_calls assertion above).
        entries = {t["tool"]: t for t in first["top_slow"]}
        assert entries
        for entry in entries.values():
            assert entry["count"] >= 1
            assert entry["avg_ms"] >= 0.0
            # avg_ms rounds to 1 decimal and max_ms to 3; allow rounding slack.
            assert entry["max_ms"] + 0.1 >= entry["avg_ms"]
        await asyncio.sleep(0.05)
        second = _loads(await engine_mod.engine_stats())
        # Uptime only moves forward (monotonic clock), and it tracks real
        # wall time rather than a counter.
        assert second["uptime_s"] >= first["uptime_s"]
        assert second["uptime_s"] - first["uptime_s"] < 5.0
        assert second["tool_calls"] >= first["tool_calls"]
    finally:
        await _stop_engine()


@pytest.mark.skipif(not _real_available(), reason="sidecar binary or Camoufox missing")
@pytest.mark.asyncio
async def test_prewarm_identity_metadata_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A saved identity gets stable pre-warm metadata and a memory hit on
    the second boot; the metadata file stays bounded and payload-free."""
    monkeypatch.setenv("KAHIN_PROFILE_CACHE_DIR", str(tmp_path))
    name = "prewarm-perf-e2e"
    await _stop_engine()
    try:
        created = _loads(await agent_mirage.identity_new(name=name))
        assert created.get("saved") is True

        first = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, identity=name, headless=True))
        assert first.get("status") == "started"
        engine = _state._current_engine
        assert engine is not None
        info1 = engine._prewarm_info
        assert info1 is not None
        assert info1["cache"] == "miss"
        assert info1["hits"] == 0
        assert info1["starts"] == 1
        identity_hash = info1["identity_hash"]
        # launch_options() mutates the config dict it is given, so hash the
        # original saved identity file (what start() hashed before launch).
        saved_path = _identity_path(name)
        assert saved_path is not None and saved_path.is_file()
        original_config = json.loads(saved_path.read_text())["config"]
        assert identity_hash == mirage_mod._identity_hash(original_config)
        await _stop_engine()

        second = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, identity=name, headless=True))
        assert second.get("status") == "started"
        engine2 = _state._current_engine
        assert engine2 is not None
        info2 = engine2._prewarm_info
        assert info2 is not None
        assert info2["cache"] == "memory"
        assert info2["identity_hash"] == identity_hash
        assert info2["hits"] == 1
        assert info2["starts"] == 2
        await _stop_engine()

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["identity_hash"] == identity_hash
        assert "config" not in data
        assert files[0].stat().st_size <= 4096
    finally:
        await _stop_engine()
        await agent_mirage.identity_delete(name=name)
