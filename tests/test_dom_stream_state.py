"""Unit tests for the bounded DOM-stream per-session bookkeeping.

The agent-facing ref/cursor state is keyed by Juggler page session id. A
long-lived MCP process opens many tabs over its lifetime and session ids are
never reused, so the map must stay bounded (see ``_MAX_DOM_STREAM_SESSIONS``).
"""

from kahin.tools import dom_stream_mirage as ds


def test_dom_stream_state_stays_bounded() -> None:
    ds._DOM_STREAM_STATE.clear()
    total = ds._MAX_DOM_STREAM_SESSIONS * 3
    try:
        for i in range(total):
            ds._dom_stream_record(f"session-{i}", stream_id=f"stream-{i}", refs_live=True)

        assert len(ds._DOM_STREAM_STATE) <= ds._MAX_DOM_STREAM_SESSIONS
        # Most-recent session is tracked; the oldest was evicted.
        assert f"session-{total - 1}" in ds._DOM_STREAM_STATE
        assert "session-0" not in ds._DOM_STREAM_STATE
    finally:
        ds._DOM_STREAM_STATE.clear()


def test_dom_stream_status_reads_recorded_state() -> None:
    ds._DOM_STREAM_STATE.clear()
    try:
        ds._dom_stream_record("s1", stream_id="st1", cursor=5, refs_live=True)
        status = ds._dom_stream_status("s1")
        assert status is not None
        assert status["streamId"] == "st1"
        assert status["refsLive"] is True
        assert ds._dom_stream_status("missing") is None
        assert ds._dom_stream_status(None) is None
    finally:
        ds._DOM_STREAM_STATE.clear()
