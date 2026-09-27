from pathlib import Path

import pytest

from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.mirage import _profile_directory


def test_default_profile_is_stable_under_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("KAHIN_HOME", raising=False)
    monkeypatch.delenv("KAHIN_PROFILE_DIR", raising=False)

    path, persistent = _profile_directory(True, None)

    assert persistent is True
    assert path == tmp_path / "kahin" / "agirbas" / "profile"


def test_ephemeral_profile_is_a_fresh_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    monkeypatch.delenv("KAHIN_PROFILE_DIR", raising=False)

    path, persistent = _profile_directory(False, None)

    assert persistent is False
    assert path is not None
    assert path.name.startswith("kahin-kes-")
    assert path.parent == tmp_path / "kes"
    mirage_mod.shutil.rmtree(path, ignore_errors=True)


def test_configured_profile_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        _profile_directory(True, "relative-profile")


def test_configured_profile_is_expanded(tmp_path):
    path, persistent = _profile_directory(True, str(tmp_path / "kahin-profile"))

    assert persistent is True
    assert path == tmp_path / "kahin-profile"


def test_default_profile_override_is_used(tmp_path, monkeypatch):
    configured = tmp_path / "override"
    monkeypatch.setenv("KAHIN_PROFILE_DIR", str(configured))
    monkeypatch.delenv("KAHIN_HOME", raising=False)

    path, persistent = _profile_directory(True, None)

    assert persistent is True
    assert path == configured


def test_persistent_flag_maps_to_the_two_modes(tmp_path, monkeypatch):
    """Back-compat: persistent True -> agirbas, False -> kes (spec §2.3)."""
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    monkeypatch.delenv("KAHIN_PROFILE_DIR", raising=False)

    agirbas_path, agirbas_persistent = _profile_directory(True, None)
    kes_path, kes_persistent = _profile_directory(False, None)
    try:
        assert agirbas_path == tmp_path / "agirbas" / "profile"
        assert agirbas_persistent is True
        assert kes_persistent is False
        assert kes_path is not None and kes_path != agirbas_path
    finally:
        if kes_path is not None:
            mirage_mod.shutil.rmtree(kes_path, ignore_errors=True)
