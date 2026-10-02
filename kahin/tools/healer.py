"""healer.py — self-healing statistics and operator reset (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin._healer import get_tracker
from kahin._mcp import mcp
from kahin.harness import get_harness
from kahin.tools._common import _RO, _RW

_RESET_REASON_MIN = 10
_RESET_REASON_MAX = 500


@mcp.tool(name="kahin_healer_stats", annotations=_RO)
async def healer_stats() -> str:
    """Get error tracking and self-healing statistics."""
    return orjson.dumps(get_tracker().get_stats(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_operator_reset", annotations=_RW)
async def operator_reset(reason: str) -> str:
    """Lift the operator-discipline harness after the cause has been handled.

    The harness refuses identical retries of a failing call, halts after
    consecutive stalls (with a state dump), blocks a page-locked path after a
    restart and requires an observation after repeated blind clicks. Call this
    only when the next step is genuinely different; ``reason`` (10-500 chars)
    must say what changed. The answer returns the harness state it cleared.
    """
    if not isinstance(reason, str) or not (_RESET_REASON_MIN <= len(reason.strip()) <= _RESET_REASON_MAX):
        return orjson.dumps({
            "error": f"reason must be {_RESET_REASON_MIN}-{_RESET_REASON_MAX} characters saying what changed",
            "code": "invalid_argument",
        }, option=orjson.OPT_INDENT_2).decode()
    return orjson.dumps(get_harness().operator_reset(reason.strip()), option=orjson.OPT_INDENT_2).decode()
