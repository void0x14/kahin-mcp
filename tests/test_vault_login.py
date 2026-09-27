"""Unit tests for the code-driven Bitwarden login resolver (state-modes §2.4).

No browser is involved: a fake Marionette client mimics the Bitwarden popup
extension page (``browser.tabs`` API, window handles and the bound-vault DOM)
and returns canned payloads from ``execute_async_script`` / ``execute_script``.

The passkey decision is VAULT-BASED and read-only: the FULL vault list is
loaded, filtered to the site host, and each matching row's read-only detail
view is opened and tested for the ``login-passkey`` signal plus the
``login-website`` host. The fake models that list/search/detail cycle and
records every action so the tests can prove the scan is strictly read-only.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest

from kahin import vault_login


class FakeElement:
    def __init__(self, selector: str, on_click=None) -> None:
        self.selector = selector
        self._on_click = on_click

    def click(self) -> None:
        if self._on_click is not None:
            self._on_click()


class FakeVaultClient:
    """Fake Marionette client standing in for the Bitwarden popup page."""

    def __init__(
        self,
        *,
        site_url: str = "https://example.com/login",
        tabs=None,
        site_window: bool = True,
        credential_rows: int = 2,
        passkey_indices=(),
        row_hosts=None,
        vault_settled: bool = True,
        autofill_host: bool = True,
        detail_reachable: bool = True,
        list_reachable: bool = True,
        search_reachable: bool = True,
        api_reachable: bool = True,
        fill_status: str = "triggered",
        fido2_popout: bool = False,
        fido2_rows: bool = True,
        window_handles_error: bool = False,
    ) -> None:
        self.site_url = site_url
        self.tabs = tabs if tabs is not None else [{"id": 7, "url": site_url}]
        self.credential_rows = credential_rows
        self.passkey_indices = set(passkey_indices)
        self.row_hosts = list(row_hosts) if row_hosts is not None else None
        self.vault_settled = vault_settled
        self.autofill_host = autofill_host
        self.detail_reachable = detail_reachable
        self.list_reachable = list_reachable
        self.search_reachable = search_reachable
        self.api_reachable = api_reachable
        self.fill_status = fill_status
        self.fido2_popout = fido2_popout
        self.fido2_rows = fido2_rows
        self.window_handles_error = window_handles_error

        # The full vault list rows: each has a ``login-website`` host and
        # whether its detail view carries ``[data-testid="login-passkey"]``.
        default_host = urlsplit(site_url).hostname or "example.com"
        hosts = self.row_hosts if self.row_hosts is not None else [default_host] * credential_rows
        self.rows = [
            {"host": host, "passkey": index in self.passkey_indices}
            for index, host in enumerate(hosts)
        ]

        self.popup_url = "moz-extension://test/popup/index.html"
        self._fido2_url = self.popup_url + "?uilocation=popout#/fido2?sessionId=abc"
        self.windows = {"popup": self.popup_url}
        if site_window:
            self.windows["site"] = site_url
        if fido2_popout:
            self.windows["fido2"] = self._fido2_url
        self.current = "popup"

        # --- observable actions (used to prove read-only behaviour) ---
        self.navigated: list[str] = []
        self.scripts: list[str] = []
        self.opened_rows: list[int] = []
        self.clicked: list[str] = []
        self.mutated = False  # set by the autofill/fill path only

        self.view = "vault"
        self.searched = False
        self.query = ""

    # --- window/tab surface ---

    @property
    def current_window_handle(self) -> str:
        return self.current

    @property
    def window_handles(self) -> list[str]:
        if self.window_handles_error:
            raise RuntimeError("window handles unavailable")
        return list(self.windows)

    def switch_to_window(self, handle: str) -> None:
        if handle not in self.windows:
            raise KeyError(handle)
        self.current = handle

    def get_url(self) -> str:
        return self.windows[self.current]

    def navigate(self, url: str) -> None:
        self.navigated.append(url)
        self.windows["popup"] = url
        self.current = "popup"
        self.view = "vault"
        # Live-measured: the vault search text SURVIVES a popup document load,
        # so ``searched``/``query`` are deliberately NOT reset here.

    def find_element(self, by: str, selector: str) -> FakeElement:
        assert by == "css selector"
        if selector == vault_login._FIDO2_ROW_SELECTOR and self.fido2_rows:
            return FakeElement(selector, on_click=lambda: self.clicked.append(selector))
        raise LookupError(selector)

    # --- extension API scripts ---

    def execute_async_script(self, script: str, script_args=(), **_kwargs):
        self.scripts.append(script)
        if not self.api_reachable:
            return {"ok": False, "error": "extension api unavailable"}
        if "tabs.sendMessage" in script:
            self.mutated = True
            tab = self._site_tab()
            if tab is None:
                return {"ok": True, "status": "no_tab"}
            return {"ok": True, "status": self.fill_status}
        if "tabs.query" in script:
            tab = self._site_tab()
            return {"ok": True, "tabId": tab["id"] if tab else None}
        raise AssertionError(f"unexpected async script: {script}")

    # --- DOM scripts ---

    def execute_script(self, script: str):
        if "app-autofill-vault-list-items" in script:
            return self._rows()
        if "bit-item" in script:
            return self._list_state()
        if "bit-search" in script:
            return self._search(script)
        if "View item" in script:
            return self._open_row(script)
        if "login-passkey" in script:
            return self._detail_state()
        return self._rows()

    # --- modelled vault DOM ---

    def _rows(self):
        if self.view == "vault":
            if self.query:
                # Live-measured: while a vault search filter is applied the
                # tab-bound autofill host stops rendering, so the bound read is
                # unreadable — and the filter survives a document load.
                return {
                    "count": 0,
                    "autofill": False,
                    "list": True,
                    "empty": False,
                    "detail": False,
                }
            if not self.vault_settled:
                return {
                    "count": 0,
                    "autofill": self.autofill_host,
                    "list": False,
                    "empty": False,
                    "detail": False,
                }
            return {
                "count": self.credential_rows,
                "autofill": self.autofill_host,
                "list": True,
                "empty": self.credential_rows == 0,
                "detail": False,
            }
        return {"count": 0, "autofill": self.autofill_host, "list": False, "empty": False, "detail": True}

    def _visible_rows(self):
        """Rows the full vault list currently shows (the search narrows them)."""
        if not self.searched or not self.query:
            return list(self.rows)
        query = self.query.lower()
        return [row for row in self.rows if query in row["host"].lower()]

    def _list_state(self):
        if not self.list_reachable:
            return {"list": False, "rows": 0, "views": 0, "detail": False}
        visible = self._visible_rows()
        return {"list": True, "rows": len(visible), "views": len(visible), "detail": False}

    def _search(self, script: str):
        if not self.search_reachable:
            return {"ok": False, "error": "no search input"}
        match = re.search(r'setter\.call\(input, (".*?")\)', script)
        assert match is not None, script
        self.query = json.loads(match.group(1))
        self.searched = True
        return {"ok": True}

    def _open_row(self, script: str):
        match = re.search(r"views\[(\d+)\]", script)
        assert match is not None, script
        index = int(match.group(1))
        visible = self._visible_rows()
        if index >= len(visible):
            return {"ok": False, "error": "no such row"}
        self.opened_rows.append(index)
        if self.detail_reachable:
            self.view = ("detail", visible[index])
        else:
            self.view = "broken_detail"
        return {"ok": True}

    def _detail_state(self):
        if isinstance(self.view, tuple) and self.view[0] == "detail":
            row = self.view[1]
            return {"detail": True, "passkey": row["passkey"], "host": row["host"]}
        return {"detail": False, "passkey": False, "host": ""}

    def _site_tab(self):
        origin = vault_login._site_origin(self.site_url)
        for tab in self.tabs:
            if isinstance(tab.get("url"), str) and tab["url"].startswith(origin):
                return tab
        return None


def _fill_scripts(client: FakeVaultClient) -> list[str]:
    return [script for script in client.scripts if "tabs.sendMessage" in script]


# --------------------------------------------------------------------------
# resolve_login: the matching-cipher count decides credentials vs user_required
# --------------------------------------------------------------------------


def test_matching_ciphers_resolve_credentials():
    client = FakeVaultClient(credential_rows=3)

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "credentials", "count": 3}
    # Ciphers exist but carry no passkey, so every row was scanned read-only.
    assert client.opened_rows == [0, 1, 2]
    assert client.mutated is False
    assert _fill_scripts(client) == []


def test_resolve_binds_the_popup_to_the_site_tab():
    client = FakeVaultClient(credential_rows=2)

    vault_login.resolve_login(client, client.site_url)

    assert client.navigated
    assert "senderTabId=7" in client.navigated[0]
    assert "#/tabs/vault" in client.navigated[0]
    # A unique query value forces a real document load: a fragment-only
    # navigation leaves the tab binding unapplied (live-measured).
    assert "?bind=" in client.navigated[0]


def test_no_matching_rows_requires_the_user():
    client = FakeVaultClient(credential_rows=0)
    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "user_required", "count": 0}
    assert client.opened_rows == []


# --------------------------------------------------------------------------
# unreachable is never "missing"
# --------------------------------------------------------------------------


def test_unsettled_vault_is_unavailable_not_user_required(monkeypatch):
    monkeypatch.setattr(vault_login, "_WAIT_SECONDS", 0.1)
    client = FakeVaultClient(credential_rows=0, vault_settled=False)

    result = vault_login.resolve_login(client, client.site_url)

    assert result["method"] == "unavailable"
    assert result["reason"] == "vault_unreadable"


def test_resolve_without_site_tab_is_unavailable_not_user_required():
    client = FakeVaultClient(tabs=[], site_window=False)

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "unavailable", "count": 0, "reason": "no_site_tab"}


def test_resolve_with_unreachable_extension_api_is_unavailable():
    client = FakeVaultClient(api_reachable=False)

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "unavailable", "count": 0, "reason": "extension_unreachable"}


# --------------------------------------------------------------------------
# resolve_login is read-only and per-origin passkey aware
# --------------------------------------------------------------------------


def test_resolve_performs_no_non_read_action():
    client = FakeVaultClient(credential_rows=2)

    vault_login.resolve_login(client, client.site_url)

    assert client.mutated is False
    assert _fill_scripts(client) == []
    # The first navigation is the tab-bound count; the passkey scan then loads
    # the vault-wide list (no senderTabId) with a unique query each time.
    assert "senderTabId=7" in client.navigated[0] and "?bind=" in client.navigated[0]
    assert all("#/tabs/vault" in url for url in client.navigated)


def test_passkey_cipher_for_the_site_host_resolves_passkey():
    client = FakeVaultClient(credential_rows=1, passkey_indices={0})

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "passkey", "count": 1}
    assert client.opened_rows == [0]


def test_passkey_cipher_for_another_host_resolves_credentials():
    # Origin safety: a passkey cipher for a lookalike host must not decide this
    # site's method even though the host search still returns the row.
    client = FakeVaultClient(
        credential_rows=1,
        passkey_indices={0},
        row_hosts=["example.com.evil.test"],
    )

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "credentials", "count": 1}


def test_ciphers_without_a_passkey_resolve_credentials():
    client = FakeVaultClient(credential_rows=2, passkey_indices=set())

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "credentials", "count": 2}
    assert client.opened_rows == [0, 1]


def test_detail_marker_is_not_the_unreliable_item_details_list():
    # Live-measured: [data-testid="item-details-list"] renders only for items
    # that carry folder/collection details, so it is absent on a reachable
    # cipher detail. Gating the scan on it made a real passkey scan report the
    # detail as unreachable and downgrade to credentials. The marker must be
    # the login fields instead.
    script = vault_login._PASSKEY_DETAIL_SCRIPT

    assert "item-details-list" not in script
    assert "login-password" in script
    assert "login-username" in script
    assert "login-website" in script
    assert "login-passkey" in script


@pytest.mark.parametrize(
    "flags",
    [
        {"list_reachable": False},
        {"search_reachable": False},
        {"detail_reachable": False},
    ],
)
def test_unreachable_passkey_scan_stays_credentials(monkeypatch, flags):
    # The ciphers are known to exist, so an unreachable passkey scan must never
    # downgrade the verdict to unavailable (nor to user_required).
    monkeypatch.setattr(vault_login, "_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(vault_login, "_PASSKEY_BUDGET_SECONDS", 0.3)
    client = FakeVaultClient(credential_rows=2, **flags)

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "credentials", "count": 2}


def test_zero_matching_ciphers_requires_the_user_without_scanning():
    client = FakeVaultClient(credential_rows=0, passkey_indices={0})

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "user_required", "count": 0}
    assert client.opened_rows == []


def test_passkey_scan_never_triggers_autofill_or_a_fill():
    client = FakeVaultClient(credential_rows=1, passkey_indices={0})

    vault_login.resolve_login(client, client.site_url)

    assert client.mutated is False
    assert _fill_scripts(client) == []
    assert "collectPageDetails" not in " ".join(client.scripts)


def test_passkey_scan_leaves_no_vault_filter_applied():
    # Live-measured: the scan's ``bit-search`` filter survives a popup document
    # load and stops the tab-bound autofill host from rendering. Left applied,
    # it made the NEXT ``resolve_login`` read the vault as unreadable, so the
    # scan must clear it again before it returns.
    client = FakeVaultClient(credential_rows=2, passkey_indices={1})

    vault_login.resolve_login(client, client.site_url)

    assert client.query == ""
    # The last navigation is the vault-wide reload the restore performs.
    assert "senderTabId" not in client.navigated[-1]
    assert "#/tabs/vault" in client.navigated[-1]


def test_resolve_login_is_reentrant_for_the_same_origin():
    # Two sequential calls in one popup session must agree: the first call's
    # passkey scan used to leave the vault filtered, so the second call's bound
    # count came back unreadable instead of "passkey".
    client = FakeVaultClient(credential_rows=1, passkey_indices={0})

    first = vault_login.resolve_login(client, client.site_url)
    second = vault_login.resolve_login(client, client.site_url)

    assert first == {"method": "passkey", "count": 1}
    assert second == first



# --------------------------------------------------------------------------
# fill_credentials (unchanged contract)
# --------------------------------------------------------------------------


def test_fill_credentials_triggers_the_verified_autofill_recipe():
    client = FakeVaultClient(credential_rows=2)

    result = vault_login.fill_credentials(client, client.site_url)

    assert result == {"status": "triggered"}
    fill_scripts = _fill_scripts(client)
    assert len(fill_scripts) == 1
    assert "collectPageDetails" in fill_scripts[0]
    assert "autofill_cmd" in fill_scripts[0]


def test_fill_credentials_reports_empty_vault_as_no_credentials():
    client = FakeVaultClient(credential_rows=0)

    result = vault_login.fill_credentials(client, client.site_url)

    assert result == {"status": "no_credentials"}
    assert _fill_scripts(client) == []


def test_fill_credentials_without_site_tab_reports_no_tab():
    client = FakeVaultClient(tabs=[], site_window=False)

    assert vault_login.fill_credentials(client, client.site_url) == {"status": "no_tab"}


def test_fill_credentials_unreachable_extension_api_is_unavailable_not_no_credentials():
    client = FakeVaultClient(api_reachable=False)

    result = vault_login.fill_credentials(client, client.site_url)

    assert result == {"status": "unavailable"}
    assert result != {"status": "no_credentials"}


# --------------------------------------------------------------------------
# select_passkey (unchanged contract)
# --------------------------------------------------------------------------


def test_select_passkey_clicks_the_fido2_row():
    client = FakeVaultClient(fido2_popout=True)

    result = vault_login.select_passkey(client)

    assert result == {"status": "selected"}
    assert client.clicked == [vault_login._FIDO2_ROW_SELECTOR]
    assert client.current == "popup"


def test_select_passkey_without_popout_reports_auto_short_circuit():
    client = FakeVaultClient(fido2_popout=False)

    assert vault_login.select_passkey(client) == {"status": "auto"}


def test_select_passkey_popout_without_rows_reports_no_popout():
    client = FakeVaultClient(fido2_popout=True, fido2_rows=False)

    assert vault_login.select_passkey(client) == {"status": "no_popout"}
    assert client.clicked == []


def test_select_passkey_unreadable_handles_is_unavailable():
    client = FakeVaultClient(window_handles_error=True)

    assert vault_login.select_passkey(client) == {"status": "unavailable"}


# --------------------------------------------------------------------------
# no secrets ever cross the boundary
# --------------------------------------------------------------------------


def test_no_result_ever_carries_a_secret():
    client = FakeVaultClient(credential_rows=1, passkey_indices=set())

    payloads = [
        vault_login.resolve_login(client, client.site_url),
        vault_login.fill_credentials(client, client.site_url),
        vault_login.select_passkey(client),
    ]

    for payload in payloads:
        assert "password" not in str(payload).lower()
        assert "secret" not in str(payload).lower()


def test_missing_autofill_host_is_unavailable_not_a_vault_wide_passkey():
    # Regression: the tab-bound view also renders the whole vault list, so a
    # vault-wide scan reported another site's passkey as this site's. Without
    # the tab-bound autofill host the verdict must be "unreadable", never
    # "passkey", and no row may be opened.
    client = FakeVaultClient(
        credential_rows=9, passkey_indices={0, 3, 7}, autofill_host=False
    )

    result = vault_login.resolve_login(client, client.site_url)

    assert result == {"method": "unavailable", "count": 0, "reason": "vault_unreadable"}
    assert client.opened_rows == []


def test_resolve_rejects_non_string_and_empty_site_url():
    # Audit FAIL 2/3: a non-string raised, and "" matched every tab because
    # ``indexOf("") === 0``. Both must be a structured "unavailable".
    client = FakeVaultClient()

    for bad in (123, None, "", "   "):
        result = vault_login.resolve_login(client, bad)
        assert result == {
            "method": "unavailable",
            "count": 0,
            "reason": "invalid_site_url",
        }, bad


def test_fill_credentials_rejects_non_string_and_empty_site_url():
    client = FakeVaultClient()

    for bad in (123, None, "", "   "):
        assert vault_login.fill_credentials(client, bad) == {"status": "unavailable"}, bad
