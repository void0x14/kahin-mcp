"""oracle.py — MCP Server (Kahin'in Sesi).

Bootstrap only: the FastMCP instance, engine lifecycle glue (event
collectors) and ``main()``. All public tools live in engine-separated category
modules under :mod:`kahin.tools`, imported below for side-effect
``@mcp.tool`` registration.
"""

from __future__ import annotations

import time
from typing import Any

from kahin import _state as state
from kahin._mcp import mcp  # noqa: F401  (re-exported for the old import path)
from kahin.the_twins.chassis import EventData


_MAX_EVENT_DEPTH = 8
_MAX_EVENT_ITEMS = 100
_MAX_EVENT_STRING = 4096


def _bound_event(value: Any, depth: int = 0) -> Any:
    """Keep debug buffers useful without allowing page data to become a RAM sink."""
    if depth >= _MAX_EVENT_DEPTH:
        return "[event depth truncated]"
    if isinstance(value, str):
        if len(value) <= _MAX_EVENT_STRING:
            return value
        return value[:_MAX_EVENT_STRING] + f"…[truncated {len(value) - _MAX_EVENT_STRING} chars]"
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_EVENT_ITEMS]
        result = {str(key): _bound_event(item, depth + 1) for key, item in items}
        if len(value) > len(items):
            result["__truncated_items__"] = len(value) - len(items)
        return result
    if isinstance(value, (list, tuple)):
        result = [_bound_event(item, depth + 1) for item in value[:_MAX_EVENT_ITEMS]]
        if len(value) > len(result):
            result.append(f"[truncated {len(value) - len(result)} items]")
        return result
    return value


def _on_cdp_event(evt: EventData) -> None:
    state._event_seq += 1
    params = _bound_event(evt.params)
    if evt.method == "Page.screencastFrame" and isinstance(params, dict):
        data = params.get("data")
        if isinstance(data, str):
            params["dataLength"] = len(data)
            params["data"] = "[screencast frame omitted from event history]"
    state._current_event_log.append({
        "seq": state._event_seq,
        "event": evt.method,
        "params": params,
        "session_id": evt.session_id,
    })


def _on_network_event(evt: EventData) -> None:
    if evt.method.startswith("Network."):
        state._network_requests.append({
            "event": evt.method.replace("Network.", ""),
            "params": _bound_event(evt.params),
            "timestamp": time.time(),
            "session_id": evt.session_id,
        })


def _on_console_event(evt: EventData) -> None:
    if evt.method == "Runtime.console":
        args = [
            _bound_event(a.get("value"))
            for a in evt.params.get("args", [])
            if isinstance(a, dict)
        ]
        state._console_messages.append({
            "type": evt.params.get("type"),
            "args": args,
            "location": _bound_event(evt.params.get("location")),
            "session_id": evt.session_id,
        })


def _on_engine_death(engine) -> None:
    """Reader EOF: mark the engine dead but keep it reachable for cleanup.

    ``browser_stop`` must still be able to reap a sidecar whose Firefox child
    disappeared first. Dropping the reference here made the next stop call
    return ``No engine running`` and left lifecycle cleanup with no owner.
    ``_require_engine`` and ``browser_start`` already reject/reap dead engines;
    keeping the reference lets those paths, or an explicit stop, do so.
    """
    if state._current_engine is engine:
        if getattr(engine, "_preserve_state_on_stop", False):
            # Shadow->Mirage promotion is a controlled backend replacement,
            # not a browser shutdown. Keep bounded evidence buffers intact.
            return
        if getattr(engine, "_stopping", False):
            state.clear_state()
            return
        process = getattr(engine, "_process", None)
        state._last_engine_death = {
            "engine": type(engine).__name__.lower(),
            "reason": getattr(engine, "_death_reason", None) or "transport_closed",
            "timestamp": getattr(engine, "_death_at", None) or time.time(),
            "pid": getattr(process, "pid", None),
            "returncode": getattr(process, "returncode", None),
            "stderr_log": str(getattr(engine, "_stderr_path", "")) or None,
        }
        state.clear_state()
        state.release_browser_lock()


def main() -> None:
    mcp.run(transport="stdio")


# Register the engine-separated category tool modules (side-effect @mcp.tool).
import kahin.tools  # noqa: E402,F401
from kahin import harness as _harness  # noqa: E402

# Every client tool call passes the operator-discipline harness (P1-2).
_harness.install(mcp)


if __name__ == "__main__":
    main()
