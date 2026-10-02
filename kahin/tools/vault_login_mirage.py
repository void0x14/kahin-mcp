"""Code-driven Bitwarden login for the active Mirage browser.

The agent calls one tool (``kahin_vault_login``) with the site URL; the fixed
contract order (passkey, then credentials, then ask the user) lives entirely in
``kahin.vault_login`` and is never chosen by the agent (docs/state-modes.md
§2.4 and §5). Secrets never cross this boundary: the resolver and fill path
keep them inside Bitwarden, and this module returns only method/action/status
strings.

Login-once contract (P1-1): a site whose login already reached a terminal
action (passkey row selected / autofill triggered) is not re-run; the call is
a structured no-op naming when the vault has been unlocked since and what was
done, because re-running the resolver cannot observe the site's state.
``force=true`` re-runs it after the site visibly logged out. Every answer
carries one readable ``outcome`` field ("<code>: <what happened>") next to
the machine ``status``; agents read ``outcome`` and ``next``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import orjson

from kahin import _state as state
from kahin import window_inventory
from kahin._mcp import mcp
from kahin.passkey_session import PasskeyUISession
from kahin.the_twins.mirage import Mirage, MirageCommandTimeout
from kahin.tools._common import _RW, _healer_ref
from kahin.vault_login import fill_credentials, resolve_login, select_passkey


logger = logging.getLogger(__name__)
_login_lock = asyncio.Lock()
_login_session: PasskeyUISession | None = None
_login_engine: Mirage | None = None

# Bound the site URL before it reaches Marionette or the vault query.
_MAX_SITE_URL_LENGTH = 2048
_OWNER = "kahin_vault_login"

# Per-engine ledger: when the vault was first read unlocked and the last
# terminal login action per site origin. Reset whenever the engine changes.
_ledger: dict[str, Any] = {"engine": None, "unlockedSince": None, "sites": {}}
_TERMINAL_STATUSES = frozenset({"selected", "triggered"})

# (method, status) -> (outcome code, what happened, what to do next). The
# single readable field replaces guessing what "auto" or "no_popout" means.
_OUTCOMES: dict[tuple[str, str], tuple[str, str, str]] = {
    ("passkey", "selected"): (
        "passkey_selected",
        "the passkey row in Bitwarden's FIDO2 popout was clicked; the site completes sign-in",
        "Observe the site (kahin_agent_status / kahin_mirage_snapshot) to confirm the signed-in page.",
    ),
    ("passkey", "auto"): (
        "no_fido2_popout",
        "no Bitwarden FIDO2 popout is open: the site has not requested a passkey yet, "
        "or Bitwarden finished the ceremony without one",
        "Trigger the site's own passkey sign-in (its button), then call kahin_vault_login once; "
        "observe the site first — if it is already signed in there is nothing left to do.",
    ),
    ("passkey", "no_popout"): (
        "fido2_popout_without_rows",
        "the FIDO2 popout is open but lists no passkey this site accepts",
        "Check the site account in Bitwarden; do not retry until it has a passkey for this site.",
    ),
    ("passkey", "unavailable"): (
        "windows_unreadable",
        "the browser window list could not be read, so the FIDO2 popout could not be located",
        "Inspect kahin_agent_status windows; do not retry blindly.",
    ),
    ("credentials", "triggered"): (
        "autofill_triggered",
        "Bitwarden autofill was sent to the site tab",
        "Observe the form; submit it if the site does not auto-submit.",
    ),
    ("credentials", "no_tab"): (
        "site_tab_missing",
        "no open tab matches the site origin",
        "Navigate a tab to the site first, then call kahin_vault_login once.",
    ),
    ("credentials", "no_credentials"): (
        "no_credentials",
        "the vault has no credentials bound to this site tab",
        "Add the account to Bitwarden, then retry.",
    ),
    ("credentials", "unavailable"): (
        "autofill_unavailable",
        "the Bitwarden extension API could not trigger autofill",
        "Inspect kahin_agent_status windows and kahin_passkey_setup_status.",
    ),
    ("user_required", "user_required"): (
        "user_required",
        "no passkey or saved credentials for this site in Bitwarden",
        "Add the account to Bitwarden, then retry.",
    ),
}

_FIDO2_PROBE = r"""(() => {
  const w = window.wrappedJSObject || window;
  const c = w.navigator && w.navigator.credentials;
  if (!c) { return JSON.stringify({api: false, origin: location.origin}); }
  const fn = c.get;
  const own = Object.prototype.hasOwnProperty.call(c, "get");
  let src = "";
  try { src = Function.prototype.toString.call(fn); } catch (e) { src = ""; }
  return JSON.stringify({
    api: true, own: own, native: /\[native code\]/.test(src),
    secure: !!w.isSecureContext, origin: location.origin
  });
})()"""


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _site_origin(site_url: str) -> str:
    parts = urlsplit(site_url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _engine_ledger(engine: Any) -> dict[str, Any]:
    if _ledger["engine"] is not engine:
        _ledger["engine"] = engine
        _ledger["unlockedSince"] = None
        _ledger["sites"] = {}
    return _ledger


def vault_unlocked_since(engine: Any) -> str | None:
    """When this engine's vault was first read unlocked (ISO-8601), if ever."""
    return _ledger["unlockedSince"] if _ledger["engine"] is engine else None


def _answer(method: str, action: str, status: str | None, **extra: Any) -> dict[str, Any]:
    code, happened, follow = _OUTCOMES.get(
        (method, status or ""),
        (str(status or "unknown"), f"{method} action ended with status {status!r}", "Inspect kahin_agent_status."),
    )
    return {
        "method": method,
        "action": action,
        "status": status,
        "outcome": f"{code}: {happened}",
        "next": follow,
        **extra,
    }


async def _fido2_self_check(engine: Any, origin: str) -> dict[str, Any]:
    """P2-1: is Bitwarden's FIDO2 page script live on the site tab?

    ``injected`` is True/False from a live probe of the tab's
    ``navigator.credentials.get`` (Bitwarden replaces it with its own
    function; a native one means WebAuthn would go to Firefox's prompt), or
    None when no site tab could be probed.
    """
    sessions = getattr(engine, "_sessions", None)
    infos = getattr(engine, "_target_infos", None)
    if not isinstance(sessions, dict) or not isinstance(infos, dict) or not callable(getattr(engine, "call", None)):
        return {"injected": None, "reason": "engine_has_no_tab_map"}
    current = getattr(engine, "_current_target", None)
    candidates = [
        target_id for target_id, info in infos.items()
        if isinstance(info, dict) and isinstance(info.get("url"), str)
        and info["url"].lower().startswith(origin) and target_id in sessions
    ]
    if not candidates:
        return {"injected": None, "reason": "no_site_tab"}
    target_id = current if current in candidates else candidates[0]
    try:
        raw = await engine.call(
            "Runtime.evaluate",
            {"expression": _FIDO2_PROBE, "returnByValue": True},
            session_id=sessions[target_id],
        )
    except MirageCommandTimeout as exc:
        return {"injected": None, "reason": f"probe_stalled:{exc.cause}", "targetId": target_id}
    except Exception as exc:  # noqa: BLE001 - classification only
        return {"injected": None, "reason": f"probe_failed:{type(exc).__name__}", "targetId": target_id}
    value = ((raw or {}).get("result") or {}).get("value")
    try:
        probe = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        probe = None
    if not isinstance(probe, dict):
        return {"injected": None, "reason": "probe_unreadable", "targetId": target_id}
    injected = bool(probe.get("api")) and bool(probe.get("own")) and not probe.get("native")
    return {
        "injected": injected,
        "targetId": target_id,
        "secureContext": bool(probe.get("secure")),
        "probe": {key: probe.get(key) for key in ("api", "own", "native")},
    }


async def _close_session() -> None:
    global _login_session, _login_engine
    session, engine = _login_session, _login_engine
    _login_session = None
    _login_engine = None
    if session is None:
        return
    window_inventory.unregister_client(session.client)
    await asyncio.to_thread(window_inventory.locked_call, _OWNER, session.close)
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
    session = await asyncio.to_thread(window_inventory.locked_call, _OWNER, PasskeyUISession.open, engine)
    _login_session, _login_engine = session, engine
    window_inventory.register_client(session.client, _OWNER)
    return session


@mcp.tool(name="kahin_vault_login", annotations=_RW)
async def vault_login(site_url: str, force: bool = False) -> str:
    """Log in to a site using the Bitwarden account, in the fixed order.

    Passkey first, then saved credentials, then ask the user to add the
    account. The agent never chooses the method. Login-once: after a site's
    login reached a terminal action, further calls are a no-op that says when
    the vault was unlocked and what was done (``force=true`` re-runs only
    after the site visibly logged out). Read ``outcome`` and ``next``.
    """
    async with _login_lock:
        async with _healer_ref.safe("kahin_vault_login"):
            invalid = _validate_site_url(site_url)
            if invalid is not None:
                return invalid
            if not isinstance(force, bool):
                return _error("force must be a boolean.", "invalid_argument")

            engine_error = _engine_error()
            if engine_error is not None:
                return engine_error
            engine = _passkey_engine()
            if engine is None:  # defensive: _engine_error already proved otherwise
                return _error(
                    "Bitwarden is unavailable; start Mirage with passkey_mode=true.",
                    "passkey_engine_unavailable",
                )

            origin = _site_origin(site_url)
            ledger = _engine_ledger(engine)
            previous = ledger["sites"].get(origin)
            if previous is not None and not force:
                since = ledger["unlockedSince"] or previous.get("at")
                return _json({
                    "method": previous.get("method"),
                    "action": "none",
                    "status": "no-op",
                    "outcome": (
                        f"no-op: unlocked since {since}; {origin} already reached "
                        f"{previous.get('outcome', '').split(':', 1)[0]} at {previous.get('at')}. "
                        "kahin_vault_login is login-once per site, not a status probe."
                    ),
                    "next": (
                        "Observe the site (kahin_agent_status / kahin_mirage_snapshot). Call again with "
                        "force=true only after the site visibly logged out or rejected the login."
                    ),
                    "previous": previous,
                    "vaultUnlockedSince": since,
                })

            try:
                session = await _ensure_session(engine)
            except Exception:  # noqa: BLE001 - never echo popup contents or credentials
                logger.exception("Bitwarden vault UI could not open")
                await _close_session()
                return _error("Could not open the Bitwarden vault UI.", "vault_login_unavailable")

            try:
                resolved = await asyncio.to_thread(
                    window_inventory.locked_call, _OWNER, resolve_login, session.client, site_url,
                )
                method = resolved.get("method") if isinstance(resolved, dict) else None
                if method in ("passkey", "credentials", "user_required") and ledger["unlockedSince"] is None:
                    ledger["unlockedSince"] = _now_iso()
                answer: dict[str, Any]
                if method == "passkey":
                    check = await _fido2_self_check(engine, origin)
                    if check.get("injected") is False:
                        return _json({
                            "error": f"fido2 not injected on {origin}",
                            "code": "fido2_not_injected",
                            "method": "passkey",
                            "outcome": (
                                f"fido2_not_injected: Bitwarden's WebAuthn bridge is not live on {origin}; "
                                "a passkey request there goes to Firefox's own prompt, not Bitwarden"
                            ),
                            "next": (
                                "Enable passkeys (kahin_passkey_setup_finish), then reload the site tab and "
                                "call kahin_vault_login once."
                            ),
                            "fido2Check": check,
                        })
                    status = await asyncio.to_thread(
                        window_inventory.locked_call, _OWNER, select_passkey, session.client,
                    )
                    answer = _answer("passkey", "select_passkey", _status(status), fido2Check=check)
                elif method == "credentials":
                    status = await asyncio.to_thread(
                        window_inventory.locked_call, _OWNER, fill_credentials, session.client, site_url,
                    )
                    answer = _answer("credentials", "fill_credentials", _status(status))
                elif method == "user_required":
                    answer = _answer(
                        "user_required", "none", "user_required",
                        message=(
                            "No passkey or saved credentials for this site in Bitwarden. "
                            "Add the account to Bitwarden, then retry."
                        ),
                    )
                else:
                    reason = (resolved.get("reason") if isinstance(resolved, dict) else None) or "unavailable"
                    return _json({
                        "method": "unavailable",
                        "action": "none",
                        "status": "unavailable",
                        "reason": reason,
                        "outcome": f"unavailable: the vault could not be read ({reason}); no account claim is made",
                        "next": "Inspect kahin_agent_status windows and kahin_passkey_setup_status before retrying.",
                        "message": (
                            "Could not determine the login method for this site. "
                            "The vault could not be read; no account claim is made."
                        ),
                    })
                answer["vaultUnlockedSince"] = ledger["unlockedSince"]
                if answer.get("status") in _TERMINAL_STATUSES:
                    ledger["sites"][origin] = {
                        "method": answer["method"],
                        "status": answer["status"],
                        "outcome": answer["outcome"],
                        "at": _now_iso(),
                        "monotonic": round(time.monotonic(), 3),
                    }
                return _json(answer)
            except Exception:  # noqa: BLE001 - never echo popup contents or credentials
                logger.exception("Bitwarden vault login failed")
                await _close_session()
                return _error("Bitwarden vault login failed.", "vault_login_failed")
