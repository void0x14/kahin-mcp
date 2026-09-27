"""Unit tests for the root-level Bitwarden vault settings write (state-modes §4).

No browser is involved: a fake Marionette client mimics browser.storage.local
semantics and returns canned payloads from execute_async_script.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from kahin.passkey_ui import (
    ACTIVE_ACCOUNT_KEY,
    configure_vault_timeout_root,
    unwrap_stored_value,
    vault_timeout_action_key,
    vault_timeout_key,
    vault_timeout_status,
    wrap_stored_value,
)
from kahin.tools import passkey_mirage


class FakeStorageClient:
    """Fake Marionette client mirroring browser.storage.local.

    ``execute_async_script`` inspects the injected script to tell a read from a
    write, exactly like the real extension page would.
    """

    def __init__(self, store=None, *, ignore_writes=False, raise_on_read=False):
        self.store = dict(store or {})
        self.ignore_writes = ignore_writes
        self.raise_on_read = raise_on_read
        self.scripts: list[str] = []

    def execute_async_script(self, script, script_args=(), **_kwargs):
        self.scripts.append(script)
        if self.raise_on_read and "api.get" in script:
            raise RuntimeError("extension page not reachable")
        if "api.set" in script:
            if not self.ignore_writes:
                self.store.update(script_args[0])
            return {"ok": True}
        keys = script_args[0]
        return {"ok": True, "result": {key: self.store[key] for key in keys if key in self.store}}


def _store_with_user(uid="user-123", settings=None):
    store = {ACTIVE_ACCOUNT_KEY: wrap_stored_value(uid)}
    store.update(settings or {})
    return store


def test_wrap_and_unwrap_round_trip():
    for value in ("never", "lock", True, 0, ["a"], {"b": 1}):
        assert unwrap_stored_value(wrap_stored_value(value)) == value
    assert wrap_stored_value("never") == {"__json__": True, "value": '"never"'}


def test_unwrap_passes_through_unwrapped_values():
    assert unwrap_stored_value("never") == "never"
    assert unwrap_stored_value(None) is None
    assert unwrap_stored_value({"__json__": True, "value": "not json"}) == {
        "__json__": True,
        "value": "not json",
    }


def test_logical_key_builder_matches_bitwarden_layout():
    assert vault_timeout_key("abc") == "user_abc_vaultTimeoutSettings_vaultTimeout"
    assert vault_timeout_action_key("abc") == "user_abc_vaultTimeoutSettings_vaultTimeoutAction"


def test_root_write_without_active_account_requires_login():
    client = FakeStorageClient()

    assert configure_vault_timeout_root(client) == {"status": "login_required"}
    assert client.store == {}


def test_root_write_reports_timeout_mismatch():
    store = _store_with_user(
        settings={vault_timeout_key("user-123"): wrap_stored_value("fiveMinutes")}
    )
    client = FakeStorageClient(store, ignore_writes=True)

    assert configure_vault_timeout_root(client) == {
        "status": "failed",
        "state": "vault_timeout_mismatch",
    }


def test_root_write_reports_action_mismatch_when_only_action_differs():
    store = _store_with_user(
        settings={
            vault_timeout_key("user-123"): wrap_stored_value("never"),
            vault_timeout_action_key("user-123"): wrap_stored_value("logout"),
        }
    )
    client = FakeStorageClient(store, ignore_writes=True)

    assert configure_vault_timeout_root(client) == {
        "status": "failed",
        "state": "vault_timeout_action_mismatch",
    }


def test_root_write_configures_and_verifies_both_settings():
    client = FakeStorageClient(_store_with_user())

    result = configure_vault_timeout_root(client)

    assert result == {
        "status": "configured",
        "settings": {"vault_timeout": "never", "vault_timeout_action": "lock"},
    }
    assert client.store[vault_timeout_key("user-123")] == wrap_stored_value("never")
    assert client.store[vault_timeout_action_key("user-123")] == wrap_stored_value("lock")


def test_root_write_is_unavailable_when_extension_page_is_unreachable():
    client = FakeStorageClient(_store_with_user(), raise_on_read=True)

    assert configure_vault_timeout_root(client) == {
        "status": "unavailable",
        "state": "storage_unavailable",
    }


def test_vault_timeout_status_reads_back_configured_values():
    store = _store_with_user(
        settings={
            vault_timeout_key("user-123"): wrap_stored_value("never"),
            vault_timeout_action_key("user-123"): wrap_stored_value("lock"),
        }
    )
    client = FakeStorageClient(store)

    assert vault_timeout_status(client) == {
        "status": "ok",
        "settings": {"vault_timeout": "never", "vault_timeout_action": "lock"},
        "never": True,
    }


def test_vault_timeout_status_requires_login_without_active_account():
    assert vault_timeout_status(FakeStorageClient()) == {"status": "login_required"}


# --- passkey_setup_finish integration (root first, UI fallback) -------------


class _FakeHealer:
    @asynccontextmanager
    async def safe(self, *_args, **_kwargs):
        yield


def _wire_finish(monkeypatch):
    engine = SimpleNamespace()
    session = SimpleNamespace(
        client=object(),
        popup_url="moz-extension://example/popup/index.html",
        original_target=None,
        close=lambda: None,
    )
    monkeypatch.setattr(passkey_mirage, "_setup_lock", asyncio.Lock())
    monkeypatch.setattr(passkey_mirage, "_setup_session", session)
    monkeypatch.setattr(passkey_mirage, "_setup_engine", engine)
    monkeypatch.setattr(passkey_mirage, "_active_engine", lambda: engine)
    monkeypatch.setattr(passkey_mirage, "_healer_ref", _FakeHealer())
    return session


@pytest.mark.asyncio
async def test_setup_finish_prefers_root_storage_and_skips_ui(monkeypatch):
    _wire_finish(monkeypatch)
    monkeypatch.setattr(
        passkey_mirage,
        "configure_vault_timeout_root",
        lambda _client: {
            "status": "configured",
            "settings": {"vault_timeout": "never", "vault_timeout_action": "lock"},
        },
    )
    ui_calls: list[bool] = []
    monkeypatch.setattr(
        passkey_mirage,
        "configure_unlocked_vault",
        lambda *_args: ui_calls.append(True) or {"status": "configured"},
    )

    result = json.loads(await passkey_mirage.passkey_setup_finish())

    assert result["method"] == "storage_root"
    assert result["status"] == "configured"
    assert ui_calls == []


@pytest.mark.asyncio
async def test_setup_finish_falls_back_to_ui_when_root_unavailable(monkeypatch):
    _wire_finish(monkeypatch)
    monkeypatch.setattr(
        passkey_mirage,
        "configure_vault_timeout_root",
        lambda _client: {"status": "unavailable", "state": "storage_unavailable"},
    )
    monkeypatch.setattr(
        passkey_mirage,
        "configure_unlocked_vault",
        lambda *_args: {"status": "configured", "settings": {"vault_timeout": "never"}},
    )

    result = json.loads(await passkey_mirage.passkey_setup_finish())

    assert result["method"] == "ui"
    assert result["status"] == "configured"


class _NoResultClient:
    """A client whose read never yields an object: the extension API was not
    actually reached (e.g. Xray hid ``browser``)."""

    def execute_async_script(self, script, script_args=(), **_kwargs):
        if "api.set" in script:
            return {"ok": True}
        return {"ok": True, "result": None}


def test_read_without_object_is_unavailable_not_login_required():
    # A silent non-object result must never be reported as "no account": that
    # would be a false negative the owner explicitly rejects.
    client = _NoResultClient()

    assert configure_vault_timeout_root(client) == {
        "status": "unavailable",
        "state": "storage_unavailable",
    }
    assert vault_timeout_status(client) == {
        "status": "unavailable",
        "state": "storage_unavailable",
    }
