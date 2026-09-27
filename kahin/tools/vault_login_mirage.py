"""Code-driven Bitwarden login for the active Mirage browser.

The agent calls one tool (``kahin_vault_login``) with the site URL; the fixed
contract order (passkey, then credentials, then ask the user) lives entirely in
``kahin.vault_login`` and is never chosen by the agent (docs/state-modes.md
§2.4 and §5). Secrets never cross this boundary: the resolver and fill path
keep them inside Bitwarden, and this module returns only method/action/status
strings.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.passkey_session import PasskeyUISession
from kahin.the_twins.mirage import Mirage
from kahin.tools._common import _RW, _healer_ref
from kahin.vault_login import fill_credentials, resolve_login, select_passkey


logger = logging.getLogger(__name__)
_login_lock = asyncio.Lock()
_login_session: PasskeyUISession | None = None
_login_engine: Mirage | None = None

# Bound the site URL before it reaches Marionette or the vault query.
_MAX_SITE_URL_LENGTH = 2048


def _json(payload: dict[str, Any]) -> str:
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


def _error(message: str, code: str) -> str:
    return _json({"error": message, "code": code})


def _passkey_engine() -> Mirage | None:
    """The running Mirage engine only when it carries Bitwarden (passkey_mode)."""
    engine = state._current_engine
    if isinstance(engine, Mirage) and bool(getattr(engine, "_passkey_mode", False)):
        return engine
    return None


def _engine_error() -> str | None:
    """A structured error when no passkey-capable engine is running, else None.

    Never starts or promotes a browser: Bitwarden only exists in a Mirage
    launch that requested ``passkey_mode=true``.
    """
    engine = state._current_engine
    if engine is None:
        return _error(
            "No browser engine running. Start Mirage with passkey_mode=true first.",
            "engine_unavailable",
        )
    if not isinstance(engine, Mirage) or not bool(getattr(engine, "_passkey_mode", False)):
        return _error(
            "The active engine is not Mirage with passkey_mode=true; Bitwarden is unavailable.",
            "passkey_engine_unavailable",
        )
    return None


def _validate_site_url(site_url: Any) -> str | None:
    """A structured error for an unusable site URL, else None."""
    if not isinstance(site_url, str) or not site_url.strip():
        return _error("site_url must be a non-empty string.", "invalid_site_url")
    if len(site_url) > _MAX_SITE_URL_LENGTH:
        return _error("site_url is too long.", "invalid_site_url")
    parts = urlsplit(site_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return _error("site_url must be an absolute http(s) URL.", "invalid_site_url")
    return None


def _status(payload: Any) -> str | None:
    """Only the coarse status of an action; never its payload (no secrets)."""
    return payload.get("status") if isinstance(payload, dict) else None


async def _close_session() -> None:
    global _login_session, _login_engine
    session, engine = _login_session, _login_engine
    _login_session = None
    _login_engine = None
    if session is None:
        return
    await asyncio.to_thread(session.close)
    if engine is not None and session.original_target in getattr(engine, "_sessions", {}):
        try:
            await engine.switch_page(session.original_target)
        except Exception:  # noqa: BLE001 - browser may have been stopped meanwhile
            logger.debug("could not restore tab after vault login", exc_info=True)


async def _ensure_session(engine: Mirage) -> PasskeyUISession:
    """Open or reuse the Bitwarden popup session for this engine."""
    global _login_session, _login_engine
    if _login_session is not None and _login_engine is engine and _login_session.is_alive():
        return _login_session
    if _login_session is not None:
        await _close_session()
    await engine.ensure_page()
    session = await asyncio.to_thread(PasskeyUISession.open, engine)
    _login_session, _login_engine = session, engine
    return session


@mcp.tool(name="kahin_vault_login", annotations=_RW)
async def vault_login(site_url: str) -> str:
    """Log in to a site using the Bitwarden account, in the fixed order.

    Passkey first, then saved credentials, then ask the user to add the
    account. The agent never chooses the method; this tool reports only the
    resolved method, the action taken and that action's status.
    """
    async with _login_lock:
        async with _healer_ref.safe("kahin_vault_login"):
            invalid = _validate_site_url(site_url)
            if invalid is not None:
                return invalid

            engine_error = _engine_error()
            if engine_error is not None:
                return engine_error
            engine = _passkey_engine()
            if engine is None:  # defensive: _engine_error already proved otherwise
                return _error(
                    "Bitwarden is unavailable; start Mirage with passkey_mode=true.",
                    "passkey_engine_unavailable",
                )

            try:
                session = await _ensure_session(engine)
            except Exception:  # noqa: BLE001 - never echo popup contents or credentials
                logger.exception("Bitwarden vault UI could not open")
                await _close_session()
                return _error("Could not open the Bitwarden vault UI.", "vault_login_unavailable")

            try:
                resolved = await asyncio.to_thread(resolve_login, session.client, site_url)
                method = resolved.get("method") if isinstance(resolved, dict) else None
                if method == "passkey":
                    status = await asyncio.to_thread(select_passkey, session.client)
                    return _json({
                        "method": "passkey",
                        "action": "select_passkey",
                        "status": _status(status),
                    })
                if method == "credentials":
                    status = await asyncio.to_thread(fill_credentials, session.client, site_url)
                    return _json({
                        "method": "credentials",
                        "action": "fill_credentials",
                        "status": _status(status),
                    })
                if method == "user_required":
                    return _json({
                        "method": "user_required",
                        "action": "none",
                        "status": "user_required",
                        "message": (
                            "No passkey or saved credentials for this site in Bitwarden. "
                            "Add the account to Bitwarden, then retry."
                        ),
                    })
                return _json({
                    "method": "unavailable",
                    "action": "none",
                    "status": "unavailable",
                    "reason": (resolved.get("reason") if isinstance(resolved, dict) else None)
                    or "unavailable",
                    "message": (
                        "Could not determine the login method for this site. "
                        "The vault could not be read; no account claim is made."
                    ),
                })
            except Exception:  # noqa: BLE001 - never echo popup contents or credentials
                logger.exception("Bitwarden vault login failed")
                await _close_session()
                return _error("Bitwarden vault login failed.", "vault_login_failed")
