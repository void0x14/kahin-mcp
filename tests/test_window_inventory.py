"""One window inventory shared by agent_status, stall diagnosis and vault_login (P0-2)."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from kahin import vault_login, window_inventory


class FakeAlert:
    def __init__(self, text: str | None) -> None:
        self._text = text

    @property
    def text(self) -> str:
        if self._text is None:
            raise RuntimeError("no such alert")
        return self._text


class FakeMarionette:
    session_id = "fake"

    def __init__(self, windows: dict[str, str], alerts: dict[str, str] | None = None) -> None:
        self.windows = windows
        self.alerts = alerts or {}
        self.current = next(iter(windows))
        self.deleted = False

    @property
    def current_window_handle(self) -> str:
        return self.current

    @property
    def window_handles(self) -> list[str]:
        return list(self.windows)

    def switch_to_window(self, handle: str) -> None:
        self.current = handle

    def get_url(self) -> str:
        return self.windows[self.current]

    def switch_to_alert(self) -> FakeAlert:
        return FakeAlert(self.alerts.get(self.current))

    def using_context(self, _context: str):
        raise RuntimeError("chrome context is not allowed")

    def delete_session(self) -> None:
        self.deleted = True


FIDO2 = "moz-extension://uuid/popup/index.html?uilocation=popout#/fido2?sessionId=x"
POPUP = "moz-extension://uuid/popup/index.html#/tabs/vault"


class FakeEngine:
    _marionette_port = 2828

    def __init__(self) -> None:
        self._sessions = {"t1": "s1"}
        self._target_infos = {"t1": {"url": "https://example.com/login"}}
        self._current_target = "t1"

    def open_dialogs(self, session_id=None):
        return [{"dialogId": "d1", "type": "alert", "message": "hi"}] if session_id == "s1" else []


def test_classify_and_route() -> None:
    assert window_inventory.classify_url(FIDO2) == "bitwarden_fido2"
    assert window_inventory.classify_url(POPUP) == "extension"
    assert window_inventory.classify_url("about:blank") == "blank"
    assert window_inventory.classify_url("https://example.com") == "page"
    assert window_inventory.extension_route(FIDO2) == "/fido2"
    assert window_inventory.extension_route("https://x/#/a") is None


def test_marionette_windows_restores_current_and_reads_alerts() -> None:
    client = FakeMarionette({"h1": "https://example.com/login", "h2": FIDO2}, alerts={"h1": "Leave?"})
    windows = window_inventory.marionette_windows(client, read_alerts=True)
    assert [w["kind"] for w in windows] == ["page", "bitwarden_fido2"]
    assert windows[0]["alert"] == "Leave?" and windows[1]["alert"] is None
    assert windows[0]["current"] is True
    assert client.current == "h1"


def test_vault_login_finds_fido2_through_the_same_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    real = window_inventory.marionette_windows

    def spy(client, **kwargs):
        seen.append(client)
        return real(client, **kwargs)

    monkeypatch.setattr(window_inventory, "marionette_windows", spy)
    client = FakeMarionette({"h1": "https://example.com/login", "h2": FIDO2})
    urls = vault_login._window_urls(client)
    assert seen == [client]
    assert vault_login._fido2_handle(urls) == "h2"


@pytest.mark.asyncio
async def test_collect_merges_tabs_windows_and_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeMarionette({"h1": "https://example.com/login", "h2": FIDO2})
    monkeypatch.setattr(window_inventory, "_transient_client", lambda port: client)

    inventory = await window_inventory.collect(FakeEngine())

    windows = inventory["windows"]
    assert len(windows) == 2
    tab, popout = windows
    assert tab["targetId"] == "t1" and tab["handle"] == "h1"
    assert tab["sources"] == ["juggler", "marionette"]
    assert tab["dialogs"] == [{"dialogId": "d1", "type": "alert", "message": "hi"}]
    assert popout["kind"] == "bitwarden_fido2" and popout["sources"] == ["marionette"]
    summary = inventory["summary"]
    assert summary["fido2Popout"] == "h2"
    assert summary["webauthnPending"] is True
    assert summary["jsDialogs"] == 1
    assert inventory["marionette"]["status"] == "ok"
    assert client.deleted is True  # a transient WebDriver session never lingers


@pytest.mark.asyncio
async def test_busy_marionette_is_reported_not_waited_on() -> None:
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with window_inventory.marionette_guard("kahin_vault_login"):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(2)
        scan = await asyncio.to_thread(window_inventory.scan_marionette, FakeEngine(), lock_timeout=0.05)
        assert scan["status"] == "busy"
        assert scan["holder"]["owner"] == "kahin_vault_login"
    finally:
        release.set()
        worker.join()


@pytest.mark.asyncio
async def test_without_marionette_webauthn_is_unknown_not_false() -> None:
    engine = FakeEngine()
    engine._marionette_port = None
    evidence = await window_inventory.webauthn_evidence(engine)
    assert evidence["pending"] is None
    assert evidence["marionette"]["status"] == "unavailable"
