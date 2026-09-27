"""Code-driven Bitwarden account resolver (docs/state-modes.md §2.4).

When a site needs an account the order is fixed here and never chosen by the
agent: a passkey first, then credentials, then "add it to Bitwarden". Every
call runs against the Bitwarden popup extension page, whose WebExtension API is
reachable only through ``window.wrappedJSObject.browser`` (``window.browser``
and ``window.chrome`` are Xray-hidden). Secrets never leave Bitwarden: the fill
path triggers the extension's own autofill and nothing here reads or returns a
password, master password or vault item.

Per-origin passkey detection IS decided here, read-only. The tab-bound
autofill list cannot be used for it (a row's content button IS the Autofill
control, so clicking it triggers a real fill), but the FULL vault list can:
every row exposes ``button[aria-label^="View item"]``, which only navigates to
the read-only detail route ``#/view-cipher?cipherId=…``. ``_passkey_for_origin``
opens that list with a unique query (forcing a real document load), narrows it
by typing the site host into ``bit-search input``, then opens each matching
row's detail view and reads ``[data-testid="login-passkey"]`` plus the
scheme-less host from the readonly ``[data-testid="login-website"]`` input.
A passkey cipher whose host matches the site host makes ``resolve_login``
return ``passkey``; ``select_passkey`` still acts on the FIDO2 popout
Bitwarden opens when the SITE requests a passkey. The scan's search filter is
cleared again before it returns (``_restore_vault_list``): the filter survives
a popup document load and otherwise leaves the next tab-bound read unreadable.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit


_CSS = "css selector"
_WAIT_SECONDS = 8.0
_POLL_SECONDS = 0.1
_SCRIPT_TIMEOUT_MS = 8000


# Rows MUST come from the tab-bound autofill list, never from the full vault.
# The bound view renders the whole vault list too, so a global ``bit-item``
# query reports another site's cipher (and its passkey) as this site's.
_AUTOFILL_HOST_SELECTOR = "app-autofill-vault-list-items"
_ROW_FILTER_SCRIPT = (
    f"const host = document.querySelector('{_AUTOFILL_HOST_SELECTOR}');"
    "const items = host ? Array.from(host.querySelectorAll('bit-item')).filter("
    "function (el) { return !!el.querySelector('[data-testid=\"item-name\"]'); }) : [];"
)

_VAULT_ROWS_SCRIPT = (
    _ROW_FILTER_SCRIPT
    + "return {"
    "count: items.length,"
    f"autofill: !!document.querySelector('{_AUTOFILL_HOST_SELECTOR}'),"
    "list: !!document.querySelector("
    "'app-vault-list-items-container, app-vault-popup-list-table'),"
    # The bound autofill view renders its list container inside the autofill
    # host and shows NO dedicated empty marker when nothing matches (live-
    # measured: ``vault-empty-vault`` never appears). "Settled empty" is
    # therefore the container being rendered with zero named rows.
    "empty: items.length === 0 && !!document.querySelector("
    f"'{_AUTOFILL_HOST_SELECTOR} app-vault-list-items-container'),"
    "detail: !!document.querySelector('[data-testid=\"item-details-list\"]')"
    "};"
)

# The extension API is reachable only through the page's real window object:
# ``window.browser``/``window.chrome`` are Xray-hidden from Marionette.
_JS_API = (
    "const w = (typeof window !== 'undefined') ? window : globalThis;"
    "const page = w.wrappedJSObject || w;"
    "const api = w.browser || w.chrome || page.browser || page.chrome;"
)

_TABS_QUERY_SCRIPT = (
    "const callback = arguments[arguments.length - 1];"
    "const origin = arguments[0];"
    "try {"
    + _JS_API
    + "if (!api || !api.tabs) {callback({ok: false, error: 'extension api unavailable'}); return; }"
    "Promise.resolve(api.tabs.query({})).then(function (tabs) {"
    "const tab = (tabs || []).find(function (t) {return t && typeof t.url === 'string' && t.url.indexOf(origin) === 0;});"
    "callback({ok: true, tabId: tab ? tab.id : null});"
    "}, function (error) {callback({ok: false, error: String((error && error.message) || error)});});"
    "} catch (error) {callback({ok: false, error: String((error && error.message) || error)});}"
)

_FILL_SCRIPT = (
    "const callback = arguments[arguments.length - 1];"
    "const origin = arguments[0];"
    "try {"
    + _JS_API
    + "if (!api || !api.tabs) {callback({ok: false, error: 'extension api unavailable'}); return; }"
    "Promise.resolve(api.tabs.query({})).then(function (tabs) {"
    "const tab = (tabs || []).find(function (t) {return t && typeof t.url === 'string' && t.url.indexOf(origin) === 0;});"
    "if (!tab) { callback({ok: true, status: 'no_tab'}); return; }"
    "Promise.resolve(api.tabs.get(tab.id)).then(function (full) {"
    "return api.tabs.sendMessage(tab.id, {command: 'collectPageDetails', tab: full, sender: 'autofill_cmd'});"
    "}).then(function () { callback({ok: true, status: 'triggered'}); },"
    "function (error) {callback({ok: false, error: String((error && error.message) || error)});});"
    "}, function (error) {callback({ok: false, error: String((error && error.message) || error)});});"
    "} catch (error) {callback({ok: false, error: String((error && error.message) || error)});}"
)

_FIDO2_ROW_SELECTOR = "app-fido2-cipher-row button"
_FIDO2_ROUTE_MARKER = "/fido2"

# --- per-origin passkey detection (read-only) -----------------------------
# The tab-bound autofill list has no read-only detail control: a row's content
# button IS the Autofill control, so it can never be clicked while detecting.
# The FULL vault list is different: every row exposes a
# ``button[aria-label^="View item"]`` that only navigates to the read-only
# detail route (``#/view-cipher?cipherId=…``). That is the signal used here.
_SEARCH_INPUT_SELECTOR = "bit-search input"
_VIEW_ITEM_SELECTOR = 'button[aria-label^="View item"]'
# Live-measured: [data-testid="item-details-list"] renders only for items that
# carry folder/collection details, so it is absent on a reachable cipher detail
# and must not gate the scan. The login fields are the reliable detail signal.
_DETAIL_MARKER_SCRIPT = (
    "!!(document.querySelector('[data-testid=\"login-password\"]')"
    " || document.querySelector('[data-testid=\"login-username\"]')"
    " || document.querySelector('[data-testid=\"login-website\"]')"
    " || document.querySelector('[data-testid=\"login-passkey\"]'))"
)

# Hard cap on rows opened per scan and on the whole scan's wall-clock time. The
# list is virtualized and narrowed by the search, so a real scan is small; an
# over-cap list stays indeterminate rather than risk a wrong "no passkey".
_PASSKEY_MAX_ROWS = 12
_PASSKEY_BUDGET_SECONDS = 20.0
# Upper bound on waiting for the filtered list to stop changing. A virtualized
# list that never settles is treated as unreachable, never scanned half-rendered.
_SETTLE_SECONDS = 2.0

# The full vault list (``cdk-virtual-scroll-viewport``) with its per-row
# read-only "View item" controls; ``bit-item`` is the row host element.
_LIST_STATE_SCRIPT = (
    "const rows = document.querySelectorAll('bit-item');"
    f"const views = document.querySelectorAll('{_VIEW_ITEM_SELECTOR}');"
    "return {"
    "list: !!document.querySelector("
    "'cdk-virtual-scroll-viewport, app-vault-list-items-container, app-vault-popup-list-table'),"
    "rows: rows.length,"
    "views: views.length,"
    f"detail: {_DETAIL_MARKER_SCRIPT}"
    "};"
)

# The read-only detail view: ``login-passkey`` present iff the cipher has a
# passkey; ``login-website`` is a readonly input whose value is the host.
_PASSKEY_DETAIL_SCRIPT = (
    "const site = document.querySelector('[data-testid=\"login-website\"]');"
    "return {"
    f"detail: {_DETAIL_MARKER_SCRIPT},"
    "passkey: !!document.querySelector('[data-testid=\"login-passkey\"]'),"
    "host: (site && typeof site.value === 'string') ? site.value : ''"
    "};"
)


def _search_script(host: str) -> str:
    """Filter the vault list by ``host`` via the native setter + input events.

    ``bit-search``'s input id is ``search-id-0`` (so ``input#search`` never
    matches) and setting ``.value`` directly does not reach Angular's model,
    hence the prototype setter and the bubbling ``input``/``change`` events.
    """
    return (
        "const input = document.querySelector('bit-search input');"
        "if (!input) { return {ok: false, error: 'no search input'}; }"
        "const setter = Object.getOwnPropertyDescriptor("
        "Object.getPrototypeOf(input), 'value').set;"
        f"setter.call(input, {json.dumps(host)});"
        "input.dispatchEvent(new Event('input', {bubbles: true}));"
        "input.dispatchEvent(new Event('change', {bubbles: true}));"
        "return {ok: true};"
    )


def _open_view_script(index: int) -> str:
    """Click the nth row's read-only "View item" control (never Autofill)."""
    return (
        f"const views = Array.from(document.querySelectorAll('{_VIEW_ITEM_SELECTOR}'));"
        f"const target = views[{index}];"
        "if (!target) { return {ok: false, error: 'no such row'}; }"
        "target.click();"
        "return {ok: true};"
    )



def _site_origin(site_url: str) -> str:
    """Return ``scheme://host`` for a site URL, unchanged when it has no host."""
    if not isinstance(site_url, str):
        return ""
    parts = urlsplit(site_url)
    if not parts.scheme or not parts.netloc:
        return site_url
    return f"{parts.scheme}://{parts.netloc}"


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


def _invoke(client: Any, script: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    """Run an async extension-page script; return its payload or None when unreachable."""
    try:
        payload = client.execute_async_script(script, args, script_timeout=_SCRIPT_TIMEOUT_MS)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return payload


def _window_urls(client: Any) -> list[tuple[str, str]] | None:
    """Return ``(handle, url)`` for every window, or None when handles are unreadable."""
    try:
        original = client.current_window_handle
        handles = list(client.window_handles)
    except Exception:
        return None

    urls: list[tuple[str, str]] = []
    try:
        for handle in handles:
            try:
                client.switch_to_window(handle)
                urls.append((handle, _current_url(client) or ""))
            except Exception:
                urls.append((handle, ""))
    finally:
        try:
            client.switch_to_window(original)
        except Exception:
            pass
    return urls


def _fido2_handle(urls: list[tuple[str, str]]) -> str | None:
    """Pick the Bitwarden FIDO2 popout window out of ``(handle, url)`` pairs."""
    return next((handle for handle, url in urls if _FIDO2_ROUTE_MARKER in url), None)


def _wait_for_element(client: Any, selector: str, timeout: float = _WAIT_SECONDS) -> Any | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.find_element(_CSS, selector)
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def _site_tab_id(client: Any, site_url: str) -> tuple[bool, Any]:
    """Return ``(reachable, tab_id)``; ``reachable`` is False when the API is unreachable."""
    payload = _invoke(client, _TABS_QUERY_SCRIPT, (_site_origin(site_url),))
    if payload is None:
        return False, None
    return True, payload.get("tabId")


def _popup_base(client: Any) -> str | None:
    """Return the extension popup base URL, or None when the page is not the popup."""
    url = _current_url(client)
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme != "moz-extension" or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _vault_url(base: str, tab_id: Any) -> str:
    """Bind the popup to the site tab so the list holds only matching ciphers.

    Bitwarden reads ``senderTabId`` when the popup DOCUMENT loads, so a
    fragment-only navigation is silently ignored and the tab-bound autofill
    host stays empty (live-measured: the verdict then degrades to
    ``unavailable``). The URL therefore carries a unique query value before the
    ``#`` to force a real document load on every call.
    """
    stamp = time.monotonic_ns()
    return f"{base}?bind={tab_id}&t={stamp}#/tabs/vault?senderTabId={tab_id}"


def _vault_rows(client: Any) -> dict[str, Any] | None:
    """Return the bound vault's rendered row state, or None when unreadable."""
    try:
        state = client.execute_script(_VAULT_ROWS_SCRIPT)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _wait_for_rows(client: Any, deadline: float) -> dict[str, Any] | None:
    """Wait until the vault list view has settled, or the deadline passes.

    "Settled" means the tab-bound autofill list is present and either rendered
    rows or showed its empty state. Returning None is a genuine unreachability
    signal, never "no rows" — and it is also returned when the autofill host is
    missing, because the full-vault list must never be mistaken for this site's
    matching set.
    """
    while True:
        state = _vault_rows(client)
        if state is not None and state.get("autofill") is True and (
            state.get("count") or state.get("empty")
        ):
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)










def _count_credentials(client: Any, tab_id: Any) -> int | None:
    """Count ciphers for the bound tab, or None when the vault view is indeterminate."""
    base = _popup_base(client)
    if base is None:
        return None
    try:
        client.navigate(_vault_url(base, tab_id))
    except Exception:
        return None

    rows = _wait_for_rows(client, time.monotonic() + _WAIT_SECONDS)
    if rows is None:
        return None
    return int(rows.get("count") or 0)


def _host_only(value: Any) -> str:
    """Return the scheme-less, lowercased host of ``value`` (``""`` when absent)."""
    text = (value or "").strip().lower() if isinstance(value, str) else ""
    if not text:
        return ""
    if "://" in text:
        return (urlsplit(text).hostname or "").lower()
    return text.split("/", 1)[0].rstrip(".")


def _full_vault_url(base: str) -> str:
    """A full-vault list URL with a unique query so each call is a fresh document.

    No ``senderTabId`` is bound here: this is the vault-wide list, whose rows
    carry the read-only "View item" control the tab-bound view lacks.
    """
    return f"{base}?p={time.monotonic_ns()}#/tabs/vault"


def _list_state(client: Any) -> dict[str, Any] | None:
    """Return the full vault list's rendered state, or None when unreadable."""
    try:
        state = client.execute_script(_LIST_STATE_SCRIPT)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _read_detail(client: Any) -> dict[str, Any] | None:
    """Return the open detail view's passkey/host state, or None when unreadable."""
    try:
        state = client.execute_script(_PASSKEY_DETAIL_SCRIPT)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _wait_for_list(client: Any, deadline: float) -> dict[str, Any] | None:
    """Wait for the full vault list to render its ``bit-item`` rows.

    None is unreachability, never "empty": the initial load of a settled vault
    must have rendered rows before the search can narrow them.
    """
    while True:
        state = _list_state(client)
        if state is not None and state.get("list") is True and int(state.get("rows") or 0) > 0:
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def _wait_for_settled_list(client: Any, deadline: float) -> dict[str, Any] | None:
    """Wait until the filtered list's visible row count stops changing.

    A virtualized list must reflect the search before its rows are enumerated;
    a list that never settles before the deadline is treated as unreachable, so
    a half-rendered list is never scanned as if it were complete.
    """
    previous = None
    while True:
        state = _list_state(client)
        if state is not None and state.get("list") is True:
            views = int(state.get("views") or 0)
            if previous == views:
                return state
            previous = views
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def _wait_for_detail(client: Any, deadline: float) -> dict[str, Any] | None:
    """Wait for a detail view to render after a "View item" click."""
    while True:
        state = _read_detail(client)
        if state is not None and state.get("detail") is True:
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def _type_search(client: Any, host: str) -> bool:
    try:
        state = client.execute_script(_search_script(host))
    except Exception:
        return False
    return isinstance(state, dict) and state.get("ok") is True


def _open_view(client: Any, index: int) -> bool:
    try:
        state = client.execute_script(_open_view_script(index))
    except Exception:
        return False
    return isinstance(state, dict) and state.get("ok") is True


def _open_filtered_list(client: Any, base: str, host: str, deadline: float) -> int | None:
    """Load the full vault list, filter it to ``host``, return its visible row count.

    None means the list or the search input was unreachable.
    """
    try:
        client.navigate(_full_vault_url(base))
    except Exception:
        return None
    loaded = _wait_for_list(client, min(deadline, time.monotonic() + _WAIT_SECONDS))
    if loaded is None:
        return None
    if not _type_search(client, host):
        return None
    state = _wait_for_settled_list(client, min(deadline, time.monotonic() + _SETTLE_SECONDS))
    if state is None:
        return None
    views = state.get("views")
    return int(views) if isinstance(views, int) else None


def _restore_vault_list(client: Any, base: str, filtered_views: int) -> None:
    """Clear the vault search a passkey scan typed, best-effort.

    Live-measured: the scan's ``bit-search`` filter SURVIVES a popup document
    load, and while it is applied the tab-bound autofill host stops rendering —
    a later ``resolve_login`` then reads the vault as unreadable. Reloading the
    vault-wide list, clearing the input and waiting for the unfiltered row count
    to settle restores the tab-bound view, so the resolver stays re-entrant.
    """
    try:
        client.navigate(_full_vault_url(base))
    except Exception:
        return
    if _wait_for_list(client, time.monotonic() + _WAIT_SECONDS) is None:
        return
    if not _type_search(client, ""):
        return
    deadline = time.monotonic() + _SETTLE_SECONDS
    previous = None
    while True:
        state = _list_state(client)
        rows = int(state.get("rows") or 0) if state is not None else 0
        if rows == previous and rows > 0:
            return
        previous = rows
        if time.monotonic() >= deadline:
            return
        time.sleep(_POLL_SECONDS)


def _passkey_for_origin(client: Any, origin: str) -> bool | None:
    """Read-only scan for a passkey cipher matching ``origin``'s host.

    Returns ``True`` when a cipher whose ``login-website`` host equals the
    origin's host carries a passkey, ``False`` when the scan completed with no
    such cipher, and ``None`` when the list, the search input or a detail view
    was unreachable, the list exceeded the read-only row cap, or the budget ran
    out. ``None`` is deliberately never reported as "no passkey".
    """
    base = _popup_base(client)
    if base is None:
        return None
    host = _host_only(origin)
    if not host:
        return None

    deadline = time.monotonic() + _PASSKEY_BUDGET_SECONDS
    views = _open_filtered_list(client, base, host, deadline)
    try:
        if views is None:
            return None
        if views > _PASSKEY_MAX_ROWS:
            # More matching ciphers than the read-only cap: the scan would be
            # incomplete, so the verdict stays indeterminate.
            return None

        for index in range(views):
            if time.monotonic() >= deadline:
                return None
            if index > 0:
                # A detail view was left open; reopen the filtered list (fresh
                # unique query) rather than relying on history.back().
                reopened = _open_filtered_list(client, base, host, deadline)
                if reopened is None:
                    return None
                if index >= reopened:
                    return False
            if not _open_view(client, index):
                return None
            detail = _wait_for_detail(client, min(deadline, time.monotonic() + _WAIT_SECONDS))
            if detail is None:
                return None
            if detail.get("passkey") is True and _host_only(detail.get("host")) == host:
                return True
        return False
    finally:
        # Never leave the vault filtered: the filter breaks the next bound read.
        _restore_vault_list(client, base, views or 0)


def resolve_login(client: Any, site_url: str) -> dict[str, Any]:
    """Decide the login method for ``site_url`` in the fixed contract order.

    Returns ``{"method": "passkey"|"credentials"|"user_required"|"unavailable",
    "count": <n>, ...}``. The matching-cipher count is measured first: ``None``
    (vault unreadable) is ``unavailable`` and ``0`` is ``user_required``. When
    ciphers exist, a read-only full-vault scan (``_passkey_for_origin``) looks
    for a passkey cipher for the site's host: a hit is ``passkey``, while both a
    miss and an unreachable scan are ``credentials`` (the ciphers are known to
    exist, so an unreachable passkey scan must never downgrade the verdict).

    ``unavailable`` means the decision could not be made (extension API, site
    tab or the tab-bound vault view unreachable) and is never ``user_required``.
    """
    if not isinstance(site_url, str) or not site_url.strip():
        # An empty origin would match every tab (``indexOf("") === 0``) and bind
        # the verdict to an arbitrary tab, so it is rejected outright.
        return {"method": "unavailable", "count": 0, "reason": "invalid_site_url"}
    reachable, tab_id = _site_tab_id(client, site_url)
    if not reachable:
        return {"method": "unavailable", "count": 0, "reason": "extension_unreachable"}
    if tab_id is None:
        return {"method": "unavailable", "count": 0, "reason": "no_site_tab"}

    count = _count_credentials(client, tab_id)
    if count is None:
        return {"method": "unavailable", "count": 0, "reason": "vault_unreadable"}
    if count == 0:
        return {"method": "user_required", "count": 0}

    if _passkey_for_origin(client, _site_origin(site_url)) is True:
        return {"method": "passkey", "count": count}
    return {"method": "credentials", "count": count}


def fill_credentials(client: Any, site_url: str) -> dict[str, Any]:
    """Trigger Bitwarden autofill for the site tab; never returns a secret."""
    if not isinstance(site_url, str) or not site_url.strip():
        return {"status": "unavailable"}
    reachable, tab_id = _site_tab_id(client, site_url)
    if not reachable:
        return {"status": "unavailable"}
    if tab_id is None:
        return {"status": "no_tab"}

    if _count_credentials(client, tab_id) == 0:
        return {"status": "no_credentials"}

    payload = _invoke(client, _FILL_SCRIPT, (_site_origin(site_url),))
    if payload is None:
        return {"status": "unavailable"}
    if payload.get("status") == "no_tab":
        return {"status": "no_tab"}
    if payload.get("status") == "triggered":
        return {"status": "triggered"}
    return {"status": "unavailable"}


def select_passkey(client: Any) -> dict[str, Any]:
    """Click the credential row in the Bitwarden FIDO2 popout.

    Returns ``selected`` after a click, ``no_popout`` when a FIDO2 popout is
    open but has no selectable row, ``auto`` when no popout is open (the
    background short-circuited the request), or ``unavailable`` when the
    Marionette window handles cannot be read.
    """
    urls = _window_urls(client)
    if urls is None:
        return {"status": "unavailable"}

    popout = _fido2_handle(urls)
    if popout is None:
        return {"status": "auto"}

    original = client.current_window_handle
    try:
        client.switch_to_window(popout)
        row = _wait_for_element(client, _FIDO2_ROW_SELECTOR)
        if row is None:
            return {"status": "no_popout"}
        row.click()
        return {"status": "selected"}
    except Exception:
        return {"status": "unavailable"}
    finally:
        try:
            client.switch_to_window(original)
        except Exception:
            pass
