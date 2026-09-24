import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from kahin.tools import passkey_mirage


class FakeHealer:
    @asynccontextmanager
    async def safe(self, *_args, **_kwargs):
        yield


class FakeEngine:
    _headless = False

    def __init__(self):
        self._sessions = {"site-target": "site-session"}
        self.switched = []

    async def ensure_page(self):
        return {"targetId": "site-target", "sessionId": "site-session"}

    async def switch_page(self, target):
        self.switched.append(target)


class FakeClient:
    def get_url(self):
        return "moz-extension://example/popup/index.html#/login?email=private"


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
def isolated_setup(monkeypatch):
    engine = FakeEngine()
    session = FakeSession()
    monkeypatch.setattr(passkey_mirage, "_setup_lock", asyncio.Lock())
    monkeypatch.setattr(passkey_mirage, "_setup_session", None)
    monkeypatch.setattr(passkey_mirage, "_setup_engine", None)
    monkeypatch.setattr(passkey_mirage, "_active_engine", lambda: engine)
    monkeypatch.setattr(passkey_mirage, "_healer_ref", FakeHealer())
    monkeypatch.setattr(passkey_mirage.PasskeyUISession, "open", lambda _engine: session)
    monkeypatch.setattr(
        passkey_mirage,
        "prepare_login_ui",
        lambda *_args: {"status": "ready", "route": "login", "state": "login"},
    )
    return engine, session


@pytest.mark.asyncio
async def test_setup_stays_open_for_login_then_closes_after_verified_configuration(
    monkeypatch, isolated_setup
):
    engine, session = isolated_setup
    result = json.loads(await passkey_mirage.passkey_setup_open())
    assert result == {"status": "ready", "route": "login", "state": "login", "setup_tab": "open"}

    status = json.loads(await passkey_mirage.passkey_setup_status())
    assert status == {"status": "open", "route": "/login"}
    assert "private" not in json.dumps(status)

    monkeypatch.setattr(
        passkey_mirage,
        "configure_unlocked_vault",
        lambda *_args: {"status": "login_required"},
    )
    waiting = json.loads(await passkey_mirage.passkey_setup_finish())
    assert waiting["status"] == "login_required"
    assert session.closed is False

    monkeypatch.setattr(
        passkey_mirage,
        "configure_unlocked_vault",
        lambda *_args: {"status": "configured", "settings": {"vault_timeout": "never"}},
    )
    done = json.loads(await passkey_mirage.passkey_setup_finish())
    assert done["status"] == "configured"
    assert session.closed is True
    assert engine.switched == ["site-target"]


@pytest.mark.asyncio
async def test_setup_requires_visible_browser(isolated_setup):
    engine, session = isolated_setup
    engine._headless = True

    response = json.loads(await passkey_mirage.passkey_setup_open())

    assert response["code"] == "visible_browser_required"
    assert session.closed is False
    assert passkey_mirage._setup_session is None
