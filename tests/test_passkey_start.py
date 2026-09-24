import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from kahin import _state as state
from kahin.tools import pilot


class FakeMirage:
    def __init__(self, engine_name="mirage"):
        self.engine_name = engine_name
        self._persistent_profile = True
        self._profile_dir = Path("/tmp/kahin-passkey-profile")
        self._addons = []
        self.started_with = None

    async def start(self, **kwargs):
        self.started_with = kwargs
        self._passkey_mode = kwargs.get("passkey_mode", False)

    async def ensure_page(self):
        return {"sessionId": "fake-session"}

    async def list_pages(self):
        return [{"url": "about:blank"}]

    async def on_event(self, _callback):
        return None

    def on_death(self, _callback):
        return None


class FakeHealer:
    @asynccontextmanager
    async def safe(self, *_args, **_kwargs):
        yield

    def bind_engine(self, _engine):
        return None


@pytest.fixture
def isolated_start(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "_lifecycle_lock", asyncio.Lock())
    monkeypatch.setattr(state, "_current_engine", None)
    monkeypatch.setattr(pilot, "_healer_ref", FakeHealer())
    monkeypatch.setattr(state, "acquire_browser_lock", lambda: None)
    monkeypatch.setattr(state, "release_browser_lock", lambda: None)
    monkeypatch.setattr(
        pilot,
        "_profile_directory",
        lambda persistent, _path=None: (tmp_path / "profile" if persistent else None, persistent),
    )
    monkeypatch.setattr(pilot, "Mirage", FakeMirage)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"passkey_mode": "true"}, "passkey_mode"),
        ({"passkey_mode": True, "engine": "shadow"}, "engine"),
        ({"passkey_mode": True, "persistent_profile": False}, "persistent_profile"),
    ],
)
async def test_passkey_mode_validates_engine_profile_and_type(monkeypatch, kwargs, field):
    async def unexpected_to_thread(*_args, **_kwargs):
        pytest.fail("extension setup must not run")

    monkeypatch.setattr(pilot.asyncio, "to_thread", unexpected_to_thread)
    response = json.loads(await pilot.browser_start(**kwargs))

    assert response["code"] == "invalid_argument"
    assert response["field"] == field


@pytest.mark.asyncio
async def test_passkey_start_installs_into_profile_and_preserves_user_addons(monkeypatch, isolated_start):
    caller_thread = threading.get_ident()
    installer_thread = []
    installer_profiles = []
    order = []
    from kahin import bitwarden

    monkeypatch.setattr(
        bitwarden,
        "install_bitwarden_into_profile",
        lambda profile: (
            order.append("install"),
            installer_thread.append(threading.get_ident()),
            installer_profiles.append(profile),
            "/tmp/bitwarden.xpi",
        )[-1],
        raising=False,
    )
    from kahin import extensions

    monkeypatch.setattr(extensions, "inspect_extension", lambda _path: {"compatible": True})
    original_start = FakeMirage.start

    async def record_start(self, **kwargs):
        order.append("start")
        await original_start(self, **kwargs)

    monkeypatch.setattr(FakeMirage, "start", record_start)

    response = json.loads(
        await pilot.browser_start(
            engine="camoufox",
            addons=["/staged/user-addon"],
            passkey_mode=True,
        )
    )

    engine = state._current_engine
    assert response["status"] == "started"
    assert response["engine"] == "mirage"
    assert response["passkey_mode"] is True
    assert response["passkey_extension"] == {
        "state": "installed_in_profile",
        "vault_state": "unknown",
    }
    assert installer_profiles == [isolated_start / "profile"]
    assert order == ["install", "start"]
    assert engine.started_with["addons"] == ["/staged/user-addon"]
    assert engine.started_with["passkey_mode"] is True
    assert installer_thread and installer_thread[0] != caller_thread
    assert engine._passkey_mode is True


@pytest.mark.asyncio
async def test_passkey_reuse_conflicts_before_install_if_active_engine_lacks_mode(
    monkeypatch, isolated_start
):
    from kahin import bitwarden

    installer_calls = []
    monkeypatch.setattr(
        bitwarden,
        "install_bitwarden_into_profile",
        lambda profile: installer_calls.append(profile),
        raising=False,
    )
    active = FakeMirage()
    active._profile_dir = isolated_start / "profile"
    active._addons = ["/staged/user-addon"]
    monkeypatch.setattr(state, "_current_engine", active)
    monkeypatch.setattr(pilot, "_engine_is_healthy", _healthy)

    response = json.loads(await pilot.browser_start(passkey_mode=True))

    assert response["code"] == "engine_config_conflict"
    assert response["requested"]["passkey_mode"] is True
    assert response["active"]["passkey_mode"] is False
    assert installer_calls == []


@pytest.mark.asyncio
async def test_reuse_reports_only_passkey_mode_recorded_on_successful_start(monkeypatch, isolated_start):
    active = FakeMirage()
    active._profile_dir = isolated_start / "profile"
    monkeypatch.setattr(state, "_current_engine", active)
    monkeypatch.setattr(pilot, "_engine_is_healthy", _healthy)

    ordinary_reuse = json.loads(await pilot.browser_start())
    assert ordinary_reuse["status"] == "reused"
    assert "passkey_mode" not in ordinary_reuse

    active._passkey_mode = True
    passkey_reuse = json.loads(await pilot.browser_start())
    assert passkey_reuse["passkey_mode"] is True
    assert passkey_reuse["passkey_extension"]["vault_state"] == "unknown"


async def _healthy(_engine):
    return True
