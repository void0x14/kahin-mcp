"""oracle.py — MCP Server (Kahin'in Sesi).

Bootstrap only: the FastMCP instance, engine lifecycle glue (event
collectors) and ``main()``. All public tools live in engine-separated category
modules under :mod:`kahin.tools`, imported below for side-effect
``@mcp.tool`` registration.
"""

from __future__ import annotations

from kahin._events import (  # noqa: F401  (re-exported for backwards compatibility)
    _MAX_EVENT_DEPTH,
    _MAX_EVENT_ITEMS,
    _MAX_EVENT_STRING,
    _bound_event,
    _on_cdp_event,
    _on_console_event,
    _on_engine_death,
    _on_network_event,
)
from kahin._mcp import mcp  # noqa: F401  (re-exported for the old import path)


def main() -> None:
    mcp.run(transport="stdio")


# Register the engine-separated category tool modules (side-effect @mcp.tool).
import kahin.tools  # noqa: E402,F401
from kahin import harness as _harness  # noqa: E402

# Every client tool call passes the operator-discipline harness (P1-2).
_harness.install(mcp)


if __name__ == "__main__":
    main()
