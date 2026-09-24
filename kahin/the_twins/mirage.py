"""the_twins/mirage.py — Mirage: real Camoufox via the Zig IPC sidecar.

The sidecar (camoufox-harness/core/ipc_main.zig) owns the Juggler pipe and
exposes a Juggler-native JSON-over-stdio contract: requests
{"id","method","params","sessionId"?} -> {"id","result"|"error"}; events flow
out as {"method","params","sessionId"} lines, verbatim.

Faz 9 Task 3 (phantom liveness):
- ``start()`` validates boot with ``Browser.health`` (dead sidecar -> raise)
- ``call(method, params, session_id)`` is THE wire entry point (no send_cdp)
- session map is fed by ``Browser.attachedToTarget`` / ``detachedFromTarget``
  events the sidecar forwards; ``create_page/close_page/switch_page/list_pages``
  manage which target page-scoped calls run on
- reader EOF (pipe closed / child died) marks the engine dead and fires
  ``on_death`` callbacks; dead engines reject calls instead of hanging
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import orjson

from kahin.dom_stream import DOM_STREAM_BINDING_NAME
from kahin.the_twins.chassis import BrowserEngine, EngineContext, EventData

try:
    from camoufox.utils import launch_options
except ImportError:  # pragma: no cover - harness without the camoflox package
    launch_options = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30.0
_PAGE_NAVIGATE_RESPONSE_TIMEOUT = 5.0
_IPC_WRITE_TIMEOUT = 5.0
_RESPONSE_BODY_RETRY_TIMEOUT = 5.0
_RESPONSE_BODY_RETRY_INTERVAL = 0.1
# asyncio.StreamReader defaults to 64 KiB. The sidecar deliberately returns
# bounded semantic trees/events up to 512 KiB, so the default turns a valid
# response into LimitOverrunError and kills the transport reader.
_IPC_STREAM_LIMIT = 64 * 1024 * 1024
_READER_SHUTDOWN_TIMEOUT = 1.0
_SIDECAR_SHUTDOWN_TIMEOUT = 3.0
_SIDECAR_KILL_TIMEOUT = 2.0
_CAMOUFOX_FETCH_TIMEOUT = 120.0
_CAMOUFOX_PROBE_TIMEOUT = 10.0
_CAMOUFOX_OUTPUT_LIMIT = 600
_MIRAGE_PAGE_DOMAINS = frozenset({"Page", "Runtime", "Network", "Input", "Accessibility", "Heap"})

# Faz 4 Task 3 — per-identity profile pre-warm. Camoufox's launch_options()
# deliberately injects per-launch randomness (font/voice subsets, spacing/
# audio/canvas seeds, webgl sampling, window.history.length), so neither the
# full options dict nor the runtime profile directory can be cached or
# reused safely: a reused profile would restore cookies/session data, and a
# pinned options snapshot would freeze the per-launch fingerprint rotation
# that Faz 3's identity tests rely on. The safe seam is therefore bounded
# preparation metadata only — a stable identity hash plus measured prep
# timings — kept in-process and mirrored to a small per-identity file under
# the cache dir. It never skips real launch work and never contains identity
# payloads.
_PREWARM_CACHE_MAX = 8
_PREWARM_METADATA_MAX = 4096  # bytes written per identity
_PREWARM_READ_MAX = 8192  # refuse metadata files larger than this
_PREWARM_CACHE: dict[str, dict[str, Any]] = {}

_PROFILE_DIR_ENV = "KAHIN_PROFILE_DIR"
_PROFILE_DIR_MAX_LENGTH = 4096


def _profile_directory(
    persistent: bool,
    configured: str | None,
) -> tuple[Path | None, bool]:
    """Resolve the browser profile policy without touching the filesystem.

    The default profile is deliberately stable so Firefox can retain its
    native cookies, web storage and other session data between launches.
    Tests and callers that need a disposable browser can opt out explicitly;
    the old temporary-profile behavior is preserved for that path.
    """
    if not persistent:
        return None, False
    if configured is not None:
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("profile directory must be a non-empty absolute path")
        raw = configured.strip()
    else:
        raw = os.environ.get(_PROFILE_DIR_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
        path = root / "kahin" / "profile"
    if len(str(path)) > _PROFILE_DIR_MAX_LENGTH:
        raise ValueError(f"profile directory exceeds {_PROFILE_DIR_MAX_LENGTH} characters")
    if not path.is_absolute():
        raise ValueError("profile directory must be an absolute path")
    return path, True


def _profile_cache_dir() -> Path:
    """Bounded cache root for per-identity pre-warm metadata."""
    env = os.environ.get("KAHIN_PROFILE_CACHE_DIR")
    path = Path(env).expanduser() if env else (Path.home() / ".cache" / "kahin" / "profiles")
    return path


def _identity_hash(config: dict[str, Any]) -> str:
    """Stable, safe key for an identity config: sha256 of canonical JSON."""
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _effective_identity_hash(env: dict[str, Any], policy: dict[str, Any]) -> str:
    """Stable, bounded digest of the effective launch identity material.

    Default (fresh BrowserForge) launches have no pinned config to hash, so
    the effective identity material is the ``CAMOU_CONFIG_*`` fingerprint
    env generated for THIS launch plus the safe launch policy (headless/
    humanize/cache/webgl/main-world and proxy presence). The raw fingerprint
    payload exists only inside this function — callers store and expose just
    the 16-hex digest, never the config itself.
    """
    material: dict[str, Any] = {
        "policy": policy,
        "camou_config": {
            key: value
            for key, value in env.items()
            if key.startswith("CAMOU_CONFIG_") and isinstance(value, (str, int, float, bool))
        },
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _prewarm_load(identity_hash: str) -> dict[str, Any] | None:
    """In-process first, then bounded on-disk metadata lookup (never raises)."""
    entry = _PREWARM_CACHE.get(identity_hash)
    if entry is not None:
        return {**entry, "source": "memory"}
    path = _profile_cache_dir() / f"{identity_hash}.json"
    try:
        raw = path.read_bytes()
        if len(raw) > _PREWARM_READ_MAX:
            return None
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("identity_hash") != identity_hash:
        return None
    return {**parsed, "source": "disk"}


def _prewarm_record(identity_hash: str, entry: dict[str, Any]) -> None:
    """Bound the in-process cache and mirror metadata to disk (never raises)."""
    _PREWARM_CACHE[identity_hash] = entry
    while len(_PREWARM_CACHE) > _PREWARM_CACHE_MAX:
        _PREWARM_CACHE.pop(next(iter(_PREWARM_CACHE)))
    path = _profile_cache_dir() / f"{identity_hash}.json"
    try:
        payload = json.dumps(entry, sort_keys=True).encode("utf-8")
        if len(payload) > _PREWARM_METADATA_MAX:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
    except OSError:
        pass


def _needs_mirage_page_for_adapter(domain: str, command: str) -> bool:
    return domain in _MIRAGE_PAGE_DOMAINS and not (domain == "Page" and command == "close")


def _sidecar_bin() -> Path:
    env = os.environ.get("KAHIN_ZIG_CORE")
    if env:
        path = Path(env) / "zig-out" / "bin" / "kahin-sidecar"
        if path.is_file():
            return path
        raise RuntimeError(f"KAHIN_ZIG_CORE points to a missing sidecar: {path}")
    # Installed wheel ships the sidecar inside the package (kahin/_vendor/);
    # source-tree installs use the repo copy. Prefer the package copy so the
    # pip-installed runtime (pnpm launcher -> PyPI) never needs the repo.
    pkg = Path(__file__).resolve().parents[1] / "_vendor" / "kahin-sidecar"
    if pkg.is_file():
        return pkg
    path = (
        Path(__file__).resolve().parents[2] / "camoufox-harness" / "vendor" / "bin" / "kahin-sidecar"
    )
    if not path.is_file():
        raise RuntimeError(
            f"Vendored sidecar binary missing: {path}. Rebuild it with scripts/build-sidecar.sh"
        )
    return path


def _camoufox_readiness_error(reason: str, **details: Any) -> RuntimeError:
    """Build a stable, actionable error for missing Camoufox readiness."""
    payload: dict[str, Any] = {
        "error": "camoufox_not_ready",
        "reason": reason,
        "actions": {
            "install_package": f"{sys.executable} -m pip install camoufox",
            "fetch_browser": f"{sys.executable} -m camoufox fetch",
            "explicit_binary": "KAHIN_CAMOUFOX_BIN=/absolute/path/to/camoufox-bin",
        },
    }
    payload.update(details)
    return RuntimeError(f"Camoufox readiness failed: {json.dumps(payload, sort_keys=True)}")


def _usable_camoufox_bin(path: Path) -> bool:
    """Require a real executable, not merely a stale cache entry."""
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _camoufox_package_roots() -> list[Path]:
    try:
        spec = importlib.util.find_spec("camoufox")
    except (ImportError, ModuleNotFoundError, ValueError):
        return []
    if spec is None:
        return []
    roots = [Path(root) for root in (spec.submodule_search_locations or ())]
    if spec.origin and spec.origin not in {"built-in", "frozen"}:
        roots.append(Path(spec.origin).parent)
    return list(dict.fromkeys(roots))


def _camoufox_cli_commands(subcommand: str) -> list[list[str]]:
    """Return official Camoufox CLI forms available in this environment."""
    commands: list[list[str]] = []
    if _camoufox_package_roots():
        commands.append([sys.executable, "-m", "camoufox", subcommand])
    cli = shutil.which("camoufox")
    if cli:
        commands.append([cli, subcommand])
    return [list(command) for command in dict.fromkeys(tuple(command) for command in commands)]


def _camoufox_package_candidates() -> list[Path]:
    candidates: list[Path] = []
    for root in _camoufox_package_roots():
        for name in ("camoufox-bin", "camoufox", "camoufox.exe"):
            direct = root / name
            if _usable_camoufox_bin(direct):
                candidates.append(direct)
        try:
            candidates.extend(
                path for path in root.glob("**/camoufox-bin") if _usable_camoufox_bin(path)
            )
        except OSError:
            continue
    return list(dict.fromkeys(candidates))


def _camoufox_cache_candidates() -> list[Path]:
    roots: list[Path] = []
    if os.environ.get("XDG_CACHE_HOME"):
        roots.append(Path(os.environ["XDG_CACHE_HOME"]))
    roots.append(Path.home() / ".cache")
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]))
    roots.append(Path.home() / "Library" / "Caches")

    candidates: list[Path] = []
    for root in dict.fromkeys(roots):
        official = root / "camoufox" / "browsers" / "official"
        try:
            candidates.extend(
                path for path in official.glob("*/camoufox-bin") if _usable_camoufox_bin(path)
            )
        except OSError:
            continue
    return sorted(dict.fromkeys(candidates), key=lambda path: (path.parent.name, str(path)))


def _camoufox_cli_path() -> Path | None:
    """Ask the installed official CLI for its resolved executable path."""
    for command in _camoufox_cli_commands("path"):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_CAMOUFOX_PROBE_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        for line in reversed((result.stdout or "").splitlines()):
            candidate = Path(line.strip().strip("\"'")).expanduser()
            if _usable_camoufox_bin(candidate):
                return candidate
    return None


def _run_camoufox_fetch() -> dict[str, Any]:
    """Run the official fetch command once, with a hard wall-clock bound."""
    commands = _camoufox_cli_commands("fetch")
    if not commands:
        raise _camoufox_readiness_error(
            "python_package_missing",
            detail="The Camoufox Python package/CLI is not installed in this interpreter.",
        )

    deadline = time.monotonic() + _CAMOUFOX_FETCH_TIMEOUT
    attempts: list[dict[str, Any]] = []
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            attempts.append({"command": command, "status": "timeout"})
            break
        except OSError as exc:
            attempts.append({"command": command, "status": "exec_error", "detail": str(exc)})
            continue
        if result.returncode == 0:
            return {"command": command, "status": "ok"}
        attempts.append({
            "command": command,
            "status": "failed",
            "returncode": result.returncode,
            "output": ((result.stderr or result.stdout or "").strip())[:_CAMOUFOX_OUTPUT_LIMIT],
        })
    raise _camoufox_readiness_error(
        "fetch_failed",
        detail="The official Camoufox fetch command did not complete successfully.",
        timeoutSeconds=_CAMOUFOX_FETCH_TIMEOUT,
        attempts=attempts,
    )


def _camoufox_bin() -> Path:
    """Resolve a usable Camoufox binary, fetching it through the official CLI.

    Resolution is deliberately confined to Camoufox's own package/CLI and
    cache. It never installs or invokes another automation engine.
    """
    env = os.environ.get("KAHIN_CAMOUFOX_BIN")
    if env:
        path = Path(env).expanduser()
        if _usable_camoufox_bin(path):
            return path
        raise _camoufox_readiness_error(
            "invalid_explicit_binary",
            path=str(path),
            detail="KAHIN_CAMOUFOX_BIN must point to an executable Camoufox binary.",
        )

    package_bin = _camoufox_cli_path()
    if package_bin is not None:
        return package_bin
    package_candidates = _camoufox_package_candidates()
    if package_candidates:
        return package_candidates[-1]
    cache_candidates = _camoufox_cache_candidates()
    if cache_candidates:
        return cache_candidates[-1]

    _run_camoufox_fetch()
    package_bin = _camoufox_cli_path()
    if package_bin is not None:
        return package_bin
    package_candidates = _camoufox_package_candidates()
    if package_candidates:
        return package_candidates[-1]
    cache_candidates = _camoufox_cache_candidates()
    if cache_candidates:
        return cache_candidates[-1]
    raise _camoufox_readiness_error(
        "fetch_succeeded_binary_missing",
        detail="Camoufox fetch completed, but no executable was reported or found in its cache.",
        cacheHints=["~/.cache/camoufox/browsers/official/*/camoufox-bin"],
    )


class Mirage(BrowserEngine):
    """Stealth Camoufox engine speaking Juggler methods over the Zig sidecar."""

    def __init__(self, engine_name: str = "mirage") -> None:
        super().__init__()
        self._engine_name = engine_name
        self._stderr_file = None
        self._stderr_path: Path | None = None
        self._profile_dir: Path | None = None
        self._persistent_profile = False
        self._headless = True
        self._marionette_port: int | None = None
        self._passkey_mode = False
        self._addons: list[str] = []
        # Active identity applied at launch (Faz 2 Task 5): the resolved
        # fingerprint config plus the saved identity name it came from.
        # Retained only while this engine is running, for
        # ``kahin_identity_report``.
        self._identity_config: dict[str, Any] | None = None
        self._identity_name: str | None = None
        self._proxy_url: str | None = None
        # Faz 4 Task 3: pre-warm metadata + monotonic uptime anchor. Kept
        # only while this engine is running; cleared in stop().
        self._identity_hash: str | None = None
        self._prewarm_info: dict[str, Any] | None = None
        # Crawler/rotation Task 1: the launch policy actually bound on this
        # start (headless/humanize/cache/webgl/main-world + proxy presence),
        # exposed by identity/status surfaces without any fingerprint data.
        self._launch_policy: dict[str, Any] | None = None
        self._started_monotonic: float | None = None
        self._write_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()
        # Switching the current tab is a routing operation, not browser
        # state that can be changed halfway through resolving a call. The
        # lock only protects target/session selection; wire calls themselves
        # remain concurrent behind ``_write_lock`` and the pending map.
        self._target_lock = asyncio.Lock()
        # targetId -> Juggler sessionId, learned from attachedToTarget events.
        self._sessions: dict[str, str] = {}
        self._target_infos: dict[str, dict[str, Any]] = {}
        self._current_target: str | None = None
        # sessionId -> {frameId -> executionContextId} (main world only),
        # fed by Runtime.executionContextCreated/Destroyed/ContextsCleared
        # events the sidecar forwards verbatim. DOM tools resolve a frame_id
        # to the main-world context of that frame.
        self._frame_contexts: dict[str, dict[str, str]] = {}
        # Page.fileChooserOpened (file-input click while interception is on):
        # bounded per-session queue, consumed by wait_for_chooser (Gap C
        # upload). A single global slot lets a second tab steal the first
        # tab's chooser during concurrent uploads.
        self._pending_choosers: deque[tuple[str | None, dict[str, Any]]] = deque(maxlen=32)
        self._chooser_event = asyncio.Event()
        # Page.screencastFrame (live screencast, Gap D): params of every
        # unconsumed frame (data is base64-JPEG, ack'd on consume — see
        # wait_for_screencast_frame). The event carries NO screencastId, so
        # the stream id returned by Page.startScreencast is tracked here and
        # echoed back on every screencastFrameAck.
        self._screencast_frames: deque[dict[str, Any]] = deque(maxlen=64)
        self._screencast_event = asyncio.Event()
        self._screencast_id: str | None = None
        self._screencast_session_id: str | None = None
        self._screencast_starting = False
        self._screencast_starting_session_id: str | None = None
        self._screencast_generation = 0
        self._screencast_lock = asyncio.Lock()
        # Real-time DOM observation uses the browser's native binding/event
        # bridge. The page owns the bounded mutation ring; this signal only
        # wakes a waiting tool so the reader never carries DOM payloads.
        self._dom_binding_installed = False
        self._dom_init_script_installed = False
        self._dom_signal = asyncio.Event()
        # Network interception is surfaced by Network.requestWillBeSent with
        # params.isIntercepted=true in this Juggler build. Keep a bounded,
        # engine-local queue so a route decision cannot consume another tab's
        # request from the global diagnostic buffer.
        self._network_events: deque[dict[str, Any]] = deque(maxlen=2_000)
        self._network_signal = asyncio.Event()
        self._network_routed_ids: set[str] = set()

    def _reset_capture_state(self, *, wake_waiters: bool) -> None:
        """Drop capture/chooser state that belongs to the old browser.

        Capture events are not durable browser state. Keeping them across a
        stop/start cycle can make a later upload consume an old chooser or a
        later screencast return a frame from a previous stream. On shutdown,
        wake waiters so they can observe liveness immediately; start() clears
        the wake-up events before the new browser is exposed.
        """
        self._pending_choosers.clear()
        self._screencast_frames.clear()
        self._screencast_id = None
        self._screencast_session_id = None
        self._screencast_starting = False
        self._screencast_starting_session_id = None
        self._network_events.clear()
        self._network_routed_ids.clear()
        if wake_waiters:
            self._chooser_event.set()
            self._screencast_event.set()
            self._network_signal.set()
        else:
            self._chooser_event.clear()
            self._screencast_event.clear()
            self._network_signal.clear()

    async def start(
        self,
        headless: bool = True,
        port: int = 0,
        *,
        passkey_mode: bool = False,
        **kwargs: Any,
    ) -> EngineContext:
        del port  # Juggler pipe: no port.
        self._headless = bool(headless)
        # A Mirage object is normally single-use, but resetting these fields
        # makes a stop/start cycle deterministic and prevents stale tab or
        # liveness state from leaking into a replacement browser.
        self._dead = False
        self._stopping = False
        self._msg_id = 0
        self._sessions.clear()
        self._target_infos.clear()
        self._frame_contexts.clear()
        self._current_target = None
        self._dom_binding_installed = False
        self._dom_init_script_installed = False
        self._dom_signal.clear()
        self._network_events.clear()
        self._network_routed_ids.clear()
        self._network_signal.clear()
        self._reset_capture_state(wake_waiters=False)
        self._identity_config = None
        self._identity_name = None
        self._proxy_url = None
        self._identity_hash = None
        self._prewarm_info = None
        self._launch_policy = None
        self._started_monotonic = None
        self._persistent_profile = False
        self._marionette_port = None
        self._passkey_mode = False
        configured_profile = kwargs.get("profile_dir")
        persistent_profile = kwargs.get("persistent_profile", True)
        if not isinstance(persistent_profile, bool):
            raise ValueError("persistent_profile must be a boolean")
        if not isinstance(passkey_mode, bool):
            raise ValueError("passkey_mode must be a boolean")
        if passkey_mode and not persistent_profile:
            raise ValueError("passkey_mode requires persistent_profile=True")
        self._passkey_mode = passkey_mode
        profile_dir, self._persistent_profile = _profile_directory(
            persistent_profile,
            configured_profile,
        )
        if passkey_mode:
            # Firefox Marionette shares the existing Camoufox process. Bind
            # to loopback only and release immediately so Firefox can claim
            # this ephemeral port during startup.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                self._marionette_port = int(sock.getsockname()[1])
        addons_input = kwargs.get("addons")
        validated_addons: list[str] = []
        if addons_input is not None:
            if not isinstance(addons_input, (list, tuple)):
                raise ValueError("addons must be a list of staged addon directory paths")
            if len(addons_input) > 16:
                raise ValueError("addons exceeds maximum 16 extensions")
            from kahin.extensions import inspect_extension
            for item in addons_input:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("addon item must be a non-empty string")
                report = inspect_extension(item)
                if not report.get("compatible"):
                    unsupported_str = ", ".join(report.get("unsupported", []))
                    raise ValueError(f"Addon '{item}' is not compatible: {unsupported_str}")
                validated_addons.append(report["path"])
        self._addons = validated_addons
        identity_config = kwargs.get("identity")
        identity_name = kwargs.get("identity_name")
        self._identity_config = (
            identity_config if isinstance(identity_config, dict) and identity_config else None
        )
        self._identity_name = identity_name if isinstance(identity_name, str) and identity_name else None
        # Per-identity profile pre-warm (Faz 4 Task 3): resolve the stable
        # identity hash and check the bounded preparation-metadata cache.
        # A cache hit only records that this identity was prepared before;
        # launch_options is still called because its output is randomized
        # per launch by design (see the module docstring on _PREWARM_CACHE).
        prewarm: dict[str, Any] | None = None
        prewarm_hash: str | None = None
        if self._identity_config is not None:
            prewarm_hash = _identity_hash(self._identity_config)
            prewarm = _prewarm_load(prewarm_hash)
        # Validate the proxy before constructing launch options so the same
        # validated value can drive Camoufox's native proxy/geoip seam and
        # the existing environment defense-in-depth seam below. Credentials
        # remain internal to the launch call and env; no display/log path
        # receives the raw URL.
        proxy_url = kwargs.get("proxy")
        validated_proxy_url: str | None = None
        proxy_environment: dict[str, str] | None = None
        if isinstance(proxy_url, str) and proxy_url.strip():
            from kahin.stealth import proxy_env  # noqa: PLC0415 - pure helper

            candidate_proxy_url = proxy_url.strip()
            try:
                proxy_environment = proxy_env(candidate_proxy_url)
                validated_proxy_url = candidate_proxy_url
            except ValueError:
                logger.warning("proxy env merge skipped: invalid proxy URL")
        # Real launch policy (crawler/rotation Task 1): the same fixed
        # Camoufox options are bound on EVERY start — default and identity —
        # so fingerprint generation, user.js prefs and args never drift from
        # the enabled stealth policy. ``headless`` is the caller's real
        # flag; ``main_world_eval=False`` keeps the Juggler main-world
        # binding off (the DOM stream uses browser-level addBinding and
        # init scripts, never allowMainWorld).
        from kahin.stealth import launch_policy  # noqa: PLC0415 - pure helper

        policy = launch_policy(headless=headless)
        # launch_options takes `config` and `firefox_user_prefs` as named
        # parameters, so they must never ride inside **launch_kwargs — the
        # identity path below passes its own `config=` and would collide.
        policy_config: dict[str, Any] = dict(policy.get("config") or {})
        policy_prefs: dict[str, Any] = dict(policy.get("firefox_user_prefs") or {})
        launch_kwargs: dict[str, Any] = {
            key: value for key, value in policy.items()
            if key not in ("config", "firefox_user_prefs")
        }
        launch_kwargs["firefox_user_prefs"] = policy_prefs
        if self._addons:
            launch_kwargs["addons"] = self._addons
        if validated_proxy_url is not None:
            # Camoufox uses this native proxy config for proxy-aware
            # fingerprint geo alignment. The raw URL is passed only to the
            # library; it is never logged or returned by Kahin.
            launch_kwargs.update({
                "proxy": {"server": validated_proxy_url},
                "geoip": True,
            })
        # BrowserForge fingerprint -> CAMOU_CONFIG_* env (master plan §2.1.5).
        # Every start() draws a fresh identity; the sidecar passes our
        # environment through to the Camoufox child verbatim (pipe.zig
        # buildEnvp reads /proc/self/environ).
        opts_t0 = time.monotonic()
        if launch_options is None:
            opts = {"env": {}, "firefox_user_prefs": {}}
        else:
            try:
                opts = launch_options(config=policy_config, **launch_kwargs)
            except Exception as exc:  # noqa: BLE001 - geoip is an optional proxy seam
                if validated_proxy_url is None or launch_kwargs.get("geoip") is not True:
                    raise
                # Native geo alignment performs an external IP lookup through
                # the proxy. A dead, local, or temporarily unavailable proxy
                # must not prevent the browser from starting; retain the
                # proxy and fall back to proxy-only launch semantics.
                logger.warning(
                    "Camoufox geoip lookup failed; continuing with proxy-only launch (%s)",
                    type(exc).__name__,
                )
                launch_kwargs = {**launch_kwargs, "geoip": False}
                opts = launch_options(config=policy_config, **launch_kwargs)
        # Identity config (Faz 2 Task 5): merge through the same seam that
        # applies the default fingerprint. ``launch_options(config=...)``
        # regenerates the full option set (env with CAMOU_CONFIG_*, user.js
        # prefs, args) and overlays the pinned values on top, so unrelated
        # defaults are preserved and the identity takes precedence. A failed
        # injection must never crash boot; fall back to a fresh random
        # fingerprint instead.
        if self._identity_config is not None:
            if launch_options is None:
                logger.warning("identity config ignored: camoufox launch_options unavailable")
            else:
                try:
                    opts = launch_options(
                        config={**policy_config, **self._identity_config},
                        i_know_what_im_doing=True,
                        **launch_kwargs,
                    )
                except Exception:  # noqa: BLE001 - identity must not crash boot
                    logger.warning("identity config injection failed; using defaults", exc_info=True)
        opts_ms = (time.monotonic() - opts_t0) * 1000
        env = {**os.environ, **opts["env"]}
        if self._passkey_mode:
            env["MOZ_MARIONETTE"] = "1"
        else:
            env.pop("MOZ_MARIONETTE", None)
        # Allow hardware GPU acceleration unless explicitly overridden by environment
        if os.environ.get("KAHIN_FORCE_SOFTWARE_GL") == "1":
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        # Proxy (Faz 3 Task 5): merge through the same env seam the sidecar
        # passes to the Camoufox child (create_subprocess_exec env below).
        # Firefox honors ALL_PROXY/HTTPS_PROXY/HTTP_PROXY/NO_PROXY for its
        # network layer; the helper validates and maps a single proxy URL.
        # Credentials stay inside the env only — never logged. The raw URL
        # is retained as engine metadata so a later browser_start reuse can
        # detect a requested configuration mismatch instead of silently
        # ignoring it. A failed merge must never crash boot.
        if proxy_environment is not None and validated_proxy_url is not None:
            env.update(proxy_environment)
            self._proxy_url = validated_proxy_url
        else:
            self._proxy_url = None

        # Safe launch metadata for the surfaces (never the raw fingerprint
        # or proxy URL): the bound policy plus proxy presence only.
        self._launch_policy = {**policy, "proxy": self._proxy_url is not None}
        # Every launch reports the effective CAMOU_CONFIG_* material actually
        # handed to Camoufox, including launches seeded from a saved identity.
        # The saved identity remains the configuration input, but does not
        # freeze the per-launch fingerprint. A restart must either produce a
        # different real digest or be rejected by the crawler rotation gate.
        # Raw fingerprint payloads are never logged or returned.
        self._identity_hash = _effective_identity_hash(env, self._launch_policy)

        # firefox_user_prefs -> <profile>/user.js (webgl etc. must be set
        # before the browser boots; the sidecar only mkdirs the profile).
        if profile_dir is None:
            profile_dir = Path(tempfile.mkdtemp(prefix="kahin-fp-"))
        else:
            profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir = profile_dir
        profile_t0 = time.monotonic()
        try:
            prefs = opts.get("firefox_user_prefs") or {}
            if self._passkey_mode:
                prefs = {**prefs, "marionette.port": self._marionette_port}
            if prefs:
                lines = [f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()]
                (profile_dir / "user.js").write_text("\n".join(lines) + "\n")

            # Binary discovery may invoke the official networked fetch. Keep
            # that bounded operation off the MCP event loop so cancellation
            # and health requests remain responsive while readiness resolves.
            camoufox_bin = await asyncio.to_thread(_camoufox_bin)
            args = [str(_sidecar_bin()), str(camoufox_bin)]
            if not headless:
                args.append("--visible")  # visible window (stealth vs anti-bot)
            args.append(str(profile_dir))
            log_dir = Path(__file__).resolve().parents[2] / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            # File lives as long as the child process, not a with-block.
            # Keep stderr diagnostics attributable to one sidecar/session.
            # A shared append-only file made old allocator leaks and unrelated
            # browser deaths look like the current crash, especially when
            # multiple MCP clients were being investigated concurrently.
            self._stderr_path = log_dir / f"kahin-sidecar-{os.getpid()}-{time.time_ns()}.err"
            self._stderr_file = await asyncio.to_thread(open, self._stderr_path, "ab")
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._stderr_file,
                env=env,
                limit=_IPC_STREAM_LIMIT,
            )
            self._start_reader()
            # Boot validation: the sidecar must report a live browser. A dead
            # sidecar (or browser that never came up) fails the start.
            health = await self.call("Browser.health")
            if not health.get("alive"):
                raise RuntimeError(
                    "Mirage boot validation failed: Browser.health reports a dead browser"
                )
            # The engine is live: anchor uptime (monotonic) and update the
            # per-identity pre-warm metadata. A failure here must never fail
            # boot — record helpers swallow OSError, and the lookup above
            # already tolerated corrupt cache state.
            self._started_monotonic = time.monotonic()
            if prewarm_hash is not None:
                hits = (int(prewarm.get("hits", 0)) + 1) if prewarm is not None else 0
                starts = (int(prewarm.get("starts", 0)) + 1) if prewarm is not None else 1
                entry = {
                    "identity_hash": prewarm_hash,
                    "options_ms": round(opts_ms, 1),
                    "profile_ms": round((time.monotonic() - profile_t0) * 1000, 1),
                    "hits": hits,
                    "starts": starts,
                }
                _prewarm_record(prewarm_hash, entry)
                self._prewarm_info = {
                    **entry,
                    "cache": prewarm.get("source", "miss") if prewarm is not None else "miss",
                }
            return EngineContext(
                engine_name=self._engine_name,
                ws_url="",  # IPC over stdio, not WebSocket
                meta={
                    "sidecar": str(_sidecar_bin()),
                    "camoufox": str(camoufox_bin),
                    "transport": "stdio-jsonl",
                },
            )
        except BaseException:
            # asyncio.wait_for(browser_start) cancels start() on a slow boot.
            # Always reap the sidecar in that path; otherwise a retry creates
            # a second Camoufox while the first orphan keeps running.
            await self.stop()
            raise

    def _start_reader(self) -> None:
        """Background task: resolve pending responses, track Juggler sessions
        (attachedToTarget/detachedFromTarget), dispatch events."""

        async def reader() -> None:
            assert self._process is not None and self._process.stdout is not None
            death_reason = "sidecar_stdout_closed"
            try:
                while True:
                    raw = await self._process.stdout.readline()
                    if not raw:
                        break  # sidecar closed stdout (browser gone)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("sidecar sent a non-JSON line: %.120s", raw)
                        continue
                    if "id" in data:
                        fut = self._pending.pop(data["id"], None)
                        if fut is not None and not fut.done():
                            if "error" in data:
                                fut.set_exception(RuntimeError(f"CDP error: {data['error']}"))
                            else:
                                fut.set_result(data.get("result", {}))
                    elif "method" in data:
                        # Event metadata is auxiliary state. A malformed
                        # browser event must be logged and dropped, never
                        # allowed to terminate the transport reader while
                        # requests are still in flight.
                        for tracker in (
                            self._track_session,
                            self._track_target_url,
                            self._track_context,
                            self._track_chooser,
                            self._track_screencast,
                            self._track_dom_binding,
                            self._track_network_event,
                        ):
                            try:
                                tracker(data)
                            except Exception:  # noqa: BLE001 - one event cannot kill the reader
                                logger.warning("malformed Mirage event metadata in %s", data.get("method"), exc_info=True)
                        evt = EventData(
                            method=data["method"],
                            params=data.get("params", {}),
                            session_id=data.get("sessionId"),
                        )
                        for cb in self._event_callbacks:
                            try:
                                result = cb(evt)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception:
                                logger.exception("event callback failed for %s", evt.method)
            except asyncio.CancelledError:
                death_reason = "reader_cancelled"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Mirage reader stopped unexpectedly")
                detail = str(exc).replace("\n", " ")[:160]
                death_reason = f"reader_error:{type(exc).__name__}:{detail}"
            finally:
                # The browser died; fail every in-flight request.
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("Mirage sidecar exited"))
                self._pending.clear()
                self._mark_dead(death_reason)

        self._reader = asyncio.create_task(reader())

    def _track_context(self, data: dict[str, Any]) -> None:
        """Maintain sessionId -> {frameId -> executionContextId} (main world).

        Runtime.executionContextCreated carries the frame in auxData.frameId;
        contexts with a name (e.g. __playwright_utility_world__) do NOT map
        to page DOM, so only unnamed (main world) contexts are kept.
        executionContextDestroyed drops the id; executionContextsCleared
        (full navigation) resets the session's map.
        """
        method = data.get("method")
        params = data.get("params", {}) or {}
        sid = data.get("sessionId")
        if method == "Runtime.executionContextCreated":
            ctx_id = params.get("executionContextId")
            aux = params.get("auxData") or {}
            frame_id = aux.get("frameId")
            if not ctx_id or not frame_id or not sid:
                return
            if aux.get("name"):
                return  # utility world — not page DOM
            self._frame_contexts.setdefault(sid, {})[frame_id] = ctx_id
        elif method == "Runtime.executionContextDestroyed":
            ctx_id = params.get("executionContextId")
            if not sid or not ctx_id:
                return
            for frame_id, cid in list(self._frame_contexts.get(sid, {}).items()):
                if cid == ctx_id:
                    del self._frame_contexts[sid][frame_id]
        elif method == "Runtime.executionContextsCleared":
            if sid:
                self._frame_contexts.pop(sid, None)

    def resolve_context(self, frame_id: str, session_id: str | None = None) -> str | None:
        """Main-world executionContextId for a frame on a live target.

        Multi-step actions pass the session captured at their start. The
        selected-target fallback remains for one-shot legacy callers.
        """
        sid = session_id or self._sessions.get(self._current_target or "")
        if not sid:
            return None
        return self._frame_contexts.get(sid, {}).get(frame_id)

    async def install_dom_stream(self, init_script: str) -> None:
        """Install the browser-native binding and init script once.

        Browser-level Juggler methods are used so the observer follows newly
        created tabs and navigated frames. The caller still evaluates the
        script in the current frame because init scripts only affect future
        documents.
        """
        if not self._dom_binding_installed:
            await self.call("Browser.addBinding", {
                "name": DOM_STREAM_BINDING_NAME,
                "script": "function() {}",
            })
            self._dom_binding_installed = True
        if not self._dom_init_script_installed:
            await self.call("Browser.setInitScripts", {
                "scripts": [{"script": init_script}],
            })
            self._dom_init_script_installed = True

    async def wait_for_dom_signal(self, timeout: float) -> bool:
        """Wait until the page reports a DOM mutation through the binding."""
        try:
            await asyncio.wait_for(self._dom_signal.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._dom_signal.clear()

    def _track_dom_binding(self, data: dict[str, Any]) -> None:
        """Wake DOM stream consumers without copying page payloads."""
        if data.get("method") != "Page.bindingCalled":
            return
        params = data.get("params", {}) or {}
        if params.get("name") == DOM_STREAM_BINDING_NAME:
            self._dom_signal.set()

    def _track_network_event(self, data: dict[str, Any]) -> None:
        """Buffer bounded Network events for one-shot route decisions.

        The sidecar schema deliberately has no ``requestIntercepted`` event;
        interception is marked on ``Network.requestWillBeSent``. The raw
        params are retained so the native continuation helpers receive the
        exact request/session identifiers emitted by the browser.
        """
        method = data.get("method")
        params = data.get("params")
        if not isinstance(method, str) or not method.startswith("Network.") or not isinstance(params, dict):
            return
        self._network_events.append({
            "method": method,
            "params": params,
            "session_id": data.get("sessionId"),
            "_kahin_received_at": time.monotonic(),
        })
        self._network_signal.set()

    async def wait_for_intercepted(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float,
        *,
        after_monotonic: float | None = None,
    ) -> dict[str, Any] | None:
        """Wait for a fresh intercepted Network event matching ``predicate``.

        The engine keeps a bounded event window for diagnostics. Route calls
        must not accidentally consume an older paused request from that
        window, so callers can provide the monotonic start of their one-shot
        wait.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            events = list(self._network_events)
            for event in events:
                if event.get("method") != "Network.requestWillBeSent":
                    continue
                received_at = event.get("_kahin_received_at")
                if after_monotonic is not None and (
                    not isinstance(received_at, (int, float)) or received_at < after_monotonic
                ):
                    continue
                params = event.get("params") or {}
                request_id = params.get("requestId")
                if not isinstance(request_id, str) or request_id in self._network_routed_ids:
                    continue
                if params.get("isIntercepted") is True and predicate(event):
                    self._network_routed_ids.add(request_id)
                    # Keep the dedupe set in step with the bounded deque:
                    # once the window is full, older events are stale anyway.
                    if len(self._network_routed_ids) > self._network_events.maxlen:
                        self._network_events.clear()
                        self._network_routed_ids.clear()
                    return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._network_signal.wait(), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                pass
            finally:
                self._network_signal.clear()

    def _track_chooser(self, data: dict[str, Any]) -> None:
        """Record Page.fileChooserOpened (file input clicked while
        Page.setInterceptFileChooserDialog is enabled) into the bounded
        per-session queue and set the event so wait_for_chooser can consume
        it (Gap C)."""
        if data.get("method") == "Page.fileChooserOpened":
            params = data.get("params", {}) or {}
            session_id = data.get("sessionId")
            owner = session_id if isinstance(session_id, str) else None
            self._pending_choosers.append((owner, params))
            self._chooser_event.set()

    async def wait_for_chooser(
        self, timeout: float, *, session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the fileChooserOpened params, waiting up to ``timeout`` for
        one when nothing is pending yet; None on timeout.

        Covers both orders: the input was already clicked (pending chooser is
        returned immediately) or the click will come after this call. When a
        session is supplied, only that tab's chooser can be consumed; events
        from another tab remain queued for their owner.
        """
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            for index, (owner, chooser) in enumerate(self._pending_choosers):
                if session_id is not None and owner != session_id:
                    continue
                del self._pending_choosers[index]
                if not self._pending_choosers:
                    self._chooser_event.clear()
                # sessionId belongs to the event envelope, not to the Juggler
                # params. Keep it private so upload_files can route the
                # chooser to the page that actually opened it.
                if owner is not None:
                    return {**chooser, "_kahinSessionId": owner}
                return chooser
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            self._chooser_event.clear()
            try:
                await asyncio.wait_for(self._chooser_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    def _track_screencast(self, data: dict[str, Any]) -> None:
        """Queue Page.screencastFrame params (Gap D live screencast).

        The frame's ``data`` is a base64-encoded JPEG (Camoufox encodes
        JPEG, not PNG — nsScreencastService.cpp). Frames are queued BEFORE
        the client acks them; camoufox's kMaxFramesInFlight=1 stalls the
        stream when an ack is missing, so every consumed frame must be
        ack'd (the screencast_frame tool does this via
        Page.screencastFrameAck). The event params carry no screencastId —
        the stream id is remembered from Page.startScreencast.
        """
        if data.get("method") == "Page.screencastFrame":
            params = data.get("params", {}) or {}
            event_session = data.get("sessionId")
            expected_session = self._screencast_session_id or self._screencast_starting_session_id
            # Accept old sidecars that omitted the event envelope's sessionId,
            # but never mix frames from two explicitly identified tabs.
            # Once a stream has an owning session, an ownerless event is not
            # safe to accept: it could have come from another tab after a
            # concurrent switch. The current sidecar carries sessionId on
            # forwarded Page events; fail closed if a future sidecar drops it.
            owned = expected_session is None or event_session == expected_session
            if params.get("data") and owned and (self._screencast_id is not None or self._screencast_starting):
                self._screencast_frames.append({
                    **params,
                    "_kahinScreencastGeneration": self._screencast_generation,
                })
                self._screencast_event.set()

    async def wait_for_screencast_frame(
        self,
        timeout: float,
        *,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Return the OLDEST pending screencastFrame params (base64-JPEG in
        ``data``), waiting up to ``timeout`` when nothing is queued yet;
        None on timeout. A frame stays queued until this consumes it — the
        caller must then Page.screencastFrameAck its stream id (mismatched
        ids are a no-op on Camoufox). The queue keeps up to 64 unacked
        frames, so a slow consumer sees the oldest one first.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            while self._screencast_frames:
                frame = self._screencast_frames.popleft()
                if generation is None or frame.get("_kahinScreencastGeneration") == generation:
                    if not self._screencast_frames:
                        self._screencast_event.clear()
                    return frame
            if generation is not None and generation != self._screencast_generation:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._screencast_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            # stop() wakes this event after clearing the queue. Do not turn
            # that liveness wake-up into a deque underflow or a fake frame.
            if not self._screencast_frames:
                if not self.is_alive():
                    return None
                if self._screencast_id is None and not self._screencast_starting:
                    return None
                self._screencast_event.clear()

    def drain_screencast_frames(self) -> int:
        """Discard queued frames before a caller requests a fresh capture.

        The caller must ACK the returned count before waiting again; frames
        are held by Camoufox until their acknowledgements arrive.
        """
        discarded = len(self._screencast_frames)
        self._screencast_frames.clear()
        self._screencast_event.clear()
        return discarded

    def screencast_pending(self) -> dict[str, Any]:
        """Unacked frame count + newest frame summary + stream id — stream
        health: pending > 0 while frames wait for their ack (Camoufox holds
        at kMaxFramesInFlight=1 unacked frames, so a long-lived non-zero
        count means the consumer stalled)."""
        last = self._screencast_frames[-1] if self._screencast_frames else None
        return {
            "pending": len(self._screencast_frames),
            "screencastId": self._screencast_id,
            "active": self._screencast_id is not None,
            "last": {
                "deviceWidth": last.get("deviceWidth"),
                "deviceHeight": last.get("deviceHeight"),
                "dataLength": len(last.get("data", "")),
            }
            if last
            else None,
        }

    def set_screencast_id(self, screencast_id: str) -> None:
        """Remember the stream id returned by Page.startScreencast so the
        frame tool can ack frames whose event carries no id."""
        self._screencast_id = screencast_id

    @property
    def screencast_session_id(self) -> str | None:
        """Session owning the active stream; used for cross-tab ACK/stop."""
        return self._screencast_session_id

    def clear_screencast(self, *, wake_waiters: bool = False) -> int:
        """Drop all queued frames + the stream id (used on stopScreencast so
        no stale frame survives the stream); returns the discarded count.
        A stop operation wakes consumers so they cannot wait for a frame from
        a stream that has already ended.
        """
        discarded = len(self._screencast_frames)
        self._screencast_frames.clear()
        self._screencast_generation += 1
        if wake_waiters:
            self._screencast_event.set()
        else:
            self._screencast_event.clear()
        self._screencast_id = None
        self._screencast_session_id = None
        self._screencast_starting_session_id = None
        return discarded

    @property
    def screencast_generation(self) -> int:
        """Monotonic stream lifetime token for concurrent frame consumers."""
        return self._screencast_generation

    def current_session_id(self) -> str | None:
        """Return the Juggler session owning the selected target."""
        return self._sessions.get(self._current_target or "")

    def current_browser_context_id(self) -> str | None:
        """Return the selected target's browser context, if it has one."""
        info = self._target_infos.get(self._current_target or "") or {}
        value = info.get("browserContextId")
        return value if isinstance(value, str) and value else None

    def frame_id_for_execution_context(self, session_id: str, context_id: Any) -> str | None:
        """Resolve a file-chooser execution context to its live frame id."""
        for frame_id, known_context in self._frame_contexts.get(session_id, {}).items():
            if str(known_context) == str(context_id):
                return frame_id
        return None

    def _track_session(self, data: dict[str, Any]) -> None:
        """Update the targetId -> sessionId map from Juggler target events."""
        method = data.get("method")
        params = data.get("params", {}) or {}
        if method == "Browser.attachedToTarget":
            target_info = params.get("targetInfo") or {}
            target_id = target_info.get("targetId") or params.get("targetId")
            session_id = params.get("sessionId") or data.get("sessionId")
            if target_id and session_id:
                # Re-attaching a target creates a new execution-session
                # lifetime. Never let contexts from either the old binding or
                # a reused session id resolve in the new document.
                old_session_id = self._sessions.get(target_id)
                if old_session_id:
                    self._frame_contexts.pop(old_session_id, None)
                self._frame_contexts.pop(session_id, None)
                self._sessions[target_id] = session_id
                self._target_infos[target_id] = dict(target_info)
        elif method == "Browser.detachedFromTarget":
            target_id = params.get("targetId")
            session_id = params.get("sessionId") or data.get("sessionId")
            # An ownerless detach cannot be safely associated with a target:
            # the target may already have been reattached with a new session.
            # Keeping the mapping is safer than deleting a live replacement.
            if not isinstance(session_id, str) or not session_id:
                return
            # A target can be reattached before the old detach event reaches
            # the reader. That old event must not delete the new session.
            if target_id and session_id:
                current_session = self._sessions.get(target_id)
                if current_session is not None and current_session != session_id:
                    return
            self._frame_contexts.pop(session_id, None)
            if not target_id:
                target_id = next(
                    (tid for tid, sid in self._sessions.items() if sid == session_id),
                    None,
                )
            if target_id:
                self._sessions.pop(target_id, None)
                self._target_infos.pop(target_id, None)
                if self._current_target == target_id:
                    self._current_target = next(iter(self._sessions), None)

    def _track_target_url(self, data: dict[str, Any]) -> None:
        """Keep CDP-shaped Target.getTargets URL data current."""
        if data.get("method") not in {"Page.navigationCommitted", "Page.sameDocumentNavigation"}:
            return
        session_id = data.get("sessionId")
        url = (data.get("params") or {}).get("url")
        if not isinstance(session_id, str) or not isinstance(url, str):
            return
        target_id = next(
            (tid for tid, sid in self._sessions.items() if sid == session_id),
            None,
        )
        if target_id is not None:
            self._target_infos.setdefault(target_id, {}).update({"url": url})

    async def call(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Send one Juggler ``Domain.method`` and await its id-matched reply.

        Page-scoped methods carry the current target's sessionId unless an
        explicit session_id is given (Browser.* methods stay on the root
        session). Dead engines raise immediately instead of hanging.
        """
        if self._dead:
            raise RuntimeError("Mirage is dead")
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Mirage not started")
        async with self._target_lock:
            sid = session_id
            if sid is None and not method.startswith("Browser."):
                sid = self._sessions.get(self._current_target or "")
        if method == "Page.setInterceptFileChooserDialog":
            # A new interception mode starts a new chooser lifetime. Clear a
            # chooser event from a previous page/action before sending the
            # real Juggler command; events emitted afterwards remain visible.
            self._pending_choosers.clear()
            self._chooser_event.clear()
        if method == "Page.startScreencast":
            # Clear the predecessor before the real start call. Frames emitted
            # by the new stream while its reply is in flight must remain queued.
            self.clear_screencast()
            self._screencast_starting = True
            self._screencast_starting_session_id = sid
        self._msg_id += 1
        request_id = self._msg_id
        msg: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        response_timeout = (
            _PAGE_NAVIGATE_RESPONSE_TIMEOUT
            if method == "Page.navigate"
            else _REQUEST_TIMEOUT
        )
        try:
            # Keep JSONL records intact when several MCP calls arrive at once;
            # pending replies remain fully concurrent behind this tiny write
            # critical section.
            async with self._write_lock:
                if self._process is None or self._process.stdin is None:
                    raise RuntimeError("Mirage not started")
                self._process.stdin.write((json.dumps(msg) + "\n").encode())
                try:
                    await asyncio.wait_for(self._process.stdin.drain(), timeout=_IPC_WRITE_TIMEOUT)
                except asyncio.TimeoutError as exc:
                    self._mark_dead("stdin_write_timeout")
                    raise RuntimeError(
                        f"Mirage: stdin write timeout ({_IPC_WRITE_TIMEOUT:.1f}s); sidecar is not draining"
                    ) from exc
            result = await asyncio.wait_for(fut, timeout=response_timeout)
            if method == "Page.startScreencast":
                screencast_id = result.get("screencastId")
                if isinstance(screencast_id, str) and screencast_id:
                    self.set_screencast_id(screencast_id)
                    self._screencast_session_id = sid
                self._screencast_starting = False
                self._screencast_starting_session_id = None
            return result
        except TimeoutError:
            if method == "Page.startScreencast":
                self._screencast_starting = False
                self._screencast_starting_session_id = None
            self._pending.pop(request_id, None)
            raise RuntimeError(f"Mirage: response timeout ({response_timeout:.1f}s) for {method}")
        except asyncio.CancelledError:
            if method == "Page.startScreencast":
                self._screencast_starting = False
                self._screencast_starting_session_id = None
            self._pending.pop(request_id, None)
            raise
        except Exception:
            if method == "Page.startScreencast":
                self._screencast_starting = False
                self._screencast_starting_session_id = None
            self._pending.pop(request_id, None)
            raise

    async def get_response_body(
        self, request_id: str, session_id: str | None = None,
    ) -> dict[str, Any]:
        """Read a response body with a bounded completion-race retry.

        Juggler may emit ``requestWillBeSent`` before the response body is
        queryable.  CDP callers commonly receive that request id from an
        event and ask for the body immediately, so a single native call can
        fail transiently even though the request is healthy.  Retry only
        native protocol errors and keep the total wait bounded; transport or
        liveness failures still fail immediately.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESPONSE_BODY_RETRY_TIMEOUT
        while True:
            try:
                return await self.call(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                    session_id=session_id,
                )
            except RuntimeError as exc:
                if "cdp error:" not in str(exc).lower():
                    raise
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(_RESPONSE_BODY_RETRY_INTERVAL, remaining))

    # --- session management (Juggler target model) ---

    async def create_page(self, url: str = "about:blank", browser_context_id: str | None = None) -> dict[str, Any]:
        """Open a new tab; returns {"targetId", "sessionId"} and makes it current.

        The sessionId is learned from the Browser.attachedToTarget event the
        sidecar forwards right after Browser.newPage's reply.
        """
        # Juggler's Browser.newPage already creates an about:blank document;
        # sending a synthetic second Page.navigate to that same URL leaves a
        # fresh target with a competing navigation in flight. The next real
        # navigation can then be aborted or wait for the sidecar deadline.
        # Keep the default tab creation a pure attach, and only ask the
        # sidecar's combined flow to navigate when the caller supplied a real
        # destination.
        params: dict[str, Any] = {}
        if url and url != "about:blank":
            params["url"] = url
        if browser_context_id:
            params["browserContextId"] = browser_context_id
        result = await self.call("Browser.newPage", params)
        target_id = result.get("targetId")
        if not target_id:
            raise RuntimeError(f"Browser.newPage returned no targetId: {result}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(_REQUEST_TIMEOUT, 10.0)
        while target_id not in self._sessions:
            if loop.time() > deadline:
                raise RuntimeError(f"session for target {target_id} never attached")
            await asyncio.sleep(0.05)
        async with self._target_lock:
            if target_id not in self._sessions:
                raise RuntimeError(f"target {target_id} detached before it became current")
            self._current_target = target_id
            session_id = self._sessions[target_id]
        await self._wait_for_page_frame(session_id, deadline=min(deadline, loop.time() + 3.0))
        return {"targetId": target_id, "sessionId": session_id}

    async def _wait_for_page_frame(self, session_id: str, *, deadline: float) -> None:
        """Wait until the sidecar has registered a main frame for a target.

        ``Browser.attachedToTarget`` can precede the initial
        ``Page.frameAttached``/execution-context events. Returning a new tab
        in that gap makes an immediate Page.navigate race the target's
        about:blank lifecycle and can leave the Juggler navigation promise
        pending for its full 30-second deadline.
        """
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            try:
                tree = await asyncio.wait_for(
                    self.call("Page.getFrameTree", session_id=session_id),
                    timeout=min(0.5, max(0.05, deadline - loop.time())),
                )
            except Exception:
                tree = None
            frame_tree = tree.get("frameTree") if isinstance(tree, dict) else None
            frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
            if isinstance(frame, dict) and isinstance(frame.get("id"), str) and frame["id"]:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("new page frame did not become ready before the bounded deadline")

    async def ensure_page(self) -> dict[str, Any]:
        """Return the current tab, creating one lazily inside this browser.

        Browser startup intentionally does not create a second process or an
        eager throw-away page. The first page-oriented operation gets one
        about:blank tab, and subsequent operations reuse it until the caller
        explicitly asks for another tab.
        """
        async with self._page_lock:
            if self._current_target in self._sessions:
                return {
                    "targetId": self._current_target,
                    "sessionId": self._sessions[self._current_target],
                }
            if self._sessions:
                async with self._target_lock:
                    self._current_target = next(iter(self._sessions))
                    target_id = self._current_target
                return {"targetId": target_id, "sessionId": self._sessions[target_id]}
            adopted = await self._adopt_existing_window()
            if adopted is not None:
                return adopted
            return await self.create_page("about:blank")

    async def _adopt_existing_window(self) -> dict[str, Any] | None:
        """Adopt the window Camoufox already opened at boot instead of opening
        a second one via Browser.newPage (Juggler newPage creates a fresh
        toplevel window, one tab per window).

        The boot window is not announced through attachedToTarget, so
        _sessions stays empty. Probe Target.getTargets and attach to the
        first page target ourselves so no second window is ever spawned.
        """
        for _ in range(10):
            try:
                result = await self.call("Target.getTargets", {})
            except Exception:
                result = None
            infos = (result or {}).get("targetInfos") or []
            if infos:
                break
            await asyncio.sleep(0.1)
        for info in infos:
            tid = info.get("targetId")
            if not isinstance(tid, str) or tid in self._sessions:
                continue
            info_type = info.get("type")
            if info_type not in (None, "page"):
                continue
            try:
                await self.call("Target.attachToTarget", {"targetId": tid})
            except Exception:
                continue
            sid = self._sessions.get(tid)
            if not sid:
                continue
            async with self._target_lock:
                self._current_target = tid
            return {"targetId": tid, "sessionId": sid}
        return None

    async def close_page(self, target_id: str) -> dict[str, Any]:
        """Close a tab (Page.close) and forget its Juggler session."""
        session_id = self._sessions.get(target_id)
        if session_id is None:
            raise RuntimeError(f"unknown target: {target_id}")
        await self.call("Page.close", session_id=session_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while target_id in self._sessions:
            if loop.time() > deadline:
                raise RuntimeError(f"target {target_id} did not detach after Page.close")
            await asyncio.sleep(0.05)
        async with self._target_lock:
            # The detach event owns removal. Never manufacture a successful
            # close by deleting a still-live session from Python state.
            if target_id in self._sessions:
                raise RuntimeError(f"target {target_id} did not detach after Page.close")
            if self._current_target == target_id:
                self._current_target = next(iter(self._sessions), None)
        return {"closed": target_id}

    async def switch_page(self, target_id: str) -> dict[str, Any]:
        """Route page-scoped calls to an existing tab."""
        async with self._target_lock:
            if target_id not in self._sessions:
                raise RuntimeError(f"unknown target: {target_id}")
            self._current_target = target_id
        return {"switched": target_id}

    async def list_pages(self) -> list[dict[str, Any]]:
        """All known tabs with their Juggler session ids."""
        return [
            {"targetId": tid, "sessionId": sid, "current": tid == self._current_target}
            for tid, sid in self._sessions.items()
        ]

    async def execute_cdp(
        self,
        domain: str,
        command: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a CDP-shaped request through the Mirage equivalent.

        Agents often know the Chrome/CDP surface better than the Juggler
        tool names. Keep that request surface working on Camoufox while
        routing the operation to the real Juggler method or tab primitive.
        The returned object stays CDP-shaped, so callers do not need to know
        that the implementation used Mirage.
        """
        p = dict(params or {})
        page_session_id = session_id
        if page_session_id is None and _needs_mirage_page_for_adapter(domain, command):
            page = await self.ensure_page()
            page_session_id = page.get("sessionId")
        method = f"{domain}.{command}"

        # These domains are event-driven in Juggler and do not need an
        # enable/disable handshake. Treat the CDP setup calls as successful
        # no-ops so a raw CDP client can keep its normal bootstrap sequence.
        if method in {
            "Page.enable",
            "Page.disable",
            "Runtime.enable",
            "Runtime.disable",
            "Network.enable",
            "Network.disable",
            "DOM.enable",
            "DOM.disable",
            "Accessibility.enable",
            "Accessibility.disable",
        }:
            return {}

        if domain == "Target":
            return await self._execute_cdp_target(command, p)
        if domain == "Input":
            return await self._execute_cdp_input(command, p, page_session_id)
        if domain == "Emulation":
            return await self._execute_cdp_emulation(command, p, page_session_id)
        if domain == "Network":
            return await self._execute_cdp_network(command, p, page_session_id)
        if domain == "Browser" and command == "getVersion":
            result = await self.call("Browser.getInfo", p)
            return {
                "protocolVersion": result.get("protocolVersion", ""),
                "product": result.get("product", result.get("userAgent", "")),
                "revision": result.get("revision", ""),
                "userAgent": result.get("userAgent", ""),
                "jsVersion": result.get("jsVersion", ""),
            }

        if domain == "Page" and command == "captureScreenshot":
            result = await self.call("Page.captureScreenshot", p, session_id=page_session_id)
            data = result.get("data")
            if not isinstance(data, str) or not data.strip():
                raise RuntimeError(f"Page.captureScreenshot returned no data: {result}")
            return {"data": data}
        if domain == "Page" and command == "getLayoutMetrics":
            return await self._execute_cdp_layout_metrics(page_session_id)
        if domain == "Page" and command == "stopLoading":
            await self.call(
                "Runtime.evaluate", {"expression": "window.stop()"}, session_id=page_session_id,
            )
            return {}
        if domain == "Page" and command == "stopScreencast":
            result = await self.call(
                "Page.stopScreencast", p, session_id=self.screencast_session_id or page_session_id,
            )
            result = dict(result)
            result["discardedFrames"] = self.clear_screencast(wake_waiters=True)
            return result
        if domain == "Page" and command == "close":
            target_id = self._current_target
            if target_id is None:
                raise RuntimeError("no current tab")
            await self.close_page(target_id)
            return {}

        # Page.navigate, Runtime.evaluate, frame tree, and the other
        # already CDP-shaped sidecar handlers remain direct Juggler calls.
        return await self.call(method, p, session_id=page_session_id)

    async def _execute_cdp_layout_metrics(self, session_id: str | None = None) -> dict[str, Any]:
        """Build the CDP layout-metrics shape from real page measurements.

        Juggler intentionally has no Page.getLayoutMetrics method.  The
        browser still exposes the underlying layout through the page's
        JavaScript environment, so the adapter measures it there instead of
        returning a fake hard-coded viewport or leaking the protocol gap to
        the agent.
        """
        result = await self.call("Runtime.evaluate", {
            "expression": """
                (() => {
                  const root = document.documentElement;
                  const body = document.body;
                  const width = Math.max(
                    window.innerWidth || 0,
                    root?.clientWidth || 0,
                    body?.clientWidth || 0,
                  );
                  const height = Math.max(
                    window.innerHeight || 0,
                    root?.clientHeight || 0,
                    body?.clientHeight || 0,
                  );
                  return {
                    viewportWidth: width,
                    viewportHeight: height,
                    contentWidth: Math.max(
                      root?.scrollWidth || 0,
                      body?.scrollWidth || 0,
                      width,
                    ),
                    contentHeight: Math.max(
                      root?.scrollHeight || 0,
                      body?.scrollHeight || 0,
                      height,
                    ),
                    pageX: window.scrollX || 0,
                    pageY: window.scrollY || 0,
                    deviceScaleFactor: window.devicePixelRatio || 1,
                  };
                })()
            """,
            "returnByValue": True,
        }, session_id=session_id)
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError(f"could not measure page layout: {result}")
        width = float(value.get("viewportWidth", 0) or 0)
        height = float(value.get("viewportHeight", 0) or 0)
        content_width = float(value.get("contentWidth", width) or width)
        content_height = float(value.get("contentHeight", height) or height)
        page_x = float(value.get("pageX", 0) or 0)
        page_y = float(value.get("pageY", 0) or 0)
        return {
            "contentSize": {"x": 0, "y": 0, "width": content_width, "height": content_height},
            "layoutViewport": {
                "pageX": page_x,
                "pageY": page_y,
                "clientWidth": width,
                "clientHeight": height,
            },
            "visualViewport": {
                "offsetX": 0,
                "offsetY": 0,
                "pageX": page_x,
                "pageY": page_y,
                "clientWidth": width,
                "clientHeight": height,
                "scale": 1,
                "zoom": 1,
            },
        }

    async def _execute_cdp_target(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "getTargets":
            infos: list[dict[str, Any]] = []
            for target_id in self._sessions:
                info = dict(self._target_infos.get(target_id, {}))
                info.update({
                    "targetId": target_id,
                    "type": info.get("type", "page"),
                    "url": info.get("url", ""),
                })
                infos.append({
                    **{
                        key: info[key]
                        for key in ("targetId", "type", "browserContextId", "url")
                        if key in info and info[key] is not None
                    },
                    "sessionId": self._sessions[target_id],
                })
            return {"targetInfos": infos}
        if command == "createTarget":
            requested_url = str(params.get("url", "about:blank"))
            page = await self.create_page(
                url="about:blank",
                browser_context_id=params.get("browserContextId"),
            )
            if requested_url != "about:blank":
                # Keep Target.createTarget's URL behavior on the same
                # frame-ready navigation path as the native tab tool.
                from kahin.tools.pilot import navigate as pilot_navigate  # noqa: PLC0415

                raw_navigation = await pilot_navigate(url=requested_url, wait_until="load")
                try:
                    navigation = orjson.loads(raw_navigation)
                except orjson.JSONDecodeError:
                    navigation = {"error": raw_navigation[:1_000]}
                if isinstance(navigation, dict) and navigation.get("error"):
                    raise RuntimeError(f"Target.createTarget navigation failed: {navigation}")
                # A bounded navigation recovery may replace a wedged target
                # while keeping the same browser. Return the live target, not
                # the attach primitive that was closed during recovery.
                page = await self.ensure_page()
            return {"targetId": page["targetId"]}
        if command == "closeTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str) or not target_id:
                raise RuntimeError("Target.closeTarget requires targetId")
            await self.close_page(target_id)
            return {}
        if command == "activateTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str):
                raise RuntimeError("Target.activateTarget requires targetId")
            await self.switch_page(target_id)
            await self.call("Page.bringToFront")
            return {}
        if command == "attachToTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str):
                raise RuntimeError("Target.attachToTarget requires targetId")
            await self.switch_page(target_id)
            return {"sessionId": self._sessions[target_id]}
        if command in {"detachFromTarget", "setAutoAttach"}:
            raise RuntimeError(
                f"Target.{command} is unsupported on Mirage: the Juggler session model "
                "has no Target-domain detach/auto-attach operation; session state is "
                "managed by Browser.attachedToTarget and Browser.detachedFromTarget events"
            )
        if command == "disposeBrowserContext":
            context_id = params.get("browserContextId")
            if not isinstance(context_id, str):
                raise RuntimeError("Target.disposeBrowserContext requires browserContextId")
            return await self.call("Browser.removeBrowserContext", {"browserContextId": context_id})
        return await self.call(f"Target.{command}", params)

    async def _execute_cdp_input(
        self, command: str, params: dict[str, Any], session_id: str | None = None,
    ) -> dict[str, Any]:
        if command == "insertText":
            text = params.get("text", "")
            if not isinstance(text, str):
                raise RuntimeError("Input.insertText text must be a string")
            await self.call("Page.insertText", {"text": text}, session_id=session_id)
            return {}
        if command == "dispatchKeyEvent":
            type_map = {
                "keyDown": "keydown",
                "keyUp": "keyup",
                "rawKeyDown": "rawkeydown",
                "char": "char",
            }
            key_type = type_map.get(params.get("type"))
            if key_type is None:
                raise RuntimeError(f"unknown Input.dispatchKeyEvent type: {params.get('type')}")
            mapped: dict[str, Any] = {
                "type": key_type,
                "key": str(params.get("key", "")),
                "keyCode": int(params.get("windowsVirtualKeyCode", params.get("keyCode", 0)) or 0),
                "location": int(params.get("location", 0) or 0),
                "code": str(params.get("code", "Unidentified")),
                "repeat": bool(params.get("autoRepeat", params.get("repeat", False))),
            }
            if params.get("text") is not None:
                mapped["text"] = params["text"]
            await self.call("Page.dispatchKeyEvent", mapped, session_id=session_id)
            return {}
        if command == "dispatchMouseEvent":
            event_type = params.get("type")
            x = float(params.get("x", 0) or 0)
            y = float(params.get("y", 0) or 0)
            modifiers = int(params.get("modifiers", 0) or 0)
            if event_type == "mouseWheel":
                await self.call("Page.dispatchWheelEvent", {
                    "x": x,
                    "y": y,
                    "deltaX": float(params.get("deltaX", 0) or 0),
                    "deltaY": float(params.get("deltaY", 0) or 0),
                    "deltaZ": 0.0,
                    "modifiers": modifiers,
                }, session_id=session_id)
                return {}
            type_map = {"mousePressed": "mousedown", "mouseReleased": "mouseup", "mouseMoved": "mousemove"}
            juggler_type = type_map.get(event_type)
            if juggler_type is None:
                raise RuntimeError(f"unknown Input.dispatchMouseEvent type: {event_type}")
            button_name = str(params.get("button", "none"))
            button_number = {"left": 0, "middle": 1, "right": 2, "back": 3, "forward": 4, "none": 0}.get(button_name)
            if button_number is None:
                raise RuntimeError(f"unknown mouse button: {button_name}")
            buttons = params.get("buttons")
            if buttons is None:
                buttons = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}.get(button_name, 0)
                if juggler_type != "mousedown":
                    buttons = 0
            await self.call("Page.dispatchMouseEvent", {
                "type": juggler_type,
                "button": button_number,
                "x": x,
                "y": y,
                "modifiers": modifiers,
                "clickCount": int(params.get("clickCount", 1) or 1),
                "buttons": int(buttons),
            }, session_id=session_id)
            return {}
        return await self.call(f"Input.{command}", params, session_id=session_id)

    async def _execute_cdp_emulation(
        self, command: str, params: dict[str, Any], session_id: str | None = None,
    ) -> dict[str, Any]:
        if command == "setDeviceMetricsOverride":
            width = params.get("width")
            height = params.get("height")
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, (int, float))
                or not isinstance(height, (int, float))
                or width <= 0
                or height <= 0
            ):
                raise RuntimeError("Emulation.setDeviceMetricsOverride requires width and height")
            if params.get("mobile") is True or params.get("screenOrientation") is not None:
                raise RuntimeError(
                    "Mirage cannot emulate mobile UA/orientation through this CDP adapter; "
                    "use kahin_mirage_set_viewport and kahin_mirage_set_touch explicitly"
                )
            viewport: dict[str, Any] = {
                "viewportSize": {"width": width, "height": height},
            }
            if params.get("deviceScaleFactor") is not None:
                dsf = params["deviceScaleFactor"]
                if isinstance(dsf, bool) or not isinstance(dsf, (int, float)) or dsf <= 0:
                    raise RuntimeError("deviceScaleFactor must be a positive number")
                viewport["deviceScaleFactor"] = params["deviceScaleFactor"]
            context_params: dict[str, Any] = {"viewport": viewport}
            context_id = self.current_browser_context_id()
            if context_id:
                context_params["browserContextId"] = context_id
            await self.call("Browser.setDefaultViewport", context_params)
            return {}
        if command == "setUserAgentOverride":
            user_agent = params.get("userAgent", "")
            if not isinstance(user_agent, str):
                raise RuntimeError("Emulation.setUserAgentOverride userAgent must be a string")
            mapped = {"userAgent": user_agent}
            context_id = self.current_browser_context_id()
            if context_id:
                mapped["browserContextId"] = context_id
            return await self.call("Browser.setUserAgentOverride", mapped)
        if command == "setTouchEmulationEnabled":
            enabled = params.get("enabled", False)
            if not isinstance(enabled, bool):
                raise RuntimeError("Emulation.setTouchEmulationEnabled enabled must be a boolean")
            mapped = {"hasTouch": enabled}
            context_id = self.current_browser_context_id()
            if context_id:
                mapped["browserContextId"] = context_id
            return await self.call("Browser.setTouchOverride", mapped)
        if command == "setEmulatedMedia":
            return await self.call(
                "Page.setEmulatedMedia", {"type": params.get("media", "screen")}, session_id=session_id,
            )
        if command == "setLocaleOverride":
            locale = params.get("locale", "")
            if not isinstance(locale, str):
                raise RuntimeError("Emulation.setLocaleOverride locale must be a string")
            mapped = {"locale": locale}
            context_id = self.current_browser_context_id()
            if context_id:
                mapped["browserContextId"] = context_id
            return await self.call("Browser.setLocaleOverride", mapped)
        if command == "setTimezoneOverride":
            timezone = params.get("timezoneId", "")
            if not isinstance(timezone, str):
                raise RuntimeError("Emulation.setTimezoneOverride timezoneId must be a string")
            mapped = {"timezoneId": timezone}
            context_id = self.current_browser_context_id()
            if context_id:
                mapped["browserContextId"] = context_id
            return await self.call("Browser.setTimezoneOverride", mapped)
        if command == "setGeolocationOverride":
            geo = {k: params[k] for k in ("latitude", "longitude", "accuracy") if k in params}
            mapped: dict[str, Any] = {"geolocation": geo or None}
            context_id = self.current_browser_context_id()
            if context_id:
                mapped["browserContextId"] = context_id
            return await self.call("Browser.setGeolocationOverride", mapped)
        return await self.call(f"Emulation.{command}", params, session_id=session_id)

    async def _execute_cdp_network(
        self, command: str, params: dict[str, Any], session_id: str | None = None,
    ) -> dict[str, Any]:
        if command == "continueInterceptedRequest":
            request_id = params.get("requestId")
            if not isinstance(request_id, str) or not request_id:
                raise RuntimeError("Network.continueInterceptedRequest requires a non-empty requestId")
            mapped: dict[str, Any] = {"requestId": request_id}
            for key in ("url", "method", "postData"):
                if key in params:
                    if not isinstance(params[key], str):
                        raise RuntimeError(f"Network.continueInterceptedRequest {key} must be a string")
                    mapped[key] = params[key]
            if "headers" in params:
                headers = params["headers"]
                if isinstance(headers, dict):
                    if not all(
                        isinstance(name, str) and isinstance(value, str)
                        for name, value in headers.items()
                    ):
                        raise RuntimeError(
                            "Network.continueInterceptedRequest header names and values must be strings"
                        )
                    mapped["headers"] = [
                        {"name": name, "value": value}
                        for name, value in headers.items()
                    ]
                elif isinstance(headers, list) and all(
                    isinstance(header, dict)
                    and isinstance(header.get("name"), str)
                    and isinstance(header.get("value"), str)
                    for header in headers
                ):
                    mapped["headers"] = headers
                else:
                    raise RuntimeError(
                        "Network.continueInterceptedRequest headers must be a CDP object "
                        "or a list of {name,value} objects"
                    )
            return await self.call("Network.resumeInterceptedRequest", mapped, session_id=session_id)
        if command == "setRequestInterception":
            enabled = params.get("enabled", True)
            if not isinstance(enabled, bool):
                raise RuntimeError("Network.setRequestInterception enabled must be a boolean")
            patterns = params.get("patterns")
            if patterns not in (None, []):
                raise RuntimeError(
                    "Mirage cannot apply CDP Network.setRequestInterception patterns: "
                    "Juggler exposes a page-wide toggle, so patterns are rejected instead of dropped"
                )
            return await self.call(
                "Network.setRequestInterception",
                {"enabled": enabled},
                session_id=session_id,
            )
        if command == "getResponseBody":
            request_id = params.get("requestId")
            if not isinstance(request_id, str) or not request_id:
                raise RuntimeError("Network.getResponseBody requires a non-empty requestId")
            result = await self.get_response_body(request_id, session_id=session_id)
            raw = result.get("base64body") if isinstance(result, dict) else None
            if not isinstance(raw, str):
                raise RuntimeError("Juggler Network.getResponseBody returned no base64body")
            max_chars = ((1024 * 1024 + 2) // 3) * 4
            if len(raw) > max_chars:
                raise RuntimeError("Network.getResponseBody exceeds the 1 MiB response limit")
            try:
                body_bytes = base64.b64decode(raw, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Juggler returned invalid base64body: {exc}") from exc
            try:
                body = body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body = raw
                base64_encoded = True
            else:
                base64_encoded = False
            response: dict[str, Any] = {"body": body, "base64Encoded": base64_encoded}
            if isinstance(result, dict) and "evicted" in result:
                response["evicted"] = result["evicted"]
            return response
        if command == "clearBrowserCookies":
            return await self.call("Browser.clearCookies", {})
        if command == "clearBrowserCache":
            return await self.call("Browser.clearCache", {})
        if command == "setCacheDisabled":
            disabled = params.get("cacheDisabled", False)
            if not isinstance(disabled, bool):
                raise RuntimeError("Network.setCacheDisabled cacheDisabled must be a boolean")
            return await self.call(
                "Page.setCacheDisabled", {"cacheDisabled": disabled}, session_id=session_id,
            )
        return await self.call(f"Network.{command}", params, session_id=session_id)

    # --- liveness ---

    async def send_cdp(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Removed in Faz 9 Task 3 — the Juggler wire is call() only."""
        raise RuntimeError(
            "Mirage.send_cdp was removed; use Mirage.call(method, params, session_id)"
        )

    async def health(self) -> dict[str, Any]:
        """Probe both the sidecar and its Camoufox child.

        ``is_alive()`` can only see the sidecar process.  Browser.health is
        answered by the sidecar's process manager and catches the stale-state
        case where Firefox has exited but the Python process has not reaped
        the sidecar yet.
        """
        if not self.is_alive():
            return {
                "alive": False,
                "state": "dead",
                "reason": self._death_reason or "sidecar_not_alive",
                "pid": self._process.pid if self._process is not None else None,
                "returncode": self._process.returncode if self._process is not None else None,
                "stderr_log": str(self._stderr_path) if self._stderr_path is not None else None,
            }
        try:
            result = await self.call("Browser.health")
        except Exception as exc:  # noqa: BLE001
            process_alive = (
                not self._dead
                and self._process is not None
                and self._process.returncode is None
            )
            if process_alive:
                # A health RPC error is not proof that the sidecar or
                # browser died. Keep the transport and process lock owned so
                # a transient busy sidecar cannot create a second browser.
                return {
                    "alive": False,
                    "state": "degraded",
                    "reason": "health_probe_failed",
                    "error": str(exc),
                    "pid": self._process.pid,
                    "returncode": self._process.returncode,
                    "stderr_log": str(self._stderr_path) if self._stderr_path is not None else None,
                }
            self._mark_dead(f"health_error:{type(exc).__name__}")
            return {
                "alive": False,
                "state": "dead",
                "error": str(exc),
                "reason": self._death_reason,
                "pid": self._process.pid if self._process is not None else None,
                "returncode": self._process.returncode if self._process is not None else None,
                "stderr_log": str(self._stderr_path) if self._stderr_path is not None else None,
            }
        if not result.get("alive"):
            self._mark_dead("browser_health_dead")
        return result

    def is_alive(self) -> bool:
        """Process up and the reader healthy (reader EOF marks death)."""
        return (
            not self._dead
            and self._process is not None
            and self._process.returncode is None
        )

    def on_death(self, callback: Callable[[], Any]) -> None:
        self._death_callbacks.append(callback)  # type: ignore[arg-type]

    async def screenshot(self, format: str = "png", full_page: bool = False) -> bytes:
        """Page.captureScreenshot passthrough. The sidecar translates
        full_page into a full-content clip (size measured via evaluate);
        otherwise the real viewport is captured."""
        await self.ensure_page()
        result = await self.call("Page.captureScreenshot", {"format": format, "fullPage": full_page})
        data = result.get("data")
        if not isinstance(data, str) or not data.strip():
            raise RuntimeError(f"captureScreenshot returned no data: {result}")
        return base64.b64decode(data)

    async def stop(self) -> None:
        self._stopping = True
        self._dom_signal.set()
        self._reset_capture_state(wake_waiters=True)
        self._identity_config = None
        self._identity_name = None
        self._proxy_url = None
        self._identity_hash = None
        self._prewarm_info = None
        self._launch_policy = None
        self._started_monotonic = None
        self._marionette_port = None
        self._passkey_mode = False
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            if reader is not asyncio.current_task():
                try:
                    await asyncio.wait_for(asyncio.shield(reader), timeout=_READER_SHUTDOWN_TIMEOUT)
                except TimeoutError:
                    # A callback or a broken pipe must not turn browser_stop
                    # into a 30-second MCP stall. The reader is already
                    # cancelled; process teardown below is authoritative.
                    reader.cancel()
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - shutdown must continue
                    logger.debug("Mirage reader failed during shutdown", exc_info=True)
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        proc, self._process = self._process, None
        if proc is None:
            self._sessions.clear()
            self._target_infos.clear()
            self._frame_contexts.clear()
            self._current_target = None
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
            self._remove_profile()
            return
        if proc.stdin:
            try:
                proc.stdin.close()  # stdin EOF -> sidecar stops the browser
            except Exception:  # noqa: BLE001
                pass
        wait_task = asyncio.create_task(proc.wait())
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=_SIDECAR_SHUTDOWN_TIMEOUT)
        except ProcessLookupError:
            # The sidecar may already have been reaped by its reader/death
            # path. Cleanup is idempotent; a dead child is already stopped.
            pass
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=_SIDECAR_KILL_TIMEOUT)
            except TimeoutError:
                # A sidecar stuck while its browser is dying must not hold an
                # MCP shutdown request forever. The kill was already sent;
                # cancel only the local waiter after the bounded reap window.
                wait_task.cancel()
                try:
                    await wait_task
                except BaseException:  # noqa: BLE001 - waiter is cancelled
                    pass
            except ProcessLookupError:
                pass
        except asyncio.CancelledError:
            # A cancelled MCP request must still reap the sidecar before the
            # cancellation escapes; otherwise the next tool sees a cleared
            # Python state with a live child underneath it.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.shield(wait_task)
            raise
        finally:
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
            self._sessions.clear()
            self._target_infos.clear()
            self._frame_contexts.clear()
            self._current_target = None
            self._remove_profile()

    def _remove_profile(self) -> None:
        profile, self._profile_dir = self._profile_dir, None
        persistent, self._persistent_profile = self._persistent_profile, False
        if profile is not None and not persistent:
            shutil.rmtree(profile, ignore_errors=True)
