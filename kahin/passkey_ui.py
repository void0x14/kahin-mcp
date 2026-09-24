"""Small Marionette helpers for Bitwarden's Firefox popup UI.

These helpers only inspect public UI state and click visible controls. They do
not read or enter account credentials.
"""

from __future__ import annotations

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
