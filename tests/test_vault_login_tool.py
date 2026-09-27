"""Unit tests for the ``kahin_vault_login`` MCP tool (state-modes §2.4/§5).

No browser is involved: a fake engine and Marionette session stand in for the
running Mirage browser, and the resolver/actions are faked so the tool's
code-side routing can be asserted in isolation. The tool must never let the
agent choose the method and must never return vault contents.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from kahin.tools import vault_login_mirage


class FakeHealer:
    @asynccontextmanager
    async def safe(self, *_args, **_kwargs):
        yield


class FakeEngine:
    def __init__(self):
        self._sessions = {"site-target": "site-session"}
        self.switched = []
        self.pages = 0

    async def ensure_page(self):
        self.pages += 1
        return {"targetId": "site-target", "sessionId": "site-session"}

    async def switch_page(self, target):
        self.switched.append(target)


class FakeState:
    def __init__(self, engine):
        self._current_engine = engine


class FakeClient:
    """Carries a sentinel secret the tool must never echo."""

    secret = "SENTINEL-SECRET-PW"


class FakeSession:
    original_target = "site-target"
    popup_url = "moz-extension://example/popup/index.html"

    def __init__(self):
        self.client = FakeClient()
        self.closed = False

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True


@pytest.fixture
def isolated(monkeypatch):
    engine = FakeEngine()
    session = FakeSession()
    opened = []

    monkeypatch.setattr(vault_login_mirage, "_login_lock", asyncio.Lock())
    monkeypatch.setattr(vault_login_mirage, "_login_session", None)
    monkeypatch.setattr(vault_login_mirage, "_login_engine", None)
    monkeypatch.setattr(vault_login_mirage, "_healer_ref", FakeHealer())
    monkeypatch.setattr(vault_login_mirage, "_engine_error", lambda: None)
    monkeypatch.setattr(vault_login_mirage, "_passkey_engine", lambda: engine)

    def fake_open(_engine):
        opened.append(_engine)
        return session

    monkeypatch.setattr(vault_login_mirage.PasskeyUISession, "open", fake_open)
    return engine, session, opened


# --- engine gating ----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_engine_returns_structured_engine_unavailable(monkeypatch):
    opened = []
    monkeypatch.setattr(vault_login_mirage, "_login_lock", asyncio.Lock())
    monkeypatch.setattr(vault_login_mirage, "_healer_ref", FakeHealer())
    monkeypatch.setattr(vault_login_mirage, "state", FakeState(None))
    monkeypatch.setattr(
        vault_login_mirage.PasskeyUISession,
        "open",
        lambda engine: opened.append(engine) or FakeSession(),
    )

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response["code"] == "engine_unavailable"
    assert "passkey_mode" in response["error"]
    assert opened == []


@pytest.mark.asyncio
async def test_non_passkey_engine_returns_passkey_engine_unavailable(monkeypatch):
    opened = []
    monkeypatch.setattr(vault_login_mirage, "_login_lock", asyncio.Lock())
    monkeypatch.setattr(vault_login_mirage, "_healer_ref", FakeHealer())
    monkeypatch.setattr(vault_login_mirage, "state", FakeState(object()))
    monkeypatch.setattr(
        vault_login_mirage.PasskeyUISession,
        "open",
        lambda engine: opened.append(engine) or FakeSession(),
    )

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response["code"] == "passkey_engine_unavailable"
    assert opened == []


# --- code-side routing ------------------------------------------------------


@pytest.mark.asyncio
async def test_passkey_method_selects_passkey_and_reports_status(monkeypatch, isolated):
    engine, session, opened = isolated
    calls = []

    def select(client):
        calls.append(("select", client))
        return {"status": "selected"}

    def fill(client, url):
        calls.append(("fill", client, url))
        return {"status": "triggered"}

    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: {"method": "passkey", "count": 1, "auto": False},
    )
    monkeypatch.setattr(vault_login_mirage, "select_passkey", select)
    monkeypatch.setattr(vault_login_mirage, "fill_credentials", fill)

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response == {"method": "passkey", "action": "select_passkey", "status": "selected"}
    assert calls == [("select", session.client)]
    assert opened == [engine]


@pytest.mark.asyncio
async def test_credentials_method_fills_and_reports_status(monkeypatch, isolated):
    engine, session, opened = isolated
    calls = []

    def select(client):
        calls.append(("select", client))
        return {"status": "selected"}

    def fill(client, url):
        calls.append(("fill", client, url))
        return {"status": "triggered"}

    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: {"method": "credentials", "count": 2},
    )
    monkeypatch.setattr(vault_login_mirage, "select_passkey", select)
    monkeypatch.setattr(vault_login_mirage, "fill_credentials", fill)

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response == {"method": "credentials", "action": "fill_credentials", "status": "triggered"}
    assert calls == [("fill", session.client, "https://example.com/login")]


@pytest.mark.asyncio
async def test_user_required_hands_off_without_running_any_action(monkeypatch, isolated):
    engine, session, opened = isolated
    calls = []

    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: {"method": "user_required", "count": 0},
    )
    monkeypatch.setattr(
        vault_login_mirage,
        "select_passkey",
        lambda client: calls.append("select") or {"status": "selected"},
    )
    monkeypatch.setattr(
        vault_login_mirage,
        "fill_credentials",
        lambda client, url: calls.append("fill") or {"status": "triggered"},
    )

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response["method"] == "user_required"
    assert response["action"] == "none"
    assert "Bitwarden" in response["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_unavailable_surfaces_reason_and_does_not_claim_missing(monkeypatch, isolated):
    engine, session, opened = isolated
    calls = []

    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: {"method": "unavailable", "count": 0, "reason": "no_site_tab"},
    )
    monkeypatch.setattr(
        vault_login_mirage,
        "select_passkey",
        lambda client: calls.append("select") or {"status": "selected"},
    )
    monkeypatch.setattr(
        vault_login_mirage,
        "fill_credentials",
        lambda client, url: calls.append("fill") or {"status": "triggered"},
    )

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response["method"] == "unavailable"
    assert response["reason"] == "no_site_tab"
    assert response["method"] != "user_required"
    assert "missing" not in response["message"].lower()
    assert calls == []


# --- validation -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["", "   ", "not-a-url", "ftp://example.com", "https://" + "a" * 3000],
)
async def test_invalid_site_url_is_rejected(monkeypatch, isolated, bad):
    engine, session, opened = isolated
    calls = []
    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: calls.append("resolve") or {"method": "user_required"},
    )

    response = json.loads(await vault_login_mirage.vault_login(bad))

    assert response["code"] == "invalid_site_url"
    assert calls == []
    assert opened == []


# --- secrecy ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_never_echoes_resolver_or_action_secrets(monkeypatch, isolated):
    engine, session, opened = isolated
    # The resolver/action payloads carry a sentinel; the tool must project
    # only the coarse method/action/status, never the payload itself.
    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: {"method": "credentials", "count": 1, "password": "SENTINEL-SECRET-PW"},
    )
    monkeypatch.setattr(
        vault_login_mirage,
        "fill_credentials",
        lambda client, url: {"status": "triggered", "secret": "SENTINEL-SECRET-PW"},
    )
    monkeypatch.setattr(vault_login_mirage, "select_passkey", lambda client: {"status": "selected"})

    response = await vault_login_mirage.vault_login("https://example.com/login")

    assert "SENTINEL-SECRET-PW" not in response
    assert "password" not in response.lower()
    assert "secret" not in response.lower()


# --- session lifecycle ------------------------------------------------------


@pytest.mark.asyncio
async def test_session_is_reused_across_calls(monkeypatch, isolated):
    engine, session, opened = isolated
    monkeypatch.setattr(
        vault_login_mirage,
        "resolve_login",
        lambda client, url: {"method": "user_required", "count": 0},
    )

    await vault_login_mirage.vault_login("https://example.com/login")
    await vault_login_mirage.vault_login("https://example.com/login")

    assert opened == [engine]


@pytest.mark.asyncio
async def test_failure_closes_session_and_restores_tab(monkeypatch, isolated):
    engine, session, opened = isolated

    def boom(client, url):
        raise RuntimeError("marionette blew up")

    monkeypatch.setattr(vault_login_mirage, "resolve_login", boom)

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response["code"] == "vault_login_failed"
    assert session.closed is True
    assert engine.switched == ["site-target"]
    assert vault_login_mirage._login_session is None


@pytest.mark.asyncio
async def test_open_failure_is_structured(monkeypatch, isolated):
    engine, session, opened = isolated

    def boom(_engine):
        raise RuntimeError("no marionette port")

    monkeypatch.setattr(vault_login_mirage.PasskeyUISession, "open", boom)

    response = json.loads(await vault_login_mirage.vault_login("https://example.com/login"))

    assert response["code"] == "vault_login_unavailable"
    assert vault_login_mirage._login_session is None
