import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from kahin import bitwarden


def valid_manifest():
    return {
        "manifest_version": 2,
        "name": "Bitwarden Password Manager",
        "version": "2026.9.0",
        "browser_specific_settings": {"gecko": {"id": "{446900e4-71c2-419f-a6a7-df9c091e268b}"}},
    }


def make_xpi(*, payload_size=128, manifest_overrides=None, extra_entries=()):
    manifest = valid_manifest()
    manifest.update(manifest_overrides or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("background.js", b"b" * payload_size)
        for name, contents in extra_entries:
            bundle.writestr(name, contents)
    return output.getvalue()


def write_valid_addon(directory: Path, manifest=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(manifest or valid_manifest()), encoding="utf-8"
    )
    (directory / "background.js").write_text("// pinned add-on", encoding="utf-8")
    return directory


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def configure_fixture(monkeypatch, tmp_path, content):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path / "kahin"))
    monkeypatch.setattr(bitwarden, "BITWARDEN_SHA256", hashlib.sha256(content).hexdigest())
    requests = []

    def open_url(url, timeout):
        requests.append((url, timeout))
        return FakeResponse(content)

    monkeypatch.setattr(bitwarden.urllib.request, "urlopen", open_url)
    return requests


def test_installs_verified_signed_xpi_and_reuses_offline(monkeypatch, tmp_path):
    content = make_xpi()
    requests = configure_fixture(monkeypatch, tmp_path, content)
    profile = tmp_path / "firefox-profile"
    profile.mkdir()
    extensions = profile / "extensions"
    extensions.mkdir()
    other_addon = extensions / "other@example.invalid.xpi"
    other_addon.write_bytes(b"leave this add-on untouched")

    first = bitwarden.install_bitwarden_into_profile(profile)

    target = extensions / "{446900e4-71c2-419f-a6a7-df9c091e268b}.xpi"
    cached = tmp_path / "cache/kahin/bitwarden/bitwarden-2026.9.0.xpi"
    assert first == str(target)
    assert target.read_bytes() == content
    assert cached.read_bytes() == content
    assert other_addon.read_bytes() == b"leave this add-on untouched"
    original_stat = target.stat()

    def offline(*_args, **_kwargs):
        raise AssertionError("valid cached signed XPI should be reused offline")

    monkeypatch.setattr(bitwarden.urllib.request, "urlopen", offline)
    second = bitwarden.install_bitwarden_into_profile(profile)

    assert second == first
    assert target.stat().st_ino == original_stat.st_ino
    assert len(requests) == 1
    assert other_addon.read_bytes() == b"leave this add-on untouched"


def test_rejects_wrong_digest_without_caching_or_installing(monkeypatch, tmp_path):
    content = make_xpi()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path / "kahin"))
    monkeypatch.setattr(bitwarden, "BITWARDEN_SHA256", "0" * 64)
    monkeypatch.setattr(
        bitwarden.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(content),
    )

    with pytest.raises(ValueError, match="SHA256"):
        bitwarden.install_bitwarden_into_profile(tmp_path / "profile")

    assert not list((tmp_path / "cache").rglob("*.xpi"))
    assert not list((tmp_path / "cache").rglob(".bitwarden-download-*"))
    assert not (tmp_path / "profile/extensions").exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": "2026.8.0"},
        {"manifest_version": 3},
        {"browser_specific_settings": {"gecko": {"id": "wrong@example.invalid"}}},
    ],
)
def test_rejects_unexpected_manifest(monkeypatch, tmp_path, overrides):
    content = make_xpi(manifest_overrides=overrides)
    configure_fixture(monkeypatch, tmp_path, content)

    with pytest.raises(ValueError, match="manifest identity"):
        bitwarden.install_bitwarden_into_profile(tmp_path / "profile")

    assert not list((tmp_path / "cache").rglob("*.xpi"))
    assert not (tmp_path / "profile/extensions").exists()


def test_profile_install_refuses_to_overwrite_another_xpi(monkeypatch, tmp_path):
    content = make_xpi()
    configure_fixture(monkeypatch, tmp_path, content)
    profile = tmp_path / "profile"
    target = profile / "extensions/{446900e4-71c2-419f-a6a7-df9c091e268b}.xpi"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different existing file")

    with pytest.raises(ValueError, match="SHA256"):
        bitwarden.install_bitwarden_into_profile(profile)

    assert target.read_bytes() == b"different existing file"


def test_rejects_archive_traversal(monkeypatch, tmp_path):
    traversal = make_xpi(extra_entries=(("../escape.js", b"bad"),))
    configure_fixture(monkeypatch, tmp_path, traversal)

    with pytest.raises(ValueError, match="path traversal"):
        bitwarden.install_bitwarden_into_profile(tmp_path / "profile")

    assert not (tmp_path / "profile/escape.js").exists()


def test_rejects_archive_symlink(monkeypatch, tmp_path):
    output = io.BytesIO()
    manifest = {
        "manifest_version": 2,
        "name": "Bitwarden",
        "version": "2026.9.0",
        "applications": {"gecko": {"id": "{446900e4-71c2-419f-a6a7-df9c091e268b}"}},
    }
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        link = zipfile.ZipInfo("link.js")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(link, "../outside.js")
    content = output.getvalue()
    configure_fixture(monkeypatch, tmp_path, content)

    with pytest.raises(ValueError, match="symlink"):
        bitwarden.install_bitwarden_into_profile(tmp_path / "profile")

    assert not (tmp_path / "outside.js").exists()


def test_ensure_addon_downloads_and_extracts_only_once(monkeypatch, tmp_path):
    content = make_xpi()
    requests = configure_fixture(monkeypatch, tmp_path, content)
    downloads = []
    extractions = []
    real_download = bitwarden._download_xpi
    real_extract = bitwarden._extract_xpi

    def counting_download(destination):
        downloads.append(destination)
        return real_download(destination)

    def counting_extract(source, destination):
        extractions.append((source, destination))
        return real_extract(source, destination)

    monkeypatch.setattr(bitwarden, "_download_xpi", counting_download)
    monkeypatch.setattr(bitwarden, "_extract_xpi", counting_extract)

    addon = bitwarden.ensure_bitwarden_addon()

    assert addon == tmp_path / "kahin/addons/bitwarden"
    assert (addon / "manifest.json").is_file()
    assert (addon / "background.js").is_file()
    assert len(requests) == 1
    assert len(downloads) == 1
    assert len(extractions) == 1
    manifest_stat = (addon / "manifest.json").stat()

    second = bitwarden.ensure_bitwarden_addon()

    assert second == addon
    assert len(requests) == 1
    assert len(downloads) == 1
    assert len(extractions) == 1
    assert (addon / "manifest.json").stat().st_mtime_ns == manifest_stat.st_mtime_ns
    assert not list((tmp_path / "kahin/addons").glob(".bitwarden-extract-*"))


def test_ensure_addon_reuses_existing_valid_directory(monkeypatch, tmp_path):
    content = make_xpi()
    configure_fixture(monkeypatch, tmp_path, content)
    addon = write_valid_addon(tmp_path / "kahin/addons/bitwarden")
    original = (addon / "background.js").stat()

    def offline(*_args, **_kwargs):
        raise AssertionError("existing valid add-on must be reused without network")

    monkeypatch.setattr(bitwarden.urllib.request, "urlopen", offline)
    monkeypatch.setattr(
        bitwarden, "_extract_xpi", lambda *_args, **_kwargs: pytest.fail("must not re-extract")
    )

    result = bitwarden.ensure_bitwarden_addon()

    assert result == addon
    assert (addon / "background.js").stat().st_mtime_ns == original.st_mtime_ns


def test_corrupt_addon_directory_is_reextracted(monkeypatch, tmp_path):
    content = make_xpi()
    requests = configure_fixture(monkeypatch, tmp_path, content)
    addon = tmp_path / "kahin/addons/bitwarden"
    addon.mkdir(parents=True)
    (addon / "manifest.json").write_text("{ not json", encoding="utf-8")
    (addon / "stale.js").write_text("stale", encoding="utf-8")

    result = bitwarden.ensure_bitwarden_addon()

    assert result == addon
    manifest = json.loads((addon / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2026.9.0"
    assert not (addon / "stale.js").exists()
    assert len(requests) == 1
    assert not list((tmp_path / "kahin/addons").glob(".bitwarden-extract-*"))


def test_addon_with_unexpected_identity_is_reextracted(monkeypatch, tmp_path):
    content = make_xpi()
    configure_fixture(monkeypatch, tmp_path, content)
    addon = write_valid_addon(
        tmp_path / "kahin/addons/bitwarden",
        manifest={**valid_manifest(), "version": "0.0.1"},
    )

    result = bitwarden.ensure_bitwarden_addon()

    assert result == addon
    manifest = json.loads((addon / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2026.9.0"


def test_profile_install_is_idempotent(monkeypatch, tmp_path):
    content = make_xpi()
    configure_fixture(monkeypatch, tmp_path, content)
    profile = tmp_path / "profile"
    target = profile / "extensions/{446900e4-71c2-419f-a6a7-df9c091e268b}.xpi"

    bitwarden.install_bitwarden_into_profile(profile)
    before = target.stat()

    def offline(*_args, **_kwargs):
        raise AssertionError("repeat install must not touch the network")

    monkeypatch.setattr(bitwarden.urllib.request, "urlopen", offline)
    bitwarden.install_bitwarden_into_profile(profile)
    after = target.stat()

    assert before.st_mtime_ns == after.st_mtime_ns
    assert before.st_ino == after.st_ino


def test_kahin_home_must_be_absolute(monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", "relative/path")

    with pytest.raises(ValueError, match="absolute"):
        bitwarden.ensure_bitwarden_addon()


def test_addon_home_falls_back_to_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("KAHIN_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert bitwarden._addon_home() == tmp_path / "data" / "kahin" / "addons" / "bitwarden"


def test_profile_status_reports_registry_truth(monkeypatch, tmp_path):
    content = make_xpi()
    configure_fixture(monkeypatch, tmp_path, content)
    profile = tmp_path / "profile"
    bitwarden.install_bitwarden_into_profile(profile)

    # Firefox has not written its registry yet: present, active unknown.
    status = bitwarden.bitwarden_profile_status(profile)
    assert status["present"] is True
    assert status["active"] is None

    registry = profile / "extensions.json"
    registry.write_text(
        json.dumps({"addons": [{"id": bitwarden.BITWARDEN_GECKO_ID, "active": True}]})
    )
    assert bitwarden.bitwarden_profile_status(profile)["active"] is True

    registry.write_text(
        json.dumps({"addons": [{"id": bitwarden.BITWARDEN_GECKO_ID, "active": False}]})
    )
    assert bitwarden.bitwarden_profile_status(profile)["active"] is False


def test_profile_status_detects_a_deleted_xpi(monkeypatch, tmp_path):
    content = make_xpi()
    configure_fixture(monkeypatch, tmp_path, content)
    profile = tmp_path / "profile"
    target = Path(bitwarden.install_bitwarden_into_profile(profile))

    target.unlink()

    status = bitwarden.bitwarden_profile_status(profile)
    assert status["present"] is False
