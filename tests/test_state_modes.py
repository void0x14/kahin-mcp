"""Durum modları: ağırbaş (kalıcı) ve keş (geçici) — spec §2/§5/§6."""

import json

import pytest

from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.mirage import (
    Mirage,
    _canonical_state_mode,
    _state_mode_directory,
)


@pytest.fixture
def kahin_home(tmp_path, monkeypatch):
    """Isolate KAHIN_HOME and the system temp dir from the real machine."""
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("KAHIN_HOME", str(home))
    monkeypatch.delenv("KAHIN_PROFILE_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(mirage_mod.tempfile, "gettempdir", lambda: str(tmp))
    return home, tmp


# --- Alias normalization --------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("agirbas", "ağırbaş"),
        ("Ağırbaş", "ağırbaş"),
        ("ağırbaş", "ağırbaş"),
        ("  Agirbas  ", "ağırbaş"),
        ("kes", "keş"),
        ("keş", "keş"),
        ("KEŞ", "keş"),
        (" Kes ", "keş"),
    ],
)
def test_alias_normalization(alias, expected):
    assert _canonical_state_mode(alias) == expected


@pytest.mark.parametrize("bad", ["", "   ", "kalici", "persistent", None, 3, ["kes"]])
def test_unknown_mode_is_rejected(bad):
    with pytest.raises(ValueError):
        _canonical_state_mode(bad)


# --- agirbas: one stable home --------------------------------------------


def test_agirbas_path_is_stable(kahin_home):
    home, _ = kahin_home

    first, mode_a = _state_mode_directory("agirbas", None)
    second, mode_b = _state_mode_directory("ağırbaş", None)

    assert mode_a == mode_b == "ağırbaş"
    assert first == second == home / "agirbas" / "profile"


def test_agirbas_explicit_profile_is_used(tmp_path):
    configured = tmp_path / "explicit"
    path, mode = _state_mode_directory("agirbas", str(configured))
    assert mode == "ağırbaş"
    assert path == configured


# --- kes: a fresh, unique directory per call -----------------------------


def test_kes_path_is_unique_per_call(kahin_home):
    home, _ = kahin_home

    first, mode_a = _state_mode_directory("kes", None)
    second, mode_b = _state_mode_directory("keş", None)
    assert first is not None and second is not None
    try:
        assert mode_a == mode_b == "keş"
        assert first != second
        assert first.is_dir() and second.is_dir()
        assert first.name.startswith("kahin-kes-")
        assert second.name.startswith("kahin-kes-")
        assert first.parent == home / "kes"
        assert second.parent == home / "kes"
    finally:
        mirage_mod.shutil.rmtree(first, ignore_errors=True)
        mirage_mod.shutil.rmtree(second, ignore_errors=True)


# --- One-time legacy migration -------------------------------------------


def test_legacy_profile_is_migrated_once(kahin_home):
    home, _ = kahin_home
    legacy = home / "profile"
    legacy.mkdir(parents=True)
    (legacy / "cookies.sqlite").write_text("uBlock+Bitwarden state")

    path, mode = _state_mode_directory("agirbas", None)

    assert mode == "ağırbaş"
    assert path is not None
    assert path == home / "agirbas" / "profile"
    assert not legacy.exists()
    assert (path / "cookies.sqlite").read_text() == "uBlock+Bitwarden state"

    # Idempotent: a second resolution keeps the migrated home.
    again, _ = _state_mode_directory("agirbas", None)
    assert again == path


def test_migration_is_skipped_when_agirbas_home_exists(kahin_home):
    home, _ = kahin_home
    legacy = home / "profile"
    legacy.mkdir(parents=True)
    (legacy / "old.txt").write_text("legacy")
    target = home / "agirbas" / "profile"
    target.mkdir(parents=True)
    (target / "new.txt").write_text("agirbas")

    path, _ = _state_mode_directory("agirbas", None)

    assert path == target
    assert (target / "new.txt").exists()
    assert (legacy / "old.txt").exists()  # legacy is left untouched


# --- stop() profile cleanup ----------------------------------------------


def test_kes_stop_deletes_exactly_its_dir(kahin_home):
    profile, _ = _state_mode_directory("kes", None)
    assert profile is not None
    engine = Mirage()
    engine._state_mode = "keş"
    engine._persistent_profile = False
    engine._profile_dir = profile
    engine._kes_profile_dir = profile
    mirage_mod._LIVE_KES_DIRS.add(str(profile))

    engine._remove_profile()

    assert not profile.exists()
    assert str(profile) not in mirage_mod._LIVE_KES_DIRS


def test_kes_stop_refuses_a_path_it_does_not_own(tmp_path):
    important = tmp_path / "important"
    important.mkdir()
    (important / "keep.txt").write_text("do not delete")
    engine = Mirage()
    engine._state_mode = "keş"
    engine._persistent_profile = False
    engine._profile_dir = important
    engine._kes_profile_dir = important

    engine._remove_profile()

    assert important.exists()
    assert (important / "keep.txt").read_text() == "do not delete"


def test_agirbas_stop_deletes_nothing(kahin_home):
    home, _ = kahin_home
    profile, _ = _state_mode_directory("agirbas", None)
    assert profile is not None
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "cookies.sqlite").write_text("persistent")
    engine = Mirage()
    engine._state_mode = "ağırbaş"
    engine._persistent_profile = True
    engine._profile_dir = profile
    engine._kes_profile_dir = None

    engine._remove_profile()

    assert profile.exists()
    assert (profile / "cookies.sqlite").read_text() == "persistent"


# --- Stale kes sweep ------------------------------------------------------


def test_stale_kes_sweep_removes_orphans_but_keeps_live(kahin_home):
    home, tmp = kahin_home
    kes_root = home / "kes"
    orphan = kes_root / "kahin-kes-orphan"
    orphan.mkdir(parents=True)
    (orphan / "x").write_text("stale")
    legacy_orphan = tmp / "kahin-fp-dead"
    legacy_orphan.mkdir(parents=True)
    live = kes_root / "kahin-kes-live"
    live.mkdir(parents=True)
    mirage_mod._LIVE_KES_DIRS.add(str(live))
    try:
        removed = mirage_mod._sweep_stale_kes_dirs()

        assert removed >= 2
        assert not orphan.exists()
        assert not legacy_orphan.exists()
        assert live.exists()
    finally:
        mirage_mod._LIVE_KES_DIRS.discard(str(live))


# --- §5 agent enforcement (browser_start) --------------------------------


@pytest.mark.asyncio
async def test_browser_start_without_mode_does_not_start(kahin_home):
    from kahin.tools import pilot

    payload = json.loads(await pilot.browser_start())

    assert payload["code"] == "mode_required"
    assert set(payload["modes"]) == {"ağırbaş", "keş"}
    assert "ağırbaş" in payload["selection_rule"]


@pytest.mark.asyncio
async def test_kes_requires_ephemeral_ack(kahin_home):
    from kahin.tools import pilot

    payload = json.loads(await pilot.browser_start(mode="kes"))

    assert payload["code"] == "ephemeral_ack_required"


@pytest.mark.asyncio
async def test_kes_rejects_explicit_profile_dir(kahin_home):
    from kahin.tools import pilot

    payload = json.loads(
        await pilot.browser_start(mode="keş", ephemeral_ack=True, profile_dir="/tmp/explicit")
    )

    assert payload["code"] == "invalid_argument"
    assert payload["field"] == "profile_dir"


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected_by_tool(kahin_home):
    from kahin.tools import pilot

    payload = json.loads(await pilot.browser_start(mode="kalici"))

    assert payload["code"] == "invalid_argument"
    assert payload["field"] == "mode"


@pytest.mark.asyncio
async def test_passkey_mode_with_kes_mode_is_a_mode_conflict(kahin_home):
    """passkey_mode + explicit mode=keş must name both arguments, not
    persistent_profile."""
    from kahin.tools import pilot

    payload = json.loads(
        await pilot.browser_start(mode="keş", ephemeral_ack=True, passkey_mode=True)
    )

    assert payload["code"] == "mode_argument_conflict"
    assert payload["field"] == "mode"
    assert payload["conflicting_arguments"] == ["mode", "passkey_mode"]
    assert "mode" in payload["error"]
    assert "passkey_mode" in payload["error"]


@pytest.mark.asyncio
async def test_passkey_mode_without_persistent_profile_keeps_legacy_error(kahin_home):
    """persistent_profile=False still reports the original field/behavior."""
    from kahin.tools import pilot

    payload = json.loads(await pilot.browser_start(passkey_mode=True, persistent_profile=False))

    assert payload["code"] == "invalid_argument"
    assert payload["field"] == "persistent_profile"
