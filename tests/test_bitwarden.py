import hashlib
import io
import json
import stat
import zipfile

import pytest

from kahin import bitwarden


def make_xpi(*, payload_size=128, manifest_overrides=None, extra_entries=()):
    manifest = {
        "manifest_version": 2,
        "name": "Bitwarden Password Manager",
        "version": "2026.9.0",
        "browser_specific_settings": {"gecko": {"id": "{446900e4-71c2-419f-a6a7-df9c091e268b}"}},
    }
    manifest.update(manifest_overrides or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("background.js", b"b" * payload_size)
        for name, contents in extra_entries:
            bundle.writestr(name, contents)
    return output.getvalue()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def configure_fixture(monkeypatch, tmp_path, content):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
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
