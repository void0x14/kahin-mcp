import json

import pytest

from kahin import passkey_session
from kahin.bitwarden import BITWARDEN_GECKO_ID


EXTENSION_UUID = "d8c0aeb6-4f37-48f9-8f15-2e9f2d8df0ac"


class FakeMarionette:
    instances = []
    failures_remaining = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.start_calls = 0
        self.deleted = 0
        self.open_calls = []
        self.navigated = []
        self.handles = ["site-tab"]
        self.current = "site-tab"
        self.closed = []
        self.session_id = None
        type(self).instances.append(self)

    def start_session(self):
        self.start_calls += 1
        if type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise OSError("not listening yet")
        self.session_id = "fake-session"

    @property
    def current_window_handle(self):
        return self.current

    @property
    def window_handles(self):
        return list(self.handles)

    def open(self, *, type, focus):
        self.open_calls.append((type, focus))
        self.handles.append("setup-tab")
        self.current = "setup-tab"
        return {"handle": "setup-tab", "type": type}

    def switch_to_window(self, handle):
        assert handle in self.handles
        self.current = handle

    def navigate(self, url):
        self.navigated.append(url)

    def close(self):
        self.closed.append(self.current)
        self.handles.remove(self.current)
        self.current = self.handles[0] if self.handles else ""

    def delete_session(self):
        self.deleted += 1
        self.session_id = None


@pytest.fixture
def setup(monkeypatch, tmp_path):
    FakeMarionette.instances = []
    FakeMarionette.failures_remaining = 0
    monkeypatch.setattr(passkey_session, "Marionette", FakeMarionette)
    profile = tmp_path / "profile"
    profile.mkdir()
    uuid_mapping = json.dumps({BITWARDEN_GECKO_ID: EXTENSION_UUID})
    prefs_value = json.dumps(uuid_mapping)
    (profile / "prefs.js").write_text(
        f'user_pref("extensions.webextensions.uuids", {prefs_value});\n',
        encoding="utf-8",
    )

    class Engine:
        _marionette_port = 29301
        _profile_dir = profile
        _current_target = "juggler-original-target"

    return Engine(), profile


def test_connect_uses_existing_endpoint_and_dedicated_popup_tab(setup):
    engine, _profile = setup

    session = passkey_session.PasskeyUISession.open(engine)

    client = FakeMarionette.instances[-1]
    assert client.kwargs == {
        "host": "127.0.0.1",
        "port": 29301,
        "app": "firefox",
        "socket_timeout": 3,
    }
    assert client.open_calls == [("tab", True)]
    assert client.navigated == [f"moz-extension://{EXTENSION_UUID}/popup/index.html"]
    assert session.popup_url == client.navigated[0]
    assert client.handles == ["site-tab", "setup-tab"]
    assert session.original_target == "juggler-original-target"
    assert session.is_alive() is True
    assert engine._current_target == "juggler-original-target"


def test_close_removes_only_setup_tab_restores_site_handle_and_deletes_session(setup):
    session = passkey_session.PasskeyUISession.open(setup[0])
    client = session.client

    session.close()
    session.close()

    assert client.closed == ["setup-tab"]
    assert client.handles == ["site-tab"]
    assert client.current == "site-tab"
    assert client.deleted == 1
    assert session.is_alive() is False


def test_close_keeps_last_tab_open_to_preserve_browser(setup):
    session = passkey_session.PasskeyUISession.open(setup[0])
    client = session.client
    client.handles.remove("site-tab")

    session.close()

    assert client.closed == []
    assert client.handles == ["setup-tab"]
    assert client.deleted == 1


def test_connect_retries_marionette_without_launching_a_browser(monkeypatch, setup):
    FakeMarionette.failures_remaining = 2
    monkeypatch.setattr(passkey_session, "RETRY_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(passkey_session.time, "sleep", lambda _seconds: None)

    session = passkey_session.PasskeyUISession.open(setup[0])

    assert len(FakeMarionette.instances) == 3
    assert [instance.deleted for instance in FakeMarionette.instances] == [1, 1, 0]
    assert session.client.navigated


def test_connect_timeout_does_not_leave_tab_or_session(monkeypatch, setup):
    FakeMarionette.failures_remaining = 5
    monkeypatch.setattr(passkey_session, "CONNECT_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(passkey_session.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="Marionette endpoint"):
        passkey_session.PasskeyUISession.open(setup[0])

    client = FakeMarionette.instances[0]
    assert client.open_calls == []
    assert client.deleted == 1


def test_missing_extension_uuid_cleans_setup_tab_and_connection(monkeypatch, setup):
    engine, profile = setup
    (profile / "prefs.js").write_text("user_pref(\"unrelated\", true);\n", encoding="utf-8")
    monkeypatch.setattr(passkey_session, "PREFS_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(passkey_session, "RETRY_INTERVAL_SECONDS", 0)

    with pytest.raises(TimeoutError, match="UUID lookup"):
        passkey_session.PasskeyUISession.open(engine)

    client = FakeMarionette.instances[-1]
    assert client.closed == ["setup-tab"]
    assert client.handles == ["site-tab"]
    assert client.deleted == 1


@pytest.mark.parametrize("port", [None, 0, -1, 65536, True, "29301"])
def test_rejects_invalid_engine_marionette_port(setup, port):
    engine, _profile = setup
    engine._marionette_port = port

    with pytest.raises(ValueError, match="Marionette port"):
        passkey_session.PasskeyUISession.open(engine)
