"""One window inventory for the agent and for the vault code (P0-2).

An agent that cannot see a window cannot reason about it. Mirage only knows
Juggler page targets; the Bitwarden FIDO2 popout and the extension popup are
Firefox windows that Juggler does not report, and a tab-modal JS dialog is
only visible as a Juggler event. This module merges all three into ONE list:

- Juggler targets (tabs Kahin drives) with their open JS dialogs,
- Marionette window handles with their URLs (the same reader
  ``kahin.vault_login`` uses to find the ``/fido2`` popout — there is no second
  scanner that could disagree with it),
- the tab-modal alert Marionette sees in each window.

Marionette serves one WebDriver session per browser and its client is not
thread-safe, so every Marionette use in Kahin goes through
``MARIONETTE_LOCK``. Status probes never wait behind a long vault action:
they take the lock with a short timeout and report ``busy`` (with the owner
and since-when) instead of blocking. Nothing here reads page text, form
fields or vault items; only URLs, extension routes and dialog messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit


FIDO2_ROUTE_MARKER = "/fido2"
_MARIONETTE_CONNECT_TIMEOUT = 2.0
_ALERT_TEXT_LIMIT = 200
_URL_LIMIT = 512

MARIONETTE_LOCK = threading.RLock()
_holder: dict[str, Any] = {"owner": None, "since": None, "depth": 0}
_holder_guard = threading.Lock()
# The live Marionette client of an open vault/setup session, if any. A probe
# reuses it instead of opening a competing WebDriver session.
_registered: dict[str, Any] = {"client": None, "owner": None}


# --- classification ---------------------------------------------------------


def extension_route(url: Any) -> str | None:
    """The ``#/route`` of an extension page (no query), else None."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme != "moz-extension":
        return None
    fragment = parts.fragment.split("?", 1)[0]
    return fragment[:128] if fragment.startswith("/") else None


def classify_url(url: Any) -> str:
    """``bitwarden_fido2`` | ``extension`` | ``about`` | ``blank`` | ``page``."""
    if not isinstance(url, str) or not url or url == "about:blank":
        return "blank"
    scheme = urlsplit(url).scheme
    if scheme == "moz-extension":
        route = extension_route(url) or ""
        return "bitwarden_fido2" if FIDO2_ROUTE_MARKER in route or FIDO2_ROUTE_MARKER in url else "extension"
    if scheme in ("about", "chrome", "resource"):
        return "about"
    return "page"


def fido2_handle(windows: list[dict[str, Any]] | list[tuple[str, str]]) -> str | None:
    """The Marionette handle of the Bitwarden FIDO2 popout, if one is open."""
    for item in windows:
        if isinstance(item, dict):
            handle, url = item.get("handle"), item.get("url")
        else:
            handle, url = item
        if isinstance(url, str) and FIDO2_ROUTE_MARKER in url and isinstance(handle, str):
            return handle
    return None


# --- Marionette access --------------------------------------------------------


@contextlib.contextmanager
def marionette_guard(owner: str, timeout: float | None = None) -> Iterator[bool]:
    """Hold ``MARIONETTE_LOCK``; yields False when it stayed busy past ``timeout``."""
    acquired = MARIONETTE_LOCK.acquire(timeout=-1 if timeout is None else max(0.0, timeout))
    if not acquired:
        yield False
        return
    with _holder_guard:
        if _holder["depth"] == 0:
            _holder["owner"] = owner
            _holder["since"] = time.time()
        _holder["depth"] += 1
    try:
        yield True
    finally:
        with _holder_guard:
            _holder["depth"] -= 1
            if _holder["depth"] == 0:
                _holder["owner"] = None
                _holder["since"] = None
        MARIONETTE_LOCK.release()


def locked_call(owner: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``fn`` (a sync Marionette routine) under ``MARIONETTE_LOCK``.

    Meant for ``asyncio.to_thread(locked_call, "kahin_vault_login", fn, ...)``.
    """
    with marionette_guard(owner):
        return fn(*args, **kwargs)


def lock_holder() -> dict[str, Any] | None:
    """Who holds the Marionette lock and since when (epoch seconds)."""
    with _holder_guard:
        if not _holder["depth"]:
            return None
        return {"owner": _holder["owner"], "since": _holder["since"]}


def register_client(client: Any, owner: str) -> None:
    _registered["client"] = client
    _registered["owner"] = owner


def unregister_client(client: Any) -> None:
    if _registered.get("client") is client:
        _registered["client"] = None
        _registered["owner"] = None


def _current_url(client: Any) -> str | None:
    for reader in ("get_url", "current_url"):
        try:
            value = getattr(client, reader)
            value = value() if callable(value) else value
        except Exception:
            continue
        if isinstance(value, str):
            return value
    return None


def _alert_text(client: Any) -> str | None:
    """The tab-modal dialog text in the current window, None when there is none."""
    try:
        alert = client.switch_to_alert()
        text = alert.text
    except Exception:
        return None
    return text[:_ALERT_TEXT_LIMIT] if isinstance(text, str) else ""


def _switch_quietly(client: Any, handle: str) -> None:
    """Switch Marionette's command target without raising/focusing the window.

    A status probe must never steal the visible browser's focus.
    """
    try:
        client.switch_to_window(handle, focus=False)
    except TypeError:  # clients without the focus keyword
        client.switch_to_window(handle)


def marionette_windows(client: Any, *, read_alerts: bool = False) -> list[dict[str, Any]] | None:
    """Every Marionette window as ``{handle, url, kind, route, current[, alert]}``.

    None when the handles themselves are unreadable. The caller's current
    window is restored before returning. ``kahin.vault_login`` locates the
    FIDO2 popout through this exact function.
    """
    try:
        original = client.current_window_handle
        handles = list(client.window_handles)
    except Exception:
        return None

    windows: list[dict[str, Any]] = []
    try:
        for handle in handles:
            url = ""
            alert: str | None = None
            try:
                _switch_quietly(client, handle)
                url = _current_url(client) or ""
                if read_alerts:
                    alert = _alert_text(client)
            except Exception:
                pass
            entry: dict[str, Any] = {
                "handle": handle,
                "url": url[:_URL_LIMIT],
                "kind": classify_url(url),
                "route": extension_route(url),
                "current": handle == original,
            }
            if read_alerts:
                entry["alert"] = alert
            windows.append(entry)
    finally:
        try:
            _switch_quietly(client, original)
        except Exception:
            pass
    return windows


_NATIVE_WEBAUTHN_SCRIPT = """
const found = [];
const windows = Services.wm.getEnumerator("navigator:browser");
while (windows.hasMoreElements()) {
  const win = windows.getNext();
  const notes = (win.PopupNotifications && win.PopupNotifications._currentNotifications) || [];
  for (const note of notes) {
    if (note && typeof note.id === "string" && note.id.startsWith("webauthn")) { found.push(note.id); }
  }
}
return found;
"""


def _native_webauthn_prompts(client: Any) -> list[str] | None:
    """Firefox's own WebAuthn doorhanger ids; None when chrome context is closed."""
    try:
        with client.using_context("chrome"):
            found = client.execute_script(_NATIVE_WEBAUTHN_SCRIPT)
    except Exception:
        return None
    if not isinstance(found, list):
        return None
    return [str(item)[:64] for item in found[:8]]


def _transient_client(port: int) -> Any:
    from kahin.passkey_session import RETRY_INTERVAL_SECONDS, _connect_client  # noqa: PLC0415

    return _connect_client(port, _MARIONETTE_CONNECT_TIMEOUT, RETRY_INTERVAL_SECONDS)


def scan_marionette(engine: Any, *, lock_timeout: float = 0.5) -> dict[str, Any]:
    """Synchronous Marionette scan (run it in a thread).

    Returns ``{"status": "ok"|"busy"|"unavailable"|"error", "windows": [...],
    "nativeWebauthnPrompts": [...]|None}``. ``busy`` names the lock holder.
    """
    port = getattr(engine, "_marionette_port", None)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return {"status": "unavailable", "reason": "marionette_disabled", "windows": []}
    with marionette_guard("window_inventory", timeout=lock_timeout) as acquired:
        if not acquired:
            return {"status": "busy", "holder": lock_holder(), "windows": []}
        client = _registered.get("client")
        transient = None
        if client is None or not getattr(client, "session_id", None):
            try:
                client = transient = _transient_client(port)
            except Exception as exc:  # noqa: BLE001 - inventory never raises
                return {"status": "error", "reason": f"connect: {type(exc).__name__}", "windows": []}
        try:
            windows = marionette_windows(client, read_alerts=True)
            if windows is None:
                return {"status": "error", "reason": "window_handles_unreadable", "windows": []}
            return {
                "status": "ok",
                "windows": windows,
                "nativeWebauthnPrompts": _native_webauthn_prompts(client),
            }
        finally:
            if transient is not None:
                try:
                    transient.delete_session()
                except Exception:
                    pass


# --- the merged inventory -----------------------------------------------------


def _juggler_tabs(engine: Any) -> list[dict[str, Any]]:
    sessions = dict(getattr(engine, "_sessions", {}) or {})
    infos = dict(getattr(engine, "_target_infos", {}) or {})
    current = getattr(engine, "_current_target", None)
    open_dialogs = getattr(engine, "open_dialogs", None)
    tabs: list[dict[str, Any]] = []
    for target_id, session_id in sessions.items():
        info = infos.get(target_id) or {}
        url = info.get("url") if isinstance(info.get("url"), str) else ""
        dialogs = open_dialogs(session_id) if callable(open_dialogs) else []
        tabs.append({
            "targetId": target_id,
            "url": url[:_URL_LIMIT],
            "kind": classify_url(url),
            "route": extension_route(url),
            "current": target_id == current,
            "dialogs": [
                {key: item.get(key) for key in ("dialogId", "type", "message")}
                for item in dialogs
            ],
        })
    return tabs


def merge(tabs: list[dict[str, Any]], windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One list: a Marionette window with a tab's URL is that tab."""
    merged: list[dict[str, Any]] = []
    unmatched = list(windows)
    for tab in tabs:
        entry = {**tab, "handle": None, "alert": None, "sources": ["juggler"]}
        for index, window in enumerate(unmatched):
            if window.get("url") and window.get("url") == tab.get("url"):
                entry["handle"] = window.get("handle")
                entry["alert"] = window.get("alert")
                entry["sources"].append("marionette")
                unmatched.pop(index)
                break
        merged.append(entry)
    for window in unmatched:
        merged.append({
            "targetId": None,
            "url": window.get("url"),
            "kind": window.get("kind"),
            "route": window.get("route"),
            "current": False,
            "dialogs": [],
            "handle": window.get("handle"),
            "alert": window.get("alert"),
            "sources": ["marionette"],
        })
    return merged


async def collect(engine: Any, *, marionette: bool = True, timeout: float = 3.0) -> dict[str, Any]:
    """The single inventory: windows, JS/native dialogs and WebAuthn state."""
    tabs = _juggler_tabs(engine)
    scan: dict[str, Any] = {"status": "skipped", "windows": []}
    if marionette:
        try:
            scan = await asyncio.wait_for(asyncio.to_thread(scan_marionette, engine), timeout=timeout)
        except asyncio.TimeoutError:
            scan = {"status": "busy", "reason": "scan_timeout", "holder": lock_holder(), "windows": []}
        except Exception as exc:  # noqa: BLE001 - inventory never raises
            scan = {"status": "error", "reason": type(exc).__name__, "windows": []}
    windows = merge(tabs, list(scan.get("windows") or []))
    native = scan.get("nativeWebauthnPrompts")
    fido2 = fido2_handle(list(scan.get("windows") or []))
    js_dialogs = sum(len(item.get("dialogs") or []) for item in windows)
    alerts = sum(1 for item in windows if item.get("alert") is not None and not item.get("dialogs"))
    if fido2 is not None or native:
        webauthn: bool | None = True
    elif scan.get("status") == "ok":
        webauthn = False
    else:
        webauthn = None  # not observable without a Marionette view
    marionette_state = {key: scan[key] for key in ("status", "reason", "holder") if key in scan}
    return {
        "windows": windows,
        "summary": {
            "windowCount": len(windows),
            "jsDialogs": js_dialogs,
            "nativeAlerts": alerts,
            "fido2Popout": fido2,
            "nativeWebauthnPrompts": native,
            "webauthnPending": webauthn,
        },
        "marionette": marionette_state,
    }


async def webauthn_evidence(engine: Any, *, timeout: float = 2.0) -> dict[str, Any]:
    """Observed WebAuthn state for stall diagnosis (``pending`` may be None)."""
    inventory = await collect(engine, timeout=timeout)
    summary = inventory["summary"]
    return {
        "pending": summary.get("webauthnPending"),
        "fido2Popout": summary.get("fido2Popout"),
        "nativeWebauthnPrompts": summary.get("nativeWebauthnPrompts"),
        "marionette": inventory.get("marionette"),
    }
