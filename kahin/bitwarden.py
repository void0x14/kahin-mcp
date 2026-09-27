"""Download and install the pinned official Bitwarden Firefox add-on."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

BITWARDEN_XPI_URL = (
    "https://addons.mozilla.org/firefox/downloads/file/5037282/"
    "bitwarden_password_manager-2026.9.0.xpi"
)
BITWARDEN_SHA256 = "324a2d97e365092fe9db0f0069e4c748c935858523868361a3277c1bbf339a17"
BITWARDEN_GECKO_ID = "{446900e4-71c2-419f-a6a7-df9c091e268b}"
BITWARDEN_VERSION = "2026.9.0"
BITWARDEN_MANIFEST_VERSION = 2

MAX_XPI_BYTES = 24 * 1024 * 1024
MAX_ADDON_BYTES = 96 * 1024 * 1024
MAX_ADDON_FILES = 256
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
_CHUNK_SIZE = 256 * 1024


def _cache_path() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return cache_home / "kahin" / "bitwarden" / f"bitwarden-{BITWARDEN_VERSION}.xpi"


def _kahin_home() -> Path:
    """Resolve the Kahin data home: KAHIN_HOME, else XDG_DATA_HOME, else ~/.local/share."""
    configured = os.environ.get("KAHIN_HOME")
    if configured:
        home = Path(configured).expanduser()
        if not home.is_absolute():
            raise ValueError("KAHIN_HOME must be an absolute path")
        return home
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / "kahin"
    return Path("~/.local/share/kahin").expanduser()


def _addon_home() -> Path:
    """Stable, shared home for the extracted pinned Bitwarden add-on."""
    return _kahin_home() / "addons" / "bitwarden"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            value.update(chunk)
    return value.hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Bitwarden XPI contains a path traversal entry")
    return path


def _verify_manifest_identity(manifest: object) -> None:
    """Reject any manifest that is not the exact pinned Bitwarden add-on."""
    if not isinstance(manifest, dict):
        raise ValueError("Bitwarden manifest.json must contain an object")
    browser_settings = manifest.get("browser_specific_settings")
    gecko = browser_settings.get("gecko", {}) if isinstance(browser_settings, dict) else {}
    if not gecko:
        applications = manifest.get("applications")
        gecko = applications.get("gecko", {}) if isinstance(applications, dict) else {}
    if (
        not isinstance(gecko, dict)
        or gecko.get("id") != BITWARDEN_GECKO_ID
        or manifest.get("version") != BITWARDEN_VERSION
        or manifest.get("manifest_version") != BITWARDEN_MANIFEST_VERSION
    ):
        raise ValueError("Bitwarden XPI manifest identity, version, or format is unexpected")


def _validate_xpi(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_XPI_BYTES:
        raise ValueError("Bitwarden XPI is missing or exceeds the compressed size limit")
    if _digest(path) != BITWARDEN_SHA256:
        raise ValueError("Bitwarden XPI SHA256 does not match the pinned release")
    try:
        with zipfile.ZipFile(path) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ADDON_FILES:
                raise ValueError("Bitwarden XPI exceeds the file count limit")
            total_size = 0
            seen: set[str] = set()
            manifests: list[zipfile.ZipInfo] = []
            for info in infos:
                member = _safe_member(info.filename)
                if info.filename in seen:
                    raise ValueError("Bitwarden XPI contains duplicate paths")
                seen.add(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise ValueError("Bitwarden XPI contains a symlink")
                if info.is_dir():
                    continue
                total_size += info.file_size
                if total_size > MAX_ADDON_BYTES:
                    raise ValueError("Bitwarden XPI exceeds the extracted size limit")
                if member.as_posix() == "manifest.json":
                    manifests.append(info)
            if len(manifests) != 1 or manifests[0].file_size > MAX_MANIFEST_BYTES:
                raise ValueError("Bitwarden XPI must contain one bounded root manifest.json")
            manifest = json.loads(bundle.read(manifests[0]))
    except zipfile.BadZipFile as exc:
        raise ValueError("pinned Bitwarden XPI is not a valid zip archive") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Bitwarden manifest.json is invalid") from exc
    _verify_manifest_identity(manifest)


def _download_xpi(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".bitwarden-download-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(
            BITWARDEN_XPI_URL, timeout=60
        ) as response:
            while chunk := response.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_XPI_BYTES:
                    raise ValueError("Bitwarden XPI exceeds the compressed size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != BITWARDEN_SHA256:
            raise ValueError("downloaded Bitwarden XPI SHA256 does not match the pinned release")
        _validate_xpi(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _cached_xpi() -> Path:
    path = _cache_path()
    if path.is_symlink():
        raise ValueError("Bitwarden XPI cache path must not be a symlink")
    try:
        _validate_xpi(path)
    except (OSError, ValueError):
        _download_xpi(path)
    return path


def _read_addon_manifest(directory: Path) -> dict:
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Bitwarden addon directory must contain a root manifest.json")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Bitwarden addon manifest.json exceeds the size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Bitwarden addon manifest.json is invalid") from exc
    _verify_manifest_identity(manifest)
    return manifest


def _addon_is_valid(directory: Path) -> bool:
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        _read_addon_manifest(directory)
    except (OSError, ValueError):
        return False
    return True


def _extract_xpi(source: Path, destination: Path) -> None:
    _validate_xpi(source)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("Bitwarden addon home must not be a symlink")
    temporary = Path(tempfile.mkdtemp(prefix=".bitwarden-extract-", dir=parent))
    try:
        with zipfile.ZipFile(source) as bundle:
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                member = _safe_member(info.filename)
                output = temporary / member
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as input_stream, output.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, _CHUNK_SIZE)
        _read_addon_manifest(temporary)
        # Publish the fully extracted add-on atomically; a crash leaves only the temp dir behind.
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def ensure_bitwarden_addon() -> Path:
    """Return the extracted pinned Bitwarden add-on, downloading and extracting it once if needed."""
    addon_dir = _addon_home()
    if _addon_is_valid(addon_dir):
        return addon_dir
    xpi = _cached_xpi()
    _extract_xpi(xpi, addon_dir)
    return addon_dir


def _install_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise ValueError("Firefox profile extensions directory must not be a symlink")
    if destination.is_symlink():
        raise ValueError("Bitwarden profile extension path must not be a symlink")
    if destination.exists():
        _validate_xpi(destination)
        return

    fd, temporary_name = tempfile.mkstemp(prefix=".bitwarden-install-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as input_stream, os.fdopen(fd, "wb") as output_stream:
            while chunk := input_stream.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_XPI_BYTES:
                    raise ValueError("Bitwarden XPI exceeds the compressed size limit")
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if digest.hexdigest() != BITWARDEN_SHA256:
            raise ValueError("Bitwarden XPI copy failed SHA256 verification")
        _validate_xpi(temporary)
        try:
            # A hard link publishes the complete file atomically and never replaces a target.
            os.link(temporary, destination)
        except FileExistsError:
            _validate_xpi(destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_bitwarden_into_profile(profile_dir: Path) -> str:
    """Install the signed Bitwarden XPI into a Firefox profile and return its path."""
    profile = Path(profile_dir).expanduser().resolve()
    ensure_bitwarden_addon()
    xpi = _cached_xpi()
    target = profile / "extensions" / f"{BITWARDEN_GECKO_ID}.xpi"
    _install_copy(xpi, target)
    return str(target)


def bitwarden_profile_status(profile_dir: Path) -> dict[str, Any]:
    """Report whether Bitwarden is really present and enabled in a profile.

    This reads the profile's own state instead of assuming the install worked,
    so a deleted/corrupted XPI or a disabled add-on is visible rather than
    claimed. Returns ``{present, active, xpi}`` where ``active`` is ``None``
    until Firefox has written ``extensions.json`` (first run not completed).
    """
    profile = Path(profile_dir).expanduser().resolve()
    xpi = profile / "extensions" / f"{BITWARDEN_GECKO_ID}.xpi"
    try:
        _validate_xpi(xpi)
        present = True
    except (OSError, ValueError):
        present = False

    active: bool | None = None
    registry = profile / "extensions.json"
    if registry.is_file():
        active = False
        try:
            data = json.loads(registry.read_bytes())
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            for addon in data.get("addons", []):
                if isinstance(addon, dict) and addon.get("id") == BITWARDEN_GECKO_ID:
                    active = bool(addon.get("active"))
                    break
    return {"present": present, "active": active, "xpi": str(xpi)}
