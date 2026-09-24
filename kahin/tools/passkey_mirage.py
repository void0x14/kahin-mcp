"""One-time Bitwarden setup in the active Mirage browser."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.passkey_session import PasskeyUISession
from kahin.passkey_ui import configure_unlocked_vault, prepare_login_ui
from kahin.the_twins.mirage import Mirage
from kahin.tools._common import _RO, _RW, _healer_ref


logger = logging.getLogger(__name__)
_setup_lock = asyncio.Lock()
_setup_session: PasskeyUISession | None = None
_setup_engine: Mirage | None = None


def _json(payload: dict[str, Any]) -> str:
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


def _error(message: str, code: str) -> str:
    return _json({"error": message, "code": code})


def _active_engine() -> Mirage | None:
    engine = state._current_engine
    if isinstance(engine, Mirage) and bool(getattr(engine, "_passkey_mode", False)):
        return engine
    return None


def _route(client: Any) -> str:
    """Return only the extension route; never return its page text or fields."""
    url = client.get_url()
    if not isinstance(url, str):
        return "unknown"
    fragment = urlsplit(url).fragment.split("?", 1)[0]
    return fragment[:128] if fragment.startswith("/") else "unknown"


async def _close_session() -> None:
    global _setup_session, _setup_engine
    session, engine = _setup_session, _setup_engine
    _setup_session = None
    _setup_engine = None
    if session is None:
        return
    await asyncio.to_thread(session.close)
    if engine is not None and session.original_target in getattr(engine, "_sessions", {}):
        try:
            await engine.switch_page(session.original_target)
        except Exception:  # noqa: BLE001 - browser may have been stopped meanwhile
            logger.debug("could not restore tab after Bitwarden setup", exc_info=True)


@mcp.tool(name="kahin_passkey_setup_open", annotations=_RW)
async def passkey_setup_open() -> str:
    """Open Bitwarden in a dedicated visible tab for the one human login."""
    global _setup_session, _setup_engine
    async with _setup_lock:
        async with _healer_ref.safe("kahin_passkey_setup_open"):
            engine = _active_engine()
            if engine is None:
                return _error(
                    "Start Mirage with passkey_mode=true before Bitwarden setup.",
                    "passkey_engine_unavailable",
                )
            if bool(getattr(engine, "_headless", True)):
                return _error(
                    "First Bitwarden login requires a visible browser. Restart with headless=false.",
                    "visible_browser_required",
                )
            if _setup_session is not None and _setup_engine is engine and _setup_session.is_alive():
                try:
                    return _json({"status": "open", "route": await asyncio.to_thread(_route, _setup_session.client)})
                except Exception:  # noqa: BLE001 - stale Marionette session
                    await _close_session()
            elif _setup_session is not None:
                await _close_session()

            try:
                await engine.ensure_page()
                session = await asyncio.to_thread(PasskeyUISession.open, engine)
                _setup_session, _setup_engine = session, engine
                result = await asyncio.to_thread(prepare_login_ui, session.client, session.popup_url)
                return _json({**result, "setup_tab": "open"})
            except Exception:  # noqa: BLE001 - never echo popup contents or credentials
                logger.exception("Bitwarden setup UI could not open")
                await _close_session()
                return _error("Could not open the Bitwarden setup UI.", "passkey_setup_unavailable")


@mcp.tool(name="kahin_passkey_setup_status", annotations=_RO)
async def passkey_setup_status() -> str:
    """Read the setup route without reading Bitwarden account fields."""
    async with _setup_lock:
        async with _healer_ref.safe("kahin_passkey_setup_status"):
            if _setup_session is None or _setup_engine is not _active_engine():
                await _close_session()
                return _error("No Bitwarden setup tab is open.", "passkey_setup_not_open")
            try:
                route = await asyncio.to_thread(_route, _setup_session.client)
            except Exception:  # noqa: BLE001 - browser or tab closed
                await _close_session()
                return _error("Bitwarden setup tab is unavailable.", "passkey_setup_unavailable")
            return _json({"status": "open", "route": route})


@mcp.tool(name="kahin_passkey_setup_finish", annotations=_RW)
async def passkey_setup_finish() -> str:
    """Set Never timeout and enable passkeys after the human signs in."""
    async with _setup_lock:
        async with _healer_ref.safe("kahin_passkey_setup_finish"):
            if _setup_session is None or _setup_engine is not _active_engine():
                await _close_session()
                return _error("No Bitwarden setup tab is open.", "passkey_setup_not_open")
            try:
                result = await asyncio.to_thread(
                    configure_unlocked_vault,
                    _setup_session.client,
                    _setup_session.popup_url,
                )
            except Exception:  # noqa: BLE001 - never echo popup contents or credentials
                logger.exception("Bitwarden setup could not complete")
                return _error("Could not complete Bitwarden setup.", "passkey_setup_failed")
            if result.get("status") == "configured":
                await _close_session()
            return _json(result)


@mcp.tool(name="kahin_passkey_setup_close", annotations=_RW)
async def passkey_setup_close() -> str:
    """Close only the setup tab; keep the Kahin browser running."""
    async with _setup_lock:
        async with _healer_ref.safe("kahin_passkey_setup_close"):
            await _close_session()
            return _json({"status": "closed"})
