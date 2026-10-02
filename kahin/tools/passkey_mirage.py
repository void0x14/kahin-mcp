"""One-time Bitwarden setup in the active Mirage browser."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import orjson

from kahin import _state as state
from kahin import window_inventory
from kahin._mcp import mcp
from kahin.passkey_session import PasskeyUISession
from kahin.passkey_ui import (
    configure_unlocked_vault,
    configure_vault_timeout_root,
    prepare_login_ui,
)
from kahin.the_twins.mirage import Mirage
from kahin.tools._common import _RO, _RW, _healer_ref
from kahin.tools.vault_login_mirage import vault_unlocked_since


logger = logging.getLogger(__name__)
_setup_lock = asyncio.Lock()
_setup_session: PasskeyUISession | None = None
_setup_engine: Mirage | None = None
_OWNER = "kahin_passkey_setup"


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
    window_inventory.unregister_client(session.client)
    await asyncio.to_thread(window_inventory.locked_call, _OWNER, session.close)
    if engine is not None and session.original_target in getattr(engine, "_sessions", {}):
        try:
            await engine.switch_page(session.original_target)
        except Exception:  # noqa: BLE001 - browser may have been stopped meanwhile
            logger.debug("could not restore tab after Bitwarden setup", exc_info=True)


async def _bitwarden_windows(engine: Mirage | None) -> list[dict[str, Any]]:
    """Bitwarden extension windows from the shared inventory (routes only)."""
    if engine is None:
        return []
    try:
        inventory = await window_inventory.collect(engine, timeout=3.0)
    except Exception:  # noqa: BLE001 - enrichment only
        return []
    return [
        {"handle": item.get("handle"), "kind": item.get("kind"), "route": item.get("route")}
        for item in inventory.get("windows") or []
        if item.get("kind") in ("extension", "bitwarden_fido2")
    ]


@mcp.tool(name="kahin_passkey_setup_open", annotations=_RW)
async def passkey_setup_open(force: bool = False) -> str:
    """Open Bitwarden in a dedicated visible tab for the one human login.

    Setup-only: once this engine has read the vault unlocked (a successful
    kahin_vault_login), the call is a no-op naming since when; ``force=true``
    opens the setup tab anyway (e.g. after Bitwarden logged out).
    """
    global _setup_session, _setup_engine
    async with _setup_lock:
        async with _healer_ref.safe("kahin_passkey_setup_open"):
            engine = _active_engine()
            if engine is None:
                return _error(
                    "Start Mirage with passkey_mode=true before Bitwarden setup.",
                    "passkey_engine_unavailable",
                )
            unlocked_since = vault_unlocked_since(engine)
            if unlocked_since is not None and not force:
                return _json({
                    "status": "no-op",
                    "outcome": (
                        f"no-op: unlocked since {unlocked_since}, setup-only tool — the vault is already "
                        "open; kahin_passkey_setup_* exists only for the one-time human Bitwarden login"
                    ),
                    "next": "Use kahin_vault_login for site logins; pass force=true only if Bitwarden logged out.",
                    "vaultUnlockedSince": unlocked_since,
                })
            if bool(getattr(engine, "_headless", True)):
                return _error(
                    "First Bitwarden login requires a visible browser. Restart with headless=false.",
                    "visible_browser_required",
                )
            if _setup_session is not None and _setup_engine is engine and _setup_session.is_alive():
                try:
                    route = await asyncio.to_thread(
                        window_inventory.locked_call, _OWNER, _route, _setup_session.client,
                    )
                    return _json({"status": "open", "route": route})
                except Exception:  # noqa: BLE001 - stale Marionette session
                    await _close_session()
            elif _setup_session is not None:
                await _close_session()

            try:
                await engine.ensure_page()
                session = await asyncio.to_thread(
                    window_inventory.locked_call, _OWNER, PasskeyUISession.open, engine,
                )
                _setup_session, _setup_engine = session, engine
                window_inventory.register_client(session.client, _OWNER)
                result = await asyncio.to_thread(
                    window_inventory.locked_call, _OWNER, prepare_login_ui, session.client, session.popup_url,
                )
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
                # P3: "not open" alone left the agent guessing where Bitwarden
                # is. Report the extension windows that ARE open (routes only,
                # from the shared inventory) and whether the vault was read.
                engine = _active_engine()
                windows = await _bitwarden_windows(engine)
                return _json({
                    "error": "No Bitwarden setup tab is open.",
                    "code": "passkey_setup_not_open",
                    "bitwardenWindows": windows,
                    "routes": sorted({item["route"] for item in windows if item.get("route")}),
                    "vaultUnlockedSince": vault_unlocked_since(engine) if engine is not None else None,
                    "hint": (
                        "A /fido2 route means a passkey popout is waiting (kahin_vault_login handles it); "
                        "the setup tab is only for the one-time human login (kahin_passkey_setup_open)."
                    ),
                })
            try:
                route = await asyncio.to_thread(
                    window_inventory.locked_call, _OWNER, _route, _setup_session.client,
                )
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
                # Root write first: it needs no UI clicking and persists on its
                # own. The UI path is only a fallback for when the extension
                # storage has no active account or cannot be reached.
                root_result = await asyncio.to_thread(
                    window_inventory.locked_call, _OWNER,
                    configure_vault_timeout_root,
                    _setup_session.client,
                )
                if root_result.get("status") == "configured":
                    result = {**root_result, "method": "storage_root"}
                else:
                    ui_result = await asyncio.to_thread(
                        window_inventory.locked_call, _OWNER,
                        configure_unlocked_vault,
                        _setup_session.client,
                        _setup_session.popup_url,
                    )
                    result = {**ui_result, "method": "ui"}
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
