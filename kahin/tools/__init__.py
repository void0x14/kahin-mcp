"""kahin.tools — engine-separated category tool modules.

Importing this package registers every tool on the shared ``mcp`` instance
(defined in :mod:`kahin.oracle`) via ``@mcp.tool`` side effects.
``kahin/oracle.py`` imports this package at the end of its bootstrap; the
tool modules import ``mcp`` back from ``kahin.oracle``.

Engine-agnostic categories are shared files; Camoufox-specific categories
live in the ``*_mirage.py`` modules.  Shadow keeps the shared CDP surface;
visual capabilities are promoted to Mirage by the common tool layer.  The
engine-specific placeholder modules are retained only as package boundaries
and do not register pretend tools.
"""

from kahin.tools import (
    accessibility_mirage,
    agent_mirage,
    cf_clear_mirage,
    crawler_mirage,
    dejavu,
    dejavu_mirage,
    dejavu_obscura,
    dialog_mirage,
    dom_stream_mirage,
    emulation_mirage,
    engine,
    extensions_mirage,
    grimoire,
    healer,
    pilot,
    pilot_mirage,
    pilot_obscura,
    passkey_mirage,
    prophecy,
    reliability_mirage,
    screencast_mirage,
    screencast_server_mirage,
    seraph,
    stealth_mirage,
    storage_mirage,
    trainman,
    trainman_mirage,
    trainman_obscura,
    upload_mirage,
    visualization,
)

__all__ = [
    "accessibility_mirage",
    "agent_mirage",
    "cf_clear_mirage",
    "crawler_mirage",
    "dejavu",
    "dejavu_mirage",
    "dejavu_obscura",
    "dialog_mirage",
    "dom_stream_mirage",
    "emulation_mirage",
    "engine",
    "extensions_mirage",
    "grimoire",
    "healer",
    "pilot",
    "pilot_mirage",
    "pilot_obscura",
    "passkey_mirage",
    "prophecy",
    "reliability_mirage",
    "screencast_mirage",
    "screencast_server_mirage",
    "seraph",
    "stealth_mirage",
    "storage_mirage",
    "trainman",
    "trainman_mirage",
    "trainman_obscura",
    "upload_mirage",
    "visualization",
]
