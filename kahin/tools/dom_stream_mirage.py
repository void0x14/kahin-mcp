"""Agent-facing real-time DOM stream for Mirage/Juggler.

The browser runs the actual ``MutationObserver``. These tools expose three
bounded surfaces an agent can compose:

* ``dom_start`` installs the observer for current and future documents;
* ``dom_snapshot`` returns a semantic, action-oriented live tree;
* ``dom_events`` returns cursor-based mutation deltas and can wait for a real
  browser mutation; ``dom_action`` executes an allow-listed action against a
  still-live node id rather than a stale CSS guess.

The stream is intentionally not an LLM prompt or hidden instruction. It is
structured browser state with explicit cursors, reset/drop signals and caps.
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.dom_stream import DOM_STREAM_INIT_SCRIPT, js_call
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_engine,
    _mirage_eval_result,
    _require_mirage,
)


def _dump(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()


# Real per-session DOM-stream bookkeeping for ``kahin_agent_status``. The
# agent-facing ref/cursor state is never guessed: ``refsLive`` is set True
# only by a successful snapshot and invalidated by the reset/dropped/stale
# signals actually observed on the wire, so status reports whether the last
# snapshot's refs can still be acted on. Keyed by Juggler page session id.
#
# Bounded: every tab opened over a long-lived MCP process contributes a
# distinct session id, and ids are never reused, so an uncapped map would
# grow for the process's whole lifetime. Keep the most recently created
# sessions (dict preserves insertion order, so the first key is the oldest);
# an evicted live session merely reports refsLive conservatively on the next
# status read, never a wrong "true".
_MAX_DOM_STREAM_SESSIONS = 64
_DOM_STREAM_STATE: dict[str, dict[str, Any]] = {}


def _dom_stream_record(
    session_id: str,
    *,
    stream_id: str | None = None,
    cursor: int | None = None,
    next_seq: int | None = None,
    refs_live: bool | None = None,
) -> None:
    """Persist the latest observed stream state for a page session."""
    entry = _DOM_STREAM_STATE.setdefault(
        session_id, {"streamId": None, "cursor": None, "nextSeq": None, "refsLive": False}
    )
    while len(_DOM_STREAM_STATE) > _MAX_DOM_STREAM_SESSIONS:
        # The just-touched session sits at the end, so the oldest-first key
        # here is always a different (idle) session.
        _DOM_STREAM_STATE.pop(next(iter(_DOM_STREAM_STATE)), None)
    if stream_id is not None and entry.get("streamId") != stream_id:
        # A different streamId means a new document: the previous snapshot's
        # refs died with it, whatever the caller believes.
        entry["streamId"] = stream_id
        entry["cursor"] = None
        entry["nextSeq"] = None
        entry["refsLive"] = False
    if cursor is not None:
        entry["cursor"] = cursor
        if next_seq is None:
            next_seq = cursor
    if next_seq is not None:
        entry["nextSeq"] = next_seq
    if refs_live is not None:
        entry["refsLive"] = refs_live


def _dom_stream_status(session_id: str | None) -> dict[str, Any] | None:
    """Latest observed stream state for a session, or None when the page has
    never touched the DOM-stream surface."""
    if not isinstance(session_id, str):
        return None
    entry = _DOM_STREAM_STATE.get(session_id)
    if entry is None:
        return None
    return {
        "streamId": entry.get("streamId"),
        "cursor": entry.get("cursor"),
        "nextSeq": entry.get("nextSeq"),
        "refsLive": bool(entry.get("refsLive")),
    }


_MAX_SELECTOR = 16_384
_MAX_FRAME_ID = 512
_MAX_TEXT = 1_000_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _argument_error(tool: str, field: str, message: str) -> str:
    return _dump({"error": message, "code": "invalid_argument", "tool": tool, "field": field})


def _optional_text(
    value: Any,
    field: str,
    maximum: int,
    tool: str = "kahin_mirage_dom",
) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, _argument_error(tool, field, f"{field} must be a string")
    if len(value) > maximum:
        return None, _argument_error(
            tool, field, f"{field} exceeds the {maximum}-character limit",
        )
    return value, None


def _bounded_int(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
    tool: str = "kahin_mirage_dom",
) -> tuple[int, str | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, _argument_error(tool, field, f"{field} must be an integer")
    if value < minimum:
        return 0, _argument_error(tool, field, f"{field} must be at least {minimum}")
    if value > _MAX_SAFE_INTEGER:
        return 0, _argument_error(tool, field, f"{field} exceeds JavaScript's safe integer range")
    return min(value, maximum), None


def _unwrap(raw: dict[str, Any] | str) -> tuple[Any | None, str | None]:
    if isinstance(raw, str):
        return None, raw
    if raw.get("exceptionDetails"):
        return None, _dump({"error": "DOM stream evaluate threw", "exception": raw["exceptionDetails"]})
    result = raw.get("result") or {}
    if "value" not in result:
        return None, _dump({"error": "DOM stream returned no serializable value", "result": result})
    return result.get("value"), None


async def _prepare(
    frame_id: str | None,
    max_events: int | None = None,
) -> tuple[Any | None, str | None, str | None]:
    err = await _require_mirage()
    if err:
        return None, None, err
    engine = _mirage_engine()
    try:
        page = await engine.ensure_page()
        session_id = page.get("sessionId")
    except Exception as exc:  # noqa: BLE001 - tool returns structured failure
        return None, None, _dump({"error": "DOM stream page setup failed", "detail": str(exc)})
    if not isinstance(session_id, str) or not session_id:
        return None, None, _dump({"error": "DOM stream has no live page session", "code": "session_unavailable"})
    try:
        await engine.install_dom_stream(DOM_STREAM_INIT_SCRIPT)
        raw = await _mirage_eval_result(DOM_STREAM_INIT_SCRIPT, frame_id, session_id=session_id)
        if isinstance(raw, str):
            return None, None, raw
        if raw.get("exceptionDetails"):
            return None, None, _dump({"error": "DOM stream setup threw", "exception": raw["exceptionDetails"]})
        if max_events is not None:
            configure = await _mirage_eval_result(
                js_call("configure", {"maxEvents": max_events}), frame_id, session_id=session_id
            )
            _, error = _unwrap(configure)
            if error:
                return None, None, error
    except Exception as exc:  # noqa: BLE001 - tool returns structured failure
        return None, None, _dump({"error": "DOM stream setup failed", "detail": str(exc)})
    return engine, session_id, None


async def _call_page(
    method: str,
    params: dict[str, Any],
    frame_id: str | None,
    session_id: str,
) -> tuple[Any | None, str | None]:
    raw = await _mirage_eval_result(js_call(method, params), frame_id, session_id=session_id)
    return _unwrap(raw)


@mcp.tool(name="kahin_mirage_dom_start", annotations=_RW)
async def mirage_dom_start(
    frame_id: str | None = None,
    max_events: int = 512,
) -> str:
    """Start/reuse the real browser DOM stream.

    The observer survives navigations through Juggler's init-script surface.
    ``frame_id`` targets an iframe for the immediate status; future frames are
    covered by the same browser-level init script.
    """
    checked_frame, error = _optional_text(frame_id, "frame_id", _MAX_FRAME_ID, "kahin_mirage_dom_start")
    if error:
        return error
    checked_events, error = _bounded_int(max_events, "max_events", 32, 2000, "kahin_mirage_dom_start")
    if error:
        return error
    async with _healer_ref.safe("kahin_mirage_dom_start", frame_id=checked_frame, max_events=checked_events):
        engine, session_id, error = await _prepare(checked_frame, max_events=checked_events)
        if error:
            return error
        assert engine is not None and session_id is not None
        value, error = await _call_page("status", {}, checked_frame, session_id)
        if error:
            return error
        if isinstance(value, dict):
            cursor = value.get("cursor")
            _dom_stream_record(
                session_id,
                stream_id=value.get("streamId"),
                cursor=cursor if isinstance(cursor, int) else None,
                next_seq=cursor if isinstance(cursor, int) else None,
            )
        return _dump({"status": "started", "stream": value, "frame_id": checked_frame})


@mcp.tool(name="kahin_mirage_dom_snapshot", annotations=_RO)
async def mirage_dom_snapshot(
    selector: str | None = None,
    max_nodes: int = 800,
    max_depth: int = 12,
    include_hidden: bool = False,
    text_limit: int = 240,
    frame_id: str | None = None,
) -> str:
    """Return a bounded live semantic DOM snapshot.

    Nodes include stable-per-document ``nodeId`` values, role/name/text,
    visibility/geometry, safe identifying attributes and action hints. A
    ``truncated`` result is an instruction to narrow with ``selector`` or
    increase caps; it is never silently presented as a complete page.
    """
    checked_selector, error = _optional_text(selector, "selector", _MAX_SELECTOR, "kahin_mirage_dom_snapshot")
    if error:
        return error
    checked_frame, error = _optional_text(frame_id, "frame_id", _MAX_FRAME_ID, "kahin_mirage_dom_snapshot")
    if error:
        return error
    checked_nodes, error = _bounded_int(max_nodes, "max_nodes", 1, 5000, "kahin_mirage_dom_snapshot")
    if error:
        return error
    checked_depth, error = _bounded_int(max_depth, "max_depth", 1, 32, "kahin_mirage_dom_snapshot")
    if error:
        return error
    checked_text, error = _bounded_int(text_limit, "text_limit", 20, 2000, "kahin_mirage_dom_snapshot")
    if error:
        return error
    if not isinstance(include_hidden, bool):
        return _argument_error("kahin_mirage_dom_snapshot", "include_hidden", "include_hidden must be a boolean")
    async with _healer_ref.safe(
        "kahin_mirage_dom_snapshot", selector=checked_selector or "", frame_id=checked_frame
    ):
        _, session_id, error = await _prepare(checked_frame)
        if error:
            return error
        assert session_id is not None
        value, error = await _call_page("snapshot", {
            "selector": checked_selector or "",
            "maxNodes": checked_nodes,
            "maxDepth": checked_depth,
            "includeHidden": include_hidden,
            "textLimit": checked_text,
        }, checked_frame, session_id)
        if error:
            return error
        if isinstance(value, dict) and not value.get("error"):
            cursor = value.get("cursor")
            _dom_stream_record(
                session_id,
                stream_id=value.get("streamId"),
                cursor=cursor if isinstance(cursor, int) else None,
                next_seq=value.get("nextSeq") if isinstance(value.get("nextSeq"), int) else None,
                refs_live=True,
            )
        return _dump(value)


@mcp.tool(name="kahin_mirage_dom_events", annotations=_RO)
async def mirage_dom_events(
    after_seq: int = 0,
    stream_id: str | None = None,
    limit: int = 100,
    wait_ms: int = 0,
    frame_id: str | None = None,
) -> str:
    """Read real MutationObserver deltas after a cursor.

    ``wait_ms`` enables bounded long-polling. On navigation or ring overflow,
    ``reset``/``dropped`` tells the agent to request a fresh snapshot before
    continuing; stale deltas are never treated as current truth. Detection
    compares the ``stream_id`` you pass against the page's current ``streamId``:
    always pass the ``streamId`` returned by ``dom_start``, otherwise a
    navigation is invisible and old deltas can mix with the new document's.
    """
    checked_after, error = _bounded_int(
        after_seq, "after_seq", 0, _MAX_SAFE_INTEGER, "kahin_mirage_dom_events",
    )
    if error:
        return error
    checked_stream, error = _optional_text(stream_id, "stream_id", _MAX_FRAME_ID, "kahin_mirage_dom_events")
    if error:
        return error
    checked_limit, error = _bounded_int(limit, "limit", 1, 500, "kahin_mirage_dom_events")
    if error:
        return error
    checked_wait, error = _bounded_int(wait_ms, "wait_ms", 0, 30_000, "kahin_mirage_dom_events")
    if error:
        return error
    checked_frame, error = _optional_text(frame_id, "frame_id", _MAX_FRAME_ID, "kahin_mirage_dom_events")
    if error:
        return error
    async with _healer_ref.safe(
        "kahin_mirage_dom_events", after_seq=checked_after, stream_id=checked_stream or "", wait_ms=checked_wait
    ):
        engine, session_id, error = await _prepare(checked_frame)
        if error:
            return error
        assert engine is not None and session_id is not None
        deadline = asyncio.get_running_loop().time() + checked_wait / 1000
        while True:
            value, error = await _call_page("drain", {
                "after": checked_after,
                "streamId": checked_stream or "",
                "limit": checked_limit,
            }, checked_frame, session_id)
            if error:
                return error
            if not isinstance(value, dict):
                return _dump({"error": "DOM stream returned an invalid event payload"})
            cursor = value.get("cursor")
            _dom_stream_record(
                session_id,
                stream_id=value.get("streamId"),
                cursor=cursor if isinstance(cursor, int) else None,
                next_seq=value.get("nextSeq") if isinstance(value.get("nextSeq"), int) else None,
                refs_live=False if (value.get("reset") or value.get("dropped")) else None,
            )
            if value.get("events") or value.get("reset") or value.get("dropped"):
                return _dump(value)
            if checked_wait <= 0:
                return _dump(value)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return _dump(value)
            await engine.wait_for_dom_signal(min(0.5, remaining))


@mcp.tool(name="kahin_mirage_dom_action", annotations=_DW)
async def mirage_dom_action(
    node_id: str,
    action: str,
    text: str | None = None,
    frame_id: str | None = None,
) -> str:
    """Act on a live snapshot node without trusting a stale selector.

    Allowed actions are ``click``, ``hover``, ``focus``, ``type``, ``scroll``
    and ``select``. Click/hover use real Juggler mouse dispatch; type focuses the
    live element and uses the real Juggler ``Page.insertText`` command; select
    matches an option by value or visible text and fires real input/change
    events. ``text`` is required for ``type`` and ``select`` and is validated
    before any page side effect. A removed or navigated node returns
    ``stale_node`` with ``requiresSnapshot: true`` instead of acting on an
    accidental replacement.
    """
    checked_node, error = _optional_text(node_id, "node_id", _MAX_FRAME_ID, "kahin_mirage_dom_action")
    if error:
        return error
    assert checked_node is not None
    checked_action, error = _optional_text(action, "action", 32, "kahin_mirage_dom_action")
    if error:
        return error
    assert checked_action is not None
    if checked_action not in {"click", "hover", "focus", "type", "scroll", "select"}:
        return _dump({
            "error": "unsupported_action",
            "action": checked_action,
            "allowed": ["click", "hover", "focus", "type", "scroll", "select"],
            "tool": "kahin_mirage_dom_action",
            "field": "action",
        })
    if text is not None and (not isinstance(text, str) or len(text) > _MAX_TEXT):
        return _argument_error("kahin_mirage_dom_action", "text", "text must be a string of at most 1000000 characters")
    if checked_action in {"type", "select"} and text is None:
        return _argument_error("kahin_mirage_dom_action", "text", f"text is required for action={checked_action}")
    checked_frame, error = _optional_text(frame_id, "frame_id", _MAX_FRAME_ID, "kahin_mirage_dom_action")
    if error:
        return error
    async with _healer_ref.safe(
        "kahin_mirage_dom_action", node_id=checked_node, action=checked_action, frame_id=checked_frame
    ):
        engine, session_id, error = await _prepare(checked_frame)
        if error:
            return error
        assert engine is not None and session_id is not None
        value, error = await _call_page(
            "action", {"nodeId": checked_node, "action": checked_action, "text": text}, checked_frame, session_id
        )
        if error:
            return error
        if not isinstance(value, dict):
            return _dump({"error": "DOM action returned an invalid target payload"})
        if value.get("error"):
            if value.get("requiresSnapshot"):
                _dom_stream_record(session_id, refs_live=False)
            return _dump(value)

        result: dict[str, Any] = {"target": value, "action": checked_action}
        if checked_action in {"click", "hover"}:
            try:
                event_type = "mousemove" if checked_action == "hover" else "mousedown"
                result["down"] = await engine.call("Page.dispatchMouseEvent", {
                    "type": event_type,
                    "button": 0,
                    "x": value["x"],
                    "y": value["y"],
                    "modifiers": 0,
                    "clickCount": 1,
                    "buttons": 1 if checked_action == "click" else 0,
                }, session_id=session_id)
                if checked_action == "click":
                    result["up"] = await engine.call("Page.dispatchMouseEvent", {
                        "type": "mouseup",
                        "button": 0,
                        "x": value["x"],
                        "y": value["y"],
                        "modifiers": 0,
                        "clickCount": 1,
                        "buttons": 0,
                    }, session_id=session_id)
            except Exception as exc:  # noqa: BLE001 - return tool-level error
                return _dump({"error": "DOM action dispatch failed", "detail": str(exc)})
        elif checked_action == "type":
            assert text is not None  # required-text validation ran before dispatch
            try:
                result["typed"] = await engine.call("Page.insertText", {"text": text}, session_id=session_id)
            except Exception as exc:  # noqa: BLE001 - return tool-level error
                return _dump({"error": "DOM action typing failed", "detail": str(exc)})
        return _dump(result)


@mcp.tool(name="kahin_mirage_dom_stop", annotations=_RW)
async def mirage_dom_stop(frame_id: str | None = None) -> str:
    """Disconnect the current document's observer and discard its page ring."""
    checked_frame, error = _optional_text(frame_id, "frame_id", _MAX_FRAME_ID, "kahin_mirage_dom_stop")
    if error:
        return error
    async with _healer_ref.safe("kahin_mirage_dom_stop", frame_id=checked_frame):
        _, session_id, error = await _prepare(checked_frame)
        if error:
            return error
        assert session_id is not None
        value, error = await _call_page("stop", {}, checked_frame, session_id)
        if error:
            return error
        _dom_stream_record(session_id, refs_live=False)
        return _dump({"status": "stopped", "stream": value, "frame_id": checked_frame})
