"""Engine resolution: a Kahin-managed patched build wins over Camoufox's cache."""

from __future__ import annotations

import json
from pathlib import Path

from kahin.the_twins import mirage


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _isolate(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("KAHIN_HOME", str(home))
    monkeypatch.delenv("KAHIN_CAMOUFOX_BIN", raising=False)
    monkeypatch.setattr(mirage, "_camoufox_cli_path", lambda: None)


def test_installed_engine_wins_over_cache(monkeypatch, tmp_path):
    home = tmp_path / "kahin-home"
    engine = _executable(home / "engines" / "152.0.4-beta.31-deadbeef" / "camoufox-bin")
    (engine.parent / "version.json").write_text(
        json.dumps({"version": "152.0.4", "build": "beta.31"})
    )
    (home / "engines" / "current").symlink_to(engine.parent)
    cache_bin = _executable(tmp_path / "cache" / "camoufox" / "camoufox-bin")
    monkeypatch.setattr(mirage, "_camoufox_cache_candidates", lambda: [cache_bin])
    _isolate(monkeypatch, home)

    resolved = mirage._camoufox_bin()

    assert resolved == home / "engines" / "current" / "camoufox-bin"
    assert resolved != cache_bin
    assert mirage._camoufox_ff_version(resolved) == "152.0.4"


def test_versioned_engine_resolves_without_current_symlink(monkeypatch, tmp_path):
    home = tmp_path / "kahin-home"
    engine = _executable(home / "engines" / "152.0.4-beta.31-deadbeef" / "camoufox-bin")
    _isolate(monkeypatch, home)

    assert mirage._camoufox_bin() == engine


def test_explicit_binary_wins_over_installed_engine(monkeypatch, tmp_path):
    home = tmp_path / "kahin-home"
    _executable(home / "engines" / "152.0.4-beta.31-deadbeef" / "camoufox-bin")
    explicit = _executable(tmp_path / "explicit" / "camoufox-bin")
    _isolate(monkeypatch, home)
    monkeypatch.setenv("KAHIN_CAMOUFOX_BIN", str(explicit))

    assert mirage._camoufox_bin() == explicit
