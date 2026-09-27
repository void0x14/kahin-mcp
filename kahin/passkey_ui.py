"""Small Marionette helpers for Bitwarden's Firefox popup UI and settings.

The UI helpers only inspect public UI state and click visible controls. The
root storage helper writes the vault timeout settings straight into the
extension's own ``browser.storage.local`` (docs/state-modes.md §4). Nothing
here reads or returns credentials, master passwords or vault items.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urldefrag


_CSS = "css selector"
_XPATH = "xpath"
_WAIT_SECONDS = 8.0
_POLL_SECONDS = 0.1


def _route_url(popup_url: str, route: str | None = None) -> str:
    base, _fragment = urldefrag(popup_url)
    return base if route is None else f"{base}#/{route.lstrip('/')}"


def _find(m: Any, selector: str, by: str = _CSS) -> Any | None:
    try:
        return m.find_element(by, selector)
    except Exception:
        return None


def _current_route(m: Any) -> str:
    try:
        current_url = m.current_url
    except Exception:
        try:
            current_url = m.execute_script("return window.location.href")
        except Exception:
            return ""
    fragment = urldefrag(str(current_url))[1].lstrip("#/")
    return fragment.split("?", 1)[0].strip("/").lower()


def _wait_for(m: Any, selector: str, *, by: str = _CSS, timeout: float = _WAIT_SECONDS) -> Any | None:
    deadline = time.monotonic() + timeout
    while True:
        element = _find(m, selector, by)
        if element is not None:
            return element
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def _text_button(m: Any, label: str) -> Any | None:
    # XPath text matching is case-insensitive and tolerates translated labels
    # when a stable English label is part of the onboarding contract.
    lowered = label.lower()
    xpath = (
        "//button[translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
        f"'{lowered}'] | //a[translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
        f"'{lowered}']"
    )
    return _find(m, xpath, _XPATH)


def _click_if_present(m: Any, label: str) -> bool:
    button = _text_button(m, label)
    if button is None:
        return False
    button.click()
    return True


def _visible_login_state(m: Any) -> str:
    if _current_route(m) == "login":
        return "login"
    if _find(m, "input[type='email'], input[autocomplete='username'], input[name='email'], #email") is not None:
        return "login"
    if _find(m, "input[type='password']") is not None:
        return "login"
    if _find(m, "#masterPassword, input[name='masterPassword']") is not None:
        return "login"
    if _find(m, "bit-session-timeout-input bit-select") is not None or _find(m, "#use-passkeys") is not None:
        return "unlocked"
    if _find(m, "button[aria-label*='lock'], button[aria-label*='Lock']") is not None:
        return "unlocked"
    return "unknown"


def prepare_login_ui(m: Any, popup_url: str) -> dict[str, str]:
    """Dismiss first-run UI and leave the popup ready for user sign-in."""
    m.navigate(_route_url(popup_url))
    deadline = time.monotonic() + _WAIT_SECONDS
    skipped = False
    logged_in = False

    while time.monotonic() < deadline:
        # The default-password-manager prompt can precede the onboarding
        # carousel. Clicking either control is a real Marionette input event.
        if not skipped and _click_if_present(m, "Skip"):
            skipped = True
            continue
        if _current_route(m) == "intro-carousel" and not logged_in and _click_if_present(m, "Log in"):
            logged_in = True
            continue

        state = _visible_login_state(m)
        if state in {"login", "unlocked"}:
            return {"status": "ready", "route": "login", "state": state}
        time.sleep(_POLL_SECONDS)

    state = _visible_login_state(m)
    route = "login" if _current_route(m) == "login" else _current_route(m) or "login"
    return {
        "status": "ready" if state in {"login", "unlocked"} else "incomplete",
        "route": route,
        "state": state,
    }


def _timeout_value(m: Any) -> str | None:
    try:
        value = m.execute_script(
            "const el = document.querySelector('bit-session-timeout-input bit-select ng-select');"
            "if (!el) return null;"
            "const label = el.querySelector('.ng-value-label');"
            "return label ? label.textContent.trim() : el.innerText.trim();"
        )
    except Exception:
        return None
    return None if value is None else str(value).strip().lower()


def _is_never(value: str | None) -> bool:
    return value in {"never", "asla"}


def _passkeys_enabled(m: Any) -> bool | None:
    try:
        value = m.execute_script(
            "const el = document.querySelector('#use-passkeys');"
            "return el ? Boolean(el.checked) : null;"
        )
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def _select_never(m: Any) -> bool:
    timeout_select = _find(m, "bit-session-timeout-input bit-select")
    if timeout_select is None:
        return False
    current = _timeout_value(m)
    if _is_never(current):
        return True

    ng_select = _find(m, "bit-session-timeout-input bit-select ng-select") or timeout_select
    ng_select.click()
    deadline = time.monotonic() + _WAIT_SECONDS
    option = None
    while time.monotonic() < deadline:
        try:
            options = m.find_elements(_CSS, ".ng-dropdown-panel .ng-option")
        except Exception:
            options = []
        for candidate in options:
            label = " ".join(str(getattr(candidate, "text", "")).split()).strip().lower()
            if label in {"never", "asla"}:
                option = candidate
                break
        if option is not None:
            break
        time.sleep(_POLL_SECONDS)
    if option is None:
        return False
    option.click()
    return True


def _confirm_if_shown(m: Any) -> bool:
    """Confirm the timeout warning through its visible dialog button."""
    deadline = time.monotonic() + 0.75
    form = None
    while time.monotonic() < deadline:
        form = _find(m, "form[bit-simple-dialog]")
        if form is not None:
            break
        time.sleep(_POLL_SECONDS)
    if form is None:
        return True
    submit = _find(m, "form[bit-simple-dialog] button[type='submit']")
    if submit is None:
        return False
    submit.click()
    return True


def _set_passkeys(m: Any) -> bool:
    checkbox = _find(m, "#use-passkeys")
    if checkbox is None:
        return False
    current = _passkeys_enabled(m)
    if current is True:
        return True
    checkbox.click()
    return True


def configure_unlocked_vault(m: Any, popup_url: str) -> dict[str, Any]:
    """Set and verify Never timeout and passkey prompts using Bitwarden UI."""
    m.navigate(_route_url(popup_url, "account-security"))
    if _visible_login_state(m) == "login":
        return {"status": "login_required", "route": "account-security", "state": "login"}
    timeout_select = _wait_for(m, "bit-session-timeout-input bit-select")
    if timeout_select is None:
        state = _visible_login_state(m)
        return {"status": "login_required", "route": "account-security", "state": state}

    if not _select_never(m):
        return {
            "status": "failed",
            "route": "account-security",
            "state": "timeout_control_unavailable",
        }
    if not _confirm_if_shown(m):
        return {
            "status": "failed",
            "route": "account-security",
            "state": "timeout_confirmation_unavailable",
        }

    deadline = time.monotonic() + _WAIT_SECONDS
    timeout_verified = False
    while time.monotonic() < deadline:
        if _is_never(_timeout_value(m)):
            timeout_verified = True
            break
        time.sleep(_POLL_SECONDS)
    if not timeout_verified:
        return {
            "status": "failed",
            "route": "account-security",
            "state": "timeout_not_verified",
        }

    m.navigate(_route_url(popup_url, "notifications"))
    checkbox = _wait_for(m, "#use-passkeys")
    if checkbox is None:
        state = _visible_login_state(m)
        return {"status": "login_required", "route": "notifications", "state": state}
    if not _set_passkeys(m):
        return {"status": "failed", "route": "notifications", "state": "passkeys_control_unavailable"}

    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if _passkeys_enabled(m) is True:
            break
        time.sleep(_POLL_SECONDS)
    else:
        return {"status": "failed", "route": "notifications", "state": "passkeys_not_verified"}

    # Route away and back to verify the extension rendered the saved settings.
    m.navigate(_route_url(popup_url, "account-security"))
    timeout_select = _wait_for(m, "bit-session-timeout-input bit-select")
    if timeout_select is None or not _is_never(_timeout_value(m)):
        return {"status": "failed", "route": "account-security", "state": "timeout_not_persisted"}

    m.navigate(_route_url(popup_url, "notifications"))
    checkbox = _wait_for(m, "#use-passkeys")
    if checkbox is None or _passkeys_enabled(m) is not True:
        return {"status": "failed", "route": "notifications", "state": "passkeys_not_persisted"}
    return {
        "status": "configured",
        "route": "notifications",
        "state": "verified",
        "settings": {"vault_timeout": "never", "ask_to_save_and_use_passkeys": True},
    }


# --- Root storage (docs/state-modes.md §4) ---------------------------------
#
# Bitwarden persists settings in browser.storage.local under logical keys and
# wraps every value as {"__json__": true, "value": JSON.stringify(actual)}.
# The popup page is a real extension page, so its JS context reaches that store
# directly; writing there disables lock/timeout with no UI clicks.

ACTIVE_ACCOUNT_KEY = "global_account_activeAccountId"
VAULT_TIMEOUT = "never"
VAULT_TIMEOUT_ACTION = "lock"
_STORAGE_TIMEOUT_MS = 5000

# Marionette's async script callback is always the last argument; the keys or
# items arrive as the first script argument.
_JS_STORAGE_API = (
    "const w = (typeof window !== 'undefined') ? window : globalThis;"
    "const page = w.wrappedJSObject || w;"
    "const api = (w.browser || w.chrome || page.browser || page.chrome).storage.local;"
)

_READ_STORAGE_SCRIPT = (
    "const callback = arguments[arguments.length - 1];"
    "const keys = arguments[0];"
    "try {"
    + _JS_STORAGE_API
    + "Promise.resolve(api.get(keys)).then("
    "function (result) { callback({ok: true, result: result}); },"
    "function (error) { callback({ok: false, error: String((error && error.message) || error)}); }"
    ");"
    "} catch (error) {"
    "callback({ok: false, error: String((error && error.message) || error)});"
    "}"
)

_WRITE_STORAGE_SCRIPT = (
    "const callback = arguments[arguments.length - 1];"
    "const items = arguments[0];"
    "try {"
    + _JS_STORAGE_API
    + "Promise.resolve(api.set(items)).then("
    "function () { callback({ok: true}); },"
    "function (error) { callback({ok: false, error: String((error && error.message) || error)}); }"
    ");"
    "} catch (error) {"
    "callback({ok: false, error: String((error && error.message) || error)});"
    "}"
)


def vault_timeout_key(user_id: str) -> str:
    """Logical storage key holding ``user_id``'s vault timeout."""
    return f"user_{user_id}_vaultTimeoutSettings_vaultTimeout"


def vault_timeout_action_key(user_id: str) -> str:
    """Logical storage key holding ``user_id``'s vault timeout action."""
    return f"user_{user_id}_vaultTimeoutSettings_vaultTimeoutAction"


def wrap_stored_value(value: Any) -> dict[str, Any]:
    """Wrap a value the way Bitwarden's storage service does before writing."""
    return {"__json__": True, "value": json.dumps(value)}


def unwrap_stored_value(stored: Any) -> Any:
    """Unwrap a stored value; return anything that is not a wrapper unchanged."""
    if isinstance(stored, dict) and stored.get("__json__") and isinstance(stored.get("value"), str):
        try:
            return json.loads(stored["value"])
        except json.JSONDecodeError:
            return stored
    return stored


def _read_storage(client: Any, keys: list[str]) -> dict[str, Any]:
    payload = client.execute_async_script(
        _READ_STORAGE_SCRIPT, (list(keys),), script_timeout=_STORAGE_TIMEOUT_MS
    )
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Bitwarden storage read failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        # A reachable store always answers with an object. Anything else means
        # the extension API was never actually reached, so callers must treat
        # it as unavailable rather than as "no account" (a false negative).
        raise RuntimeError("Bitwarden storage returned no object")
    return result


def _write_storage(client: Any, items: dict[str, Any]) -> None:
    payload = client.execute_async_script(
        _WRITE_STORAGE_SCRIPT, (items,), script_timeout=_STORAGE_TIMEOUT_MS
    )
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Bitwarden storage write failed")


def configure_vault_timeout_root(client: Any) -> dict[str, Any]:
    """Disable Bitwarden lock/timeout at the storage root and verify it.

    ``client`` must be a live Marionette connection whose current context is
    the Bitwarden popup extension page. Returns ``login_required`` when no
    account is active, ``configured`` when the read-back matches, ``failed``
    naming the mismatching setting, or ``unavailable`` when the extension
    storage cannot be reached (callers may then fall back to the UI).
    """
    try:
        account = _read_storage(client, [ACTIVE_ACCOUNT_KEY])
    except Exception:  # noqa: BLE001 - extension page/storage not reachable
        return {"status": "unavailable", "state": "storage_unavailable"}

    user_id = unwrap_stored_value(account.get(ACTIVE_ACCOUNT_KEY))
    if not isinstance(user_id, str) or not user_id:
        return {"status": "login_required"}

    timeout_key = vault_timeout_key(user_id)
    action_key = vault_timeout_action_key(user_id)
    try:
        _write_storage(
            client,
            {
                timeout_key: wrap_stored_value(VAULT_TIMEOUT),
                action_key: wrap_stored_value(VAULT_TIMEOUT_ACTION),
            },
        )
        stored = _read_storage(client, [timeout_key, action_key])
    except Exception:  # noqa: BLE001 - extension page/storage not reachable
        return {"status": "unavailable", "state": "storage_unavailable"}

    if unwrap_stored_value(stored.get(timeout_key)) != VAULT_TIMEOUT:
        return {"status": "failed", "state": "vault_timeout_mismatch"}
    if unwrap_stored_value(stored.get(action_key)) != VAULT_TIMEOUT_ACTION:
        return {"status": "failed", "state": "vault_timeout_action_mismatch"}
    return {
        "status": "configured",
        "settings": {"vault_timeout": VAULT_TIMEOUT, "vault_timeout_action": VAULT_TIMEOUT_ACTION},
    }


def vault_timeout_status(client: Any) -> dict[str, Any]:
    """Read-only probe of the root vault timeout settings.

    Run this live after a human Bitwarden login (and again after a restart) to
    confirm the settings stuck. It never writes and never reads vault contents.
    """
    try:
        account = _read_storage(client, [ACTIVE_ACCOUNT_KEY])
    except Exception:  # noqa: BLE001 - extension page/storage not reachable
        return {"status": "unavailable", "state": "storage_unavailable"}

    user_id = unwrap_stored_value(account.get(ACTIVE_ACCOUNT_KEY))
    if not isinstance(user_id, str) or not user_id:
        return {"status": "login_required"}

    timeout_key = vault_timeout_key(user_id)
    action_key = vault_timeout_action_key(user_id)
    try:
        stored = _read_storage(client, [timeout_key, action_key])
    except Exception:  # noqa: BLE001 - extension page/storage not reachable
        return {"status": "unavailable", "state": "storage_unavailable"}

    timeout_value = unwrap_stored_value(stored.get(timeout_key))
    action_value = unwrap_stored_value(stored.get(action_key))
    return {
        "status": "ok",
        "settings": {"vault_timeout": timeout_value, "vault_timeout_action": action_value},
        "never": timeout_value == VAULT_TIMEOUT,
    }
