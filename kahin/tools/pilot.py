"""pilot.py — PILOT tools: browser engine control (engine-agnostic).

Owns browser lifecycle (start/stop) plus the shared navigation, DOM and
CDP passthrough tools. Event collectors are registered from
``kahin.oracle`` (engine lifecycle glue lives there); this module pulls
them back in for ``browser_start``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.oracle import _on_cdp_event, _on_console_event, _on_engine_death, _on_network_event
from kahin.the_twins.capabilities import capabilities_for
from kahin.the_twins.mirage import Mirage, _profile_directory
from kahin.the_twins.shadow import Obscura
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _auto_learn,
    _get_schema,
    _healer_ref,
    _MAX_TOOL_PAYLOAD_BYTES,
    _require_mirage,
    _safe_cdp,
)

logger = logging.getLogger(__name__)

_ENGINE_START_TIMEOUT = 60.0
_ENGINE_STOP_TIMEOUT = 15.0
_ENGINE_HEALTH_TIMEOUT = 5.0
_MAX_SELECTOR_LENGTH = 16_384
_MAX_ATTRIBUTE_LENGTH = 1_024
_MAX_EXTRACT_LENGTH = 100_000
_MAX_EVALUATE_LENGTH = 1_000_000
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
_VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"
_MAX_OCR_INPUT_LENGTH = 50 * 1024 * 1024
_NAVIGATE_WAIT_UNTIL = ("commit", "domcontentloaded", "load", "networkidle")
_NAVIGATE_IDLE_QUIET = 0.5
_NAVIGATE_MAX_TIMEOUT = 120.0
_MAX_IDENTITY_PAYLOAD = 16 * 1024 * 1024
_MAX_PROFILE_DIR_LENGTH = 4_096


def _json_error(tool: str, message: str, code: str = "tool_error", **details: Any) -> str:
    payload: dict[str, Any] = {"error": message, "code": code, "tool": tool}
    payload.update(details)
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


def _js_literal(value: str) -> str:
    """Encode a Python string as a JavaScript string literal, never repr()."""
    return orjson.dumps(value).decode()


def _normalized_navigation_url(value: str) -> str:
    """Normalize browser URL serialization for lifecycle comparisons.

    Browsers serialize an origin URL such as ``https://example.com`` as
    ``https://example.com/`` and may retain a fragment that does not affect
    the document lifecycle.  Comparing those strings literally makes a
    successful Shadow navigation wait until its timeout even though the
    document is already complete.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    path = parts.path
    if parts.scheme.lower() in {"http", "https", "ws", "wss"} and not path:
        path = "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _navigation_urls_match(current_url: str, target_url: str) -> bool:
    """Return whether the live URL represents the requested navigation."""
    return _normalized_navigation_url(current_url) == _normalized_navigation_url(target_url)


def _validate_text(
    value: Any, *, tool: str, field: str, maximum: int, allow_none: bool = False,
) -> tuple[str | None, str | None]:
    if value is None and allow_none:
        return None, None
    if not isinstance(value, str):
        return None, _json_error(tool, f"{field} must be a string", "invalid_argument", field=field)
    if len(value) > maximum:
        return None, _json_error(
            tool,
            f"{field} exceeds the maximum length of {maximum}",
            "argument_too_large",
            field=field,
            maximum=maximum,
            received=len(value),
        )
    return value, None


def _normalize_evaluate_response(raw: Any, tool: str, *, nested_error: bool = False) -> str:
    """Turn evaluate failures into an error object without changing successes."""
    if isinstance(raw, str):
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return _json_error(tool, raw[:1_000], "tool_failed", raw_response=True)
    else:
        payload = raw

    if not isinstance(payload, dict):
        return _json_error(tool, "Browser returned an invalid evaluate response", "invalid_engine_response")
    if payload.get("exceptionDetails"):
        return _json_error(
            tool,
            "JavaScript evaluation failed",
            "javascript_error",
            exception=payload["exceptionDetails"],
        )
    if nested_error:
        result = payload.get("result")
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, dict) and value.get("error"):
            return _json_error(
                tool,
                str(value["error"]),
                "dom_error",
            )
    return raw if isinstance(raw, str) else orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


async def _stop_engine(engine: Any, *, suppress: bool = True) -> None:
    """Best-effort bounded cleanup used on every failed lifecycle path."""
    try:
        await asyncio.wait_for(engine.stop(), timeout=_ENGINE_STOP_TIMEOUT)
    except asyncio.CancelledError:
        # Cancellation must reach the caller, but an interrupted MCP request
        # must not leave a browser child behind for the next request.
        try:
            await asyncio.shield(asyncio.wait_for(engine.stop(), timeout=_ENGINE_STOP_TIMEOUT))
        except BaseException:  # noqa: BLE001 - cleanup is already best effort
            logger.exception("browser cleanup failed after cancellation for %s", type(engine).__name__)
        raise
    except Exception:  # noqa: BLE001 - cleanup must not mask the start error
        logger.exception("browser cleanup failed for %s", type(engine).__name__)
        if not suppress:
            raise


async def _stop_mirage_page_loading(engine: Mirage) -> bool:
    """Stop the selected Mirage document before a replacement navigation.

    This is deliberately an internal, session-pinned operation. Calling the
    public ``kahin_mirage_stop`` function from inside ``kahin_navigate`` made
    recovery errors indistinguishable from a successful stop and introduced a
    second tool/healer layer around the same page call. The navigation path
    needs the real result from the exact target it is about to replace.
    """
    try:
        page = await asyncio.wait_for(engine.ensure_page(), timeout=1.0)
        session_id = page.get("sessionId") if isinstance(page, dict) else None
        if not isinstance(session_id, str) or not session_id:
            return False
        result = await asyncio.wait_for(
            engine.call(
                "Runtime.evaluate",
                {
                    "expression": "(() => {"
                    "const s = window[Symbol.for('kahin.dom.stream.v1')];"
                    "const root = document.documentElement;"
                    "const heavy = !!root && ("
                    "document.getElementsByTagName('*').length > 1000 || "
                    "root.scrollHeight > 100000"
                    ");"
                    "if (!((s && s.active) || heavy)) return false;"
                    "if (s && s.active) s.stop();"
                    "window.stop();"
                    "return true;"
                    "})()",
                    "returnByValue": True,
                },
                session_id=session_id,
            ),
            timeout=1.5,
        )
        value = (result.get("result") or {}).get("value") if isinstance(result, dict) else None
        if value is True:
            return True
        if value is False:
            return False
        logger.warning("Mirage pre-navigation stop returned no true value: %s", result)
    except Exception:  # noqa: BLE001 - navigation remains the authoritative operation
        logger.warning("Mirage pre-navigation stop failed", exc_info=True)
    return False


async def _recover_mirage_navigation_target(engine: Mirage) -> bool:
    """Replace one wedged page target without replacing the browser engine."""
    old_target = engine._current_target
    if old_target:
        try:
            await asyncio.wait_for(engine.close_page(old_target), timeout=2.5)
        except Exception:  # noqa: BLE001 - a wedged target may refuse close
            logger.warning("Mirage target close failed during navigation recovery", exc_info=True)
    try:
        await asyncio.wait_for(engine.create_page(url="about:blank"), timeout=5.0)
    except Exception:
        logger.warning("Mirage could not create a replacement target during navigation recovery", exc_info=True)
        return False
    # If close raced the target detach, the old target may still be present.
    # Try one bounded cleanup after the replacement is live; failure is
    # reported through the retained tab list rather than killing the browser.
    if old_target and old_target in engine._sessions and old_target != engine._current_target:
        try:
            await asyncio.wait_for(engine.close_page(old_target), timeout=2.5)
        except Exception:
            logger.warning("stale Mirage target remained after navigation recovery", exc_info=True)
    return True


async def _engine_is_healthy(engine: Any) -> bool:
    """Check the browser child, not only the Python/sidecar process."""
    if isinstance(engine, Mirage):
        try:
            result = await asyncio.wait_for(engine.health(), timeout=_ENGINE_HEALTH_TIMEOUT)
        except asyncio.TimeoutError:
            # A transient health timeout must not drop the process lock or
            # trigger a second browser. Reuse remains safe while the sidecar
            # transport is alive; explicit stop/start is the recovery path
            # if the next operation also fails.
            return bool(engine.is_alive())
        except Exception:  # noqa: BLE001
            return False
        if result.get("state") == "degraded":
            # A failed Browser.health RPC is not proof that Firefox died.
            # Keep the existing process/tab context reusable; treating this
            # as dead makes idempotent browser_start erase agent context.
            return bool(engine.is_alive())
        return bool(result.get("alive"))
    return bool(engine.is_alive())


def _engine_config_conflict(
    engine: Any,
    *,
    proxy: str | None,
    identity_config: dict[str, Any] | None,
    identity_name: str | None,
) -> dict[str, Any] | None:
    """A healthy engine reuse must never silently ignore a requested
    identity/proxy that differs from the active configuration. Returns a
    structured conflict payload (credentials redacted) or None when reuse
    is safe. Only meaningful for Mirage, which records identity/proxy
    metadata; Shadow applies neither."""
    from kahin.stealth import _redact_proxy  # noqa: PLC0415 - pure helper

    active_proxy = getattr(engine, "_proxy_url", None)
    active_identity_name = getattr(engine, "_identity_name", None)
    active_identity_config = getattr(engine, "_identity_config", None)
    requested: dict[str, Any] = {}
    active: dict[str, Any] = {}
    if proxy is not None and proxy != active_proxy:
        requested["proxy"] = _redact_proxy(proxy)
        active["proxy"] = _redact_proxy(active_proxy) if active_proxy else None
    if identity_name is not None and identity_name != active_identity_name:
        requested["identity"] = identity_name
        active["identity"] = active_identity_name or None
    elif identity_config is not None and identity_config != active_identity_config:
        requested["identity"] = identity_name or "inline config"
        active["identity"] = active_identity_name or "inline config"
    if not requested:
        return None
    return {
        "error": (
            "Engine already running with a different identity/proxy configuration; "
            "the requested configuration would be silently ignored."
        ),
        "hint": "Stop the engine with kahin_browser_stop, then start again with the requested identity/proxy.",
        "code": "engine_config_conflict",
        "requested": requested,
        "active": active,
    }


def _profile_config_conflict(
    engine: Any,
    *,
    persistent: bool,
    profile_dir: Path | None,
) -> dict[str, Any] | None:
    """Reject a reuse request that would silently choose another profile."""
    active_persistent = bool(getattr(engine, "_persistent_profile", False))
    active_path = getattr(engine, "_profile_dir", None)
    if active_persistent == persistent and (
        not persistent or active_path == profile_dir
    ):
        return None
    return {
        "error": "Engine already running with a different profile configuration.",
        "hint": "Stop the engine with kahin_browser_stop, then start again with the requested profile.",
        "code": "engine_config_conflict",
        "requested": {
            "persistent_profile": persistent,
            "profile_dir": str(profile_dir) if profile_dir is not None else None,
        },
        "active": {
            "persistent_profile": active_persistent,
            "profile_dir": str(active_path) if isinstance(active_path, Path) else None,
        },
    }
def _addons_start_summary(engine: Any) -> list[str]:
    addons = getattr(engine, "_addons", None)
    if isinstance(addons, list):
        return [str(item) for item in addons]
    return []


def _addons_config_conflict(
    engine: Any,
    *,
    addons: list[str] | None,
) -> dict[str, Any] | None:
    active_addons = getattr(engine, "_addons", []) or []
    requested_addons = addons or []
    if active_addons == requested_addons:
        return None
    return {
        "error": "Engine already running with a different addons configuration.",
        "hint": "Stop the engine with kahin_browser_stop, then start again with the requested addons.",
        "code": "engine_config_conflict",
        "requested": {"addons": requested_addons},
        "active": {"addons": active_addons},
    }


def _identity_start_summary(engine: Any) -> dict[str, Any] | None:
    """Expose bounded identity metadata without returning fingerprint data.

    Always carries the active 16-hex effective per-launch BrowserForge digest
    and the ``stealth`` launch policy
    actually bound on this start, whether or not an identity is configured.
    """
    if not isinstance(engine, Mirage):
        return None
    config = getattr(engine, "_identity_config", None)
    active_hash = getattr(engine, "_identity_hash", None)
    stealth = getattr(engine, "_launch_policy", None)
    return {
        "name": getattr(engine, "_identity_name", None),
        "hash": active_hash if isinstance(active_hash, str) else None,
        "configured": isinstance(config, dict) and bool(config),
        "stealth": stealth if isinstance(stealth, dict) and stealth else None,
    }


def _profile_start_summary(engine: Any) -> dict[str, Any]:
    path = getattr(engine, "_profile_dir", None)
    return {
        "persistent": bool(getattr(engine, "_persistent_profile", False)),
        "path": str(path) if isinstance(path, Path) else None,
    }


@mcp.tool(name="kahin_browser_start", annotations=_RW)
async def browser_start(
    engine: str = "mirage",
    headless: bool = True,
    port: int = 0,
    identity: str | dict[str, Any] | None = None,
    proxy: str | None = None,
    persistent_profile: bool = True,
    profile_dir: str | None = None,
    addons: list[str] | None = None,
) -> str:
    """Start or reuse one browser engine.

    Camoufox/Mirage is the default because it is the complete visual browser
    surface: screenshots, mobile viewport, input, accessibility and
    screencast all work in the same Kahin process. Shadow/Obscura remains an
    explicit fast CDP opt-in; a visual tool promotes it to Mirage in-process.
    Repeating a start for the active engine is idempotent; use
    ``kahin_mirage_tab_new`` for another task/page instead of booting another
    browser. ``identity`` pins a Camoufox fingerprint at launch: either a
    saved identity name (``kahin_identity_save``/``kahin_identity_new``) or
    an inline config dict. Identity applies to Mirage only, never Shadow.
    ``proxy`` routes the browser through a proxy URL (http/https/socks4/
    socks5) via its environment; it applies to Mirage only, never Shadow,
    and credentials are never echoed back. Reusing a healthy engine that
    runs a different identity/proxy is a conflict (``engine_config_conflict``),
    never a silent ignore — stop the engine first to change configuration.
    The summary always carries the active bounded ``identity.hash`` (fresh
    BrowserForge digest or the pinned identity's config hash) and the
    enabled ``identity.stealth`` launch policy, configured or not.
    By default the Mirage profile is persistent, so native cookies and web
    storage survive a clean browser restart. Set ``persistent_profile=False``
    for a disposable profile; ``profile_dir`` selects an explicit absolute
    persistent directory.
    """
    if not isinstance(engine, str):
        return _json_error("kahin_browser_start", "engine must be a string", "invalid_argument", field="engine")
    if not isinstance(headless, bool):
        return _json_error("kahin_browser_start", "headless must be a boolean", "invalid_argument", field="headless")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        return _json_error("kahin_browser_start", "port must be an integer between 0 and 65535", "invalid_argument", field="port")
    if port in (9222, 9240):
        return _json_error(
            "kahin_browser_start",
            f"Port {port} is RESERVED. Use a different port.",
            "reserved_port",
            field="port",
        )
    if not isinstance(persistent_profile, bool):
        return _json_error(
            "kahin_browser_start",
            "persistent_profile must be a boolean",
            "invalid_argument",
            field="persistent_profile",
        )
    if profile_dir is not None:
        profile_dir, profile_error = _validate_text(
            profile_dir,
            tool="kahin_browser_start",
            field="profile_dir",
            maximum=_MAX_PROFILE_DIR_LENGTH,
        )
        if profile_error:
            return profile_error
        if not persistent_profile:
            return _json_error(
                "kahin_browser_start",
                "profile_dir requires persistent_profile=true",
                "invalid_argument",
                field="profile_dir",
            )
        try:
            _profile_directory(True, profile_dir)
        except ValueError as exc:
            return _json_error(
                "kahin_browser_start",
                str(exc),
                "invalid_argument",
                field="profile_dir",
            )
    try:
        requested_profile, requested_persistent = _profile_directory(
            persistent_profile,
            profile_dir,
        )
    except ValueError as exc:
        return _json_error(
            "kahin_browser_start",
            str(exc),
            "invalid_argument",
            field="profile_dir",
        )
    if addons is not None:
        if not isinstance(addons, list):
            return _json_error(
                "kahin_browser_start",
                "addons must be a list of staged addon directory paths",
                "invalid_argument",
                field="addons",
            )
        if len(addons) > 16:
            return _json_error(
                "kahin_browser_start",
                "addons exceeds maximum 16 extensions",
                "invalid_argument",
                field="addons",
            )
        for idx, item in enumerate(addons):
            if not isinstance(item, str) or not item.strip():
                return _json_error(
                    "kahin_browser_start",
                    "addon item must be a non-empty string",
                    "invalid_argument",
                    field=f"addons[{idx}]",
                )
            if len(item) > 4096:
                return _json_error(
                    "kahin_browser_start",
                    "addon path exceeds 4096 characters",
                    "invalid_argument",
                    field=f"addons[{idx}]",
                )
            from kahin.extensions import inspect_extension
            try:
                report = inspect_extension(item)
                if not report.get("compatible"):
                    unsupported_str = ", ".join(report.get("unsupported", []))
                    return _json_error(
                        "kahin_browser_start",
                        f"Addon '{item}' is not compatible: {unsupported_str}",
                        "addon_incompatible",
                        field=f"addons[{idx}]",
                        unsupported=report.get("unsupported", []),
                    )
            except (OSError, ValueError) as exc:
                return _json_error(
                    "kahin_browser_start",
                    str(exc),
                    "invalid_argument",
                    field=f"addons[{idx}]",
                )
    if identity is not None and not isinstance(identity, (str, dict)):
        return _json_error(
            "kahin_browser_start",
            "identity must be a saved identity name (string) or an inline config object",
            "invalid_argument",
            field="identity",
        )
    if isinstance(identity, dict) and not identity:
        return _json_error(
            "kahin_browser_start",
            "identity config must be a non-empty object",
            "invalid_argument",
            field="identity",
        )
    proxy_value: str | None = None
    if proxy is not None:
        if not isinstance(proxy, str):
            return _json_error(
                "kahin_browser_start",
                "proxy must be a string proxy URL (http/https/socks4/socks5)",
                "invalid_argument",
                field="proxy",
            )
        stripped = proxy.strip()
        if stripped:
            from kahin.stealth import proxy_env  # noqa: PLC0415 - pure helper

            try:
                proxy_env(stripped)
            except ValueError as exc:
                return _json_error(
                    "kahin_browser_start",
                    str(exc),
                    "invalid_argument",
                    field="proxy",
                )
            proxy_value = stripped

    identity_config: dict[str, Any] | None = None
    identity_name: str | None = None
    if identity is not None:
        if isinstance(identity, dict):
            identity_config = identity
        else:
            from kahin.tools.agent_mirage import _identity_path  # noqa: PLC0415

            path = _identity_path(identity)
            if path is None or not path.is_file():
                return _json_error(
                    "kahin_browser_start",
                    f"unknown identity: {identity!r}",
                    "invalid_argument",
                    field="identity",
                )
            try:
                data = path.read_bytes()
            except OSError as exc:
                return _json_error(
                    "kahin_browser_start",
                    f"identity file unreadable: {exc}",
                    "invalid_argument",
                    field="identity",
                )
            if len(data) > _MAX_IDENTITY_PAYLOAD:
                return _json_error(
                    "kahin_browser_start",
                    "identity file exceeds the read payload bound",
                    "invalid_argument",
                    field="identity",
                    maximum=_MAX_IDENTITY_PAYLOAD,
                )
            try:
                payload = orjson.loads(data)
            except orjson.JSONDecodeError as exc:
                return _json_error(
                    "kahin_browser_start",
                    f"identity file is not valid JSON: {exc}",
                    "invalid_argument",
                    field="identity",
                )
            if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
                return _json_error(
                    "kahin_browser_start",
                    "identity file must contain a JSON object with a config object",
                    "invalid_argument",
                    field="identity",
                )
            if not payload["config"]:
                return _json_error(
                    "kahin_browser_start",
                    "identity file config must be a non-empty object",
                    "invalid_argument",
                    field="identity",
                )
            identity_config = payload["config"]
            identity_name = identity

    if engine not in ("shadow", "mirage", "camoufox"):
        return _json_error(
            "kahin_browser_start",
            f"Unknown engine: {engine}. Use 'shadow', 'mirage' or 'camoufox'.",
            "unknown_engine",
            field="engine",
        )

    async with state._lifecycle_lock:
        async with _healer_ref.safe("kahin_browser_start", engine=engine, headless=headless, port=port):
            current = state._current_engine
            if current is not None:
                if await _engine_is_healthy(current):
                    current_kind = "shadow" if isinstance(current, Obscura) else "mirage"
                    requested_kind = "shadow" if engine == "shadow" else "mirage"
                    if current_kind == requested_kind:
                        # Same browser, same process: callers may safely make
                        # start part of their setup without leaking a child.
                        # A requested identity/proxy that differs from the
                        # active configuration is a conflict, never silently
                        # ignored (Faz 3 Task 5).
                        if current_kind == "mirage":
                            profile_conflict = _profile_config_conflict(
                                current,
                                persistent=requested_persistent,
                                profile_dir=requested_profile,
                            )
                            if profile_conflict is not None:
                                return orjson.dumps(profile_conflict, option=orjson.OPT_INDENT_2).decode()
                            addons_conflict = _addons_config_conflict(
                                current,
                                addons=addons,
                            )
                            if addons_conflict is not None:
                                return orjson.dumps(addons_conflict, option=orjson.OPT_INDENT_2).decode()
                            conflict = _engine_config_conflict(
                                current,
                                proxy=proxy_value,
                                identity_config=identity_config,
                                identity_name=identity_name,
                            )
                            if conflict is not None:
                                return orjson.dumps(conflict, option=orjson.OPT_INDENT_2).decode()
                        if isinstance(current, Mirage):
                            try:
                                await current.ensure_page()
                            except Exception as exc:  # noqa: BLE001
                                return _json_error(
                                    "kahin_browser_start",
                                    f"Existing Mirage engine has no usable page: {exc}",
                                    "session_unavailable",
                                )
                        tabs = await current.list_pages() if isinstance(current, Mirage) else []
                        current_port = (
                            getattr(current, "port", None) if current_kind == "shadow" else 0
                        )
                        return orjson.dumps({
                            "status": "reused",
                            "engine": current_kind,
                            "capabilities": capabilities_for(current_kind),
                            "identity": _identity_start_summary(current),
                            "profile": _profile_start_summary(current),
                            "addons": _addons_start_summary(current),
                            "message": "Engine already running; reusing the existing browser and tabs.",
                            "port": current_port or 0,
                            "tabs": tabs,
                        }, option=orjson.OPT_INDENT_2).decode()
                    return orjson.dumps({
                        "error": f"Engine {current_kind} already running. Stop it before switching to {engine}.",
                        "hint": "Reuse the current engine or use its tab tools; no second browser was started.",
                    }, option=orjson.OPT_INDENT_2).decode()

                # The sidecar can outlive its Firefox child briefly. Probe
                # above catches that; now reap the stale process before any
                # replacement is allowed to start.
                logger.warning("replacing dead engine: %s", type(current).__name__)
                try:
                    await _stop_engine(current, suppress=False)
                except Exception as exc:
                    return _json_error(
                        "kahin_browser_start",
                        f"Could not reap the dead {type(current).__name__} engine: {exc}",
                        "engine_reap_failed",
                        hint="The old engine reference was retained; retry kahin_browser_stop before starting a replacement.",
                    )
                state._current_engine = None
                _healer_ref.bind_engine(None)
                state.clear_state()

            browser_lock_error = state.acquire_browser_lock()
            if browser_lock_error is not None:
                return _json_error(
                    "kahin_browser_start",
                    "Another Kahin MCP process owns the machine-wide browser slot; refusing to open a second browser.",
                    "engine_process_conflict",
                    hint="Reuse the owner MCP session or stop it before starting a new session.",
                    **browser_lock_error,
                )

            if engine == "shadow":
                candidate: Any = Obscura()
                actual_port = port
            else:
                candidate = Mirage(engine_name=engine)
                actual_port = 0  # Juggler pipe: no remote-debugging port

            try:
                if engine == "shadow":
                    start_kwargs: dict[str, Any] = {}
                else:
                    start_kwargs = {
                        "identity": identity_config,
                        "identity_name": identity_name,
                        "proxy": proxy_value,
                        "persistent_profile": persistent_profile,
                        "profile_dir": profile_dir,
                        "addons": addons,
                    }
                await asyncio.wait_for(
                    candidate.start(headless=headless, port=actual_port, **start_kwargs),
                    timeout=_ENGINE_START_TIMEOUT,
                )
                if engine == "shadow":
                    actual_port = candidate.port or actual_port
            except asyncio.TimeoutError:
                failed_port = getattr(candidate, "port", None) or actual_port
                await _stop_engine(candidate)
                state.release_browser_lock()
                _healer_ref.bind_engine(None)
                return _json_error(
                    "kahin_browser_start",
                    f"Engine {engine} failed to start on port {failed_port} (timeout after {_ENGINE_START_TIMEOUT:.0f}s)",
                    "engine_start_timeout",
                    engine=engine,
                    port=failed_port,
                )
            except asyncio.CancelledError:
                await _stop_engine(candidate)
                state.release_browser_lock()
                _healer_ref.bind_engine(None)
                raise
            except Exception as exc:
                await _stop_engine(candidate)
                state.release_browser_lock()
                _healer_ref.bind_engine(None)
                return _json_error(
                    "kahin_browser_start",
                    f"Engine {engine} failed to start: {exc}",
                    "engine_start_failed",
                    engine=engine,
                )

            # Publish only a fully booted engine. If registration or a later
            # callback fails, the same cleanup rule prevents an orphan.
            try:
                state._current_engine = candidate
                await candidate.on_event(_on_cdp_event)
                await candidate.on_event(_on_network_event)
                await candidate.on_event(_on_console_event)
                eng = candidate
                eng.on_death(lambda: _on_engine_death(eng))
                _healer_ref.bind_engine(candidate)
                if isinstance(candidate, Mirage):
                    # Publish a deterministic first tab during startup. This
                    # is still one browser process; it prevents the first
                    # navigate/status race from manufacturing a second page
                    # after the caller already believes startup completed.
                    await candidate.ensure_page()
                tabs = await candidate.list_pages() if isinstance(candidate, Mirage) else []
            except BaseException:
                state._current_engine = None
                await _stop_engine(candidate)
                state.release_browser_lock()
                _healer_ref.bind_engine(None)
                state.clear_state()
                raise

            return orjson.dumps({
                "status": "started",
                "engine": "mirage" if engine == "camoufox" else engine,
                "capabilities": capabilities_for("mirage" if engine == "camoufox" else engine),
                "identity": _identity_start_summary(candidate),
                "profile": _profile_start_summary(candidate),
                "addons": _addons_start_summary(candidate),
                "port": actual_port,
                "tabs": tabs,
                "hint": "Reuse this browser; for separate work create/switch a Mirage tab.",
            }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_browser_stop", annotations=_RW)
async def browser_stop() -> str:
    """Stop the active browser engine."""
    async with state._lifecycle_lock:
        if state._current_engine is None:
            _healer_ref.bind_engine(None)
            return _json_error(
                "kahin_browser_stop",
                "No engine running.",
                "engine_unavailable",
            )
        async with _healer_ref.safe("kahin_browser_stop"):
            engine = state._current_engine
            try:
                await _stop_engine(engine, suppress=False)
            except Exception as exc:
                return _json_error(
                    "kahin_browser_stop",
                    f"Browser cleanup failed: {exc}",
                    "engine_stop_failed",
                    hint="Retry kahin_browser_stop; the engine reference was retained for cleanup.",
                )
            state._current_engine = None
            _healer_ref.bind_engine(None)
            state.clear_state()
            state.release_browser_lock()
            return '{"status": "stopped"}'


async def _navigation_wait(
    tool: str,
    *,
    wait_until: str,
    timeout: float,
    target_url: str,
    previous_url: str | None,
    frame_id: str | None,
    session_id: str | None,
    started_at: float,
    event_start_seq: int,
) -> dict[str, Any] | None:
    """Wait for a bounded document lifecycle state after navigation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    expression = (
        "(() => { const rs = document.readyState; "
        "const resources = performance.getEntriesByType('resource').length; "
        "return {readyState: rs, resources, url: location.href}; })()"
    )
    engine = state._current_engine
    last_event_ts = 0.0
    while True:
        ready_state = ""
        current_url = ""
        if isinstance(engine, Mirage):
            from kahin.tools.pilot_mirage import _safe_mirage_eval_result  # noqa: PLC0415

            evaluated = await _safe_mirage_eval_result(
                tool, expression, frame_id, session_id=session_id,
            )
        else:
            evaluated_text = await _safe_cdp(
                "Runtime", "evaluate", {"expression": expression, "returnByValue": True},
            )
            try:
                evaluated = orjson.loads(evaluated_text)
            except orjson.JSONDecodeError:
                evaluated = {"error": "invalid_engine_response"}
        if isinstance(evaluated, dict):
            result = evaluated.get("result") or {}
            value = result.get("value") if isinstance(result, dict) else None
            if isinstance(value, dict):
                ready_state = str(value.get("readyState") or "")
                current_url = str(value.get("url") or "")

        now = time.time()
        committed = False
        history = list(state._current_event_log)
        for event in history:
            event_seq = event.get("seq")
            if not isinstance(event_seq, int) or event_seq <= event_start_seq:
                continue
            if event.get("session_id") != session_id:
                continue
            event_name = event.get("event")
            if event_name == "Page.navigationAborted":
                return {
                    "error": "navigation was aborted by the browser",
                    "code": "navigation_aborted",
                    "wait_until": wait_until,
                    "timeout": timeout,
                    "readyState": ready_state,
                }
            if event_name in {"Page.navigationCommitted", "Page.sameDocumentNavigation"}:
                committed = True

        url_ready = bool(
            committed
            or _navigation_urls_match(current_url, target_url)
            or (
                previous_url
                and current_url
                and not _navigation_urls_match(current_url, previous_url)
            )
        )
        if wait_until == "commit" and url_ready:
            return None
        if wait_until == "domcontentloaded" and url_ready and ready_state in {"interactive", "complete"}:
            return None
        if wait_until == "load" and url_ready and ready_state == "complete":
            return None
        if wait_until == "networkidle":
            events = state._network_requests
            for event in reversed(events):
                event_ts = float(event.get("timestamp") or 0.0)
                if event_ts >= started_at:
                    last_event_ts = event_ts
                    break
            if url_ready and ready_state == "complete" and last_event_ts and now - last_event_ts >= _NAVIGATE_IDLE_QUIET:
                return None
        if loop.time() >= deadline:
            return {
                "error": f"navigation did not reach {wait_until} within {timeout:g}s",
                "code": "navigation_timeout",
                "wait_until": wait_until,
                "timeout": timeout,
                "readyState": ready_state,
                "url": current_url,
            }
        await asyncio.sleep(0.1)


@mcp.tool(name="kahin_navigate", annotations=_RW)
async def navigate(
    url: str,
    wait_until: str = "load",
    timeout: float = 30.0,
    referer: str | None = None,
) -> str:
    """Navigate the current page and wait for a bounded lifecycle state.

    ``wait_until`` accepts ``commit``, ``domcontentloaded``, ``load`` or
    ``networkidle``. ``referer`` is forwarded when the active engine accepts
    it; the native adapter reports a structured unsupported error otherwise.
    """
    url_value, error = _validate_text(url, tool="kahin_navigate", field="url", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    assert url_value is not None
    if wait_until not in _NAVIGATE_WAIT_UNTIL:
        return _json_error(
            "kahin_navigate",
            f"wait_until must be one of {_NAVIGATE_WAIT_UNTIL}",
            "invalid_argument",
            field="wait_until",
            received=wait_until,
        )
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        timeout_value = 30.0
    if not isinstance(timeout_value, float) or not timeout_value == timeout_value or timeout_value in {float("inf"), float("-inf")}:
        timeout_value = 30.0
    timeout_value = max(0.0, min(_NAVIGATE_MAX_TIMEOUT, timeout_value))
    referer_value: str | None = None
    if referer is not None:
        referer_value, error = _validate_text(
            referer, tool="kahin_navigate", field="referer", maximum=_MAX_SELECTOR_LENGTH,
        )
        if error:
            return error
    async with _healer_ref.safe(
        "kahin_navigate", url=url_value[:80], wait_until=wait_until, timeout=timeout_value,
    ):
        params: dict[str, Any] = {"url": url_value}
        if referer_value:
            params["referer"] = referer_value
        # A previous large DOM/accessibility probe can leave the current
        # document's loader doing background work even after the agent has
        # decided to leave it. Firefox then occasionally keeps the native
        # Juggler Page.navigate gate open until its 30s deadline. Stopping the
        # document immediately before a new navigation is the browser-native
        # equivalent of a user clicking a new link; it does not create a tab
        # or a second browser and is bounded so it cannot delay navigation.
        engine = state._current_engine
        previous_url: str | None = None
        if isinstance(engine, Mirage):
            info = engine._target_infos.get(engine._current_target or "") or {}
            if isinstance(info.get("url"), str):
                previous_url = info["url"]
        if isinstance(engine, Mirage):
            if await _stop_mirage_page_loading(engine):
                # Give the sidecar reader one scheduling turn after the
                # successful stop so its document lifecycle state is settled
                # before the replacement Page.navigate is written.
                await asyncio.sleep(0.05)
        navigation_started_at = time.time()
        event_start_seq = state._event_seq
        target_recovered = False
        navigation_result = await _safe_cdp("Page", "navigate", params)
        try:
            parsed_result = orjson.loads(navigation_result)
        except orjson.JSONDecodeError:
            return navigation_result
        if isinstance(parsed_result, dict) and parsed_result.get("error") and isinstance(engine, Mirage):
            error_text = str(parsed_result.get("error") or "").lower()
            retryable_navigation_error = (
                parsed_result.get("code") == "cdp_command_failed"
                and any(token in error_text for token in ("timeout", "aborted", "navigate failed"))
            )
            if retryable_navigation_error:
                # A response timeout means this target is no longer a safe
                # navigation surface. Replace only the wedged target inside
                # the same Mirage browser/context; never start a second
                # browser as a timeout strategy. Abort errors get one
                # same-target retry first because the target may still be
                # healthy.
                if "timeout" in error_text:
                    target_recovered = await _recover_mirage_navigation_target(engine)
                    if not target_recovered:
                        return navigation_result
                    previous_url = None
                else:
                    await _stop_mirage_page_loading(engine)
                await asyncio.sleep(0.05)
                navigation_started_at = time.time()
                event_start_seq = state._event_seq
                navigation_result = await _safe_cdp("Page", "navigate", params)
                try:
                    parsed_result = orjson.loads(navigation_result)
                except orjson.JSONDecodeError:
                    return navigation_result
        if not isinstance(parsed_result, dict) or parsed_result.get("error"):
            return navigation_result
        await _auto_learn("Page", "navigate", {"url": url_value})

        frame_id: str | None = None
        session_id: str | None = None
        engine = state._current_engine
        if isinstance(engine, Mirage):
            try:
                page = await engine.ensure_page()
            except Exception as exc:  # noqa: BLE001 - structured tool response
                return _json_error("kahin_navigate", str(exc), "session_unavailable")
            if isinstance(page, dict):
                frame_id = page.get("frameId") if isinstance(page.get("frameId"), str) else None
                session_id = page.get("sessionId") if isinstance(page.get("sessionId"), str) else None
        wait_error = await _navigation_wait(
            "kahin_navigate",
            wait_until=wait_until,
            timeout=timeout_value,
            target_url=url_value,
            previous_url=previous_url,
            frame_id=frame_id,
            session_id=session_id,
            started_at=navigation_started_at,
            event_start_seq=event_start_seq,
        )
        if wait_error:
            return orjson.dumps(wait_error, option=orjson.OPT_INDENT_2).decode()
        parsed_result["wait_until"] = wait_until
        parsed_result["timeout"] = timeout_value
        if target_recovered:
            parsed_result["target_recovered"] = True
        return orjson.dumps(parsed_result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_click", annotations=_DW)
async def click(selector: str) -> str:
    """Click an element by CSS selector.

    Mirage uses its native coordinate/actionability path.  The JS click is
    retained only for an explicitly selected Shadow session, whose CDP
    surface has no native DOM-click primitive.
    """
    selector_value, error = _validate_text(
        selector, tool="kahin_click", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_click", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_click", selector=selector_value[:80]):
        try:
            if isinstance(state._current_engine, Mirage):
                # Lazy import avoids the pilot <-> pilot_mirage bootstrap
                # cycle while making the default Camoufox path real input.
                from kahin.tools.pilot_mirage import mirage_click  # noqa: PLC0415

                return await mirage_click(selector_value)
            await _auto_learn("Runtime", "click", {"selector": selector_value})
            expr = f"""(() => {{
                const el = document.querySelector({_js_literal(selector_value)});
                if (!el) return {{"error": "not found"}};
                el.scrollIntoView({{block: "center"}});
                el.click();
                return "clicked";
            }})()"""
            raw = await _safe_cdp("Runtime", "evaluate", {"expression": expr, "returnByValue": True})
            normalized = _normalize_evaluate_response(raw, "kahin_click", nested_error=True)
            try:
                parsed = orjson.loads(normalized)
            except orjson.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict) or not parsed.get("error"):
                await _auto_learn("Runtime", "click", {"selector": selector_value})
            return normalized
        except Exception as exc:  # noqa: BLE001 - MCP tools must return JSON errors
            return _json_error("kahin_click", f"Click failed: {exc}", "tool_failed")


@mcp.tool(name="kahin_extract", annotations=_RO)
async def extract(selector: str | None = None, attribute: str | None = None) -> str:
    """Extract text content or attribute from page/element."""
    selector_value, error = _validate_text(
        selector,
        tool="kahin_extract",
        field="selector",
        maximum=_MAX_SELECTOR_LENGTH,
        allow_none=True,
    )
    if error:
        return error
    attribute_value, error = _validate_text(
        attribute,
        tool="kahin_extract",
        field="attribute",
        maximum=_MAX_ATTRIBUTE_LENGTH,
        allow_none=True,
    )
    if error:
        return error

    async with _healer_ref.safe(
        "kahin_extract", selector=selector_value or "", attribute=attribute_value or "",
    ):
        try:
            if selector_value and attribute_value:
                expr = (
                    "(() => {"
                    f"const el = document.querySelector({_js_literal(selector_value)});"
                    "if (!el) return '';"
                    f"const name = {_js_literal(attribute_value)};"
                    "const type = String(el.getAttribute('type') || el.type || '').toLowerCase();"
                    "if (type === 'password' && name.toLowerCase() === 'value') return '[redacted]';"
                    f"return String(el.getAttribute(name) ?? '').slice(0, {_MAX_EXTRACT_LENGTH});"
                    "})()"
                )
            elif selector_value:
                expr = (
                    "(() => {"
                    f"const el = document.querySelector({_js_literal(selector_value)});"
                    f"return String(el?.textContent ?? '').trim().slice(0, {_MAX_EXTRACT_LENGTH});"
                    "})()"
                )
            elif attribute_value:
                expr = (
                    f"String(document.documentElement.getAttribute({_js_literal(attribute_value)}) ?? '')"
                    f".slice(0, {_MAX_EXTRACT_LENGTH})"
                )
            else:
                expr = f"String(document.body?.innerText ?? '').slice(0, {_MAX_EXTRACT_LENGTH})"
            raw = await _safe_cdp("Runtime", "evaluate", {"expression": expr, "returnByValue": True})
            return _normalize_evaluate_response(raw, "kahin_extract")
        except Exception as exc:  # noqa: BLE001 - MCP tools must return JSON errors
            return _json_error("kahin_extract", f"Extraction failed: {exc}", "tool_failed")


@mcp.tool(name="kahin_screenshot", annotations=_RO)
async def screenshot(full_page: bool = False) -> str:
    """Capture a screenshot through Camoufox, promoting Shadow if needed."""
    if not isinstance(full_page, bool):
        return _json_error(
            "kahin_screenshot", "full_page must be a boolean", "invalid_argument", field="full_page",
        )
    try:
        err = await _require_mirage()
        if err:
            return err
        async with _healer_ref.safe("kahin_screenshot", full_page=full_page):
            engine = state._current_engine
            if engine is None:
                return _json_error("kahin_screenshot", "No browser engine is running", "engine_unavailable")
            data = await engine.screenshot(full_page=full_page)
            if not isinstance(data, (bytes, bytearray)) or not data:
                return _json_error(
                    "kahin_screenshot", "Browser returned no screenshot data", "invalid_engine_response",
                )
            if len(data) > _MAX_SCREENSHOT_BYTES:
                return _json_error(
                    "kahin_screenshot",
                    "Screenshot exceeds Kahin's bounded response size",
                    "result_too_large",
                    payloadBytes=len(data),
                    maxPayloadBytes=_MAX_SCREENSHOT_BYTES,
                    hint="Use the mobile viewport or a non-full-page screenshot.",
                )
            from pathlib import Path as _P
            import time as _t
            out_dir = _P("screenshots")
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"screenshot-{int(_t.time()*1000)}.png"
            fpath = out_dir / fname
            fpath.write_bytes(bytes(data))
            return orjson.dumps({"path": str(fpath.resolve()), "format": "png", "bytes": len(data)}, option=orjson.OPT_INDENT_2).decode()
    except Exception as exc:  # noqa: BLE001 - visual tools must never leak exceptions
        return _json_error("kahin_screenshot", f"Screenshot failed: {exc}", "tool_failed")


def _read_ocr_image(value: str) -> bytes:
    if value.startswith("data:"):
        try:
            _, encoded = value.split(",", 1)
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image data URI must contain valid base64") from exc
    else:
        try:
            image_path = Path(value).expanduser()
            if image_path.is_file():
                image_bytes = image_path.read_bytes()
            else:
                image_bytes = base64.b64decode(value, validate=True)
        except (OSError, ValueError, binascii.Error) as exc:
            raise ValueError("image must be an existing path or base64 data") from exc
    if not image_bytes:
        raise ValueError("image must not be empty")
    return image_bytes


@mcp.tool(name="kahin_ocr", annotations=_RO)
async def ocr(image: str) -> str:
    """Read the supplied image with Google Vision TEXT_DETECTION."""
    image_value, error = _validate_text(
        image,
        tool="kahin_ocr",
        field="image",
        maximum=_MAX_OCR_INPUT_LENGTH,
    )
    if error:
        return error
    assert image_value is not None
    try:
        image_bytes = _read_ocr_image(image_value)
    except ValueError as exc:
        return _json_error("kahin_ocr", str(exc), "invalid_argument", field="image")

    api_key = os.environ.get("GOOGLE_VISION_API_KEY", "")
    if not api_key:
        return _json_error(
            "kahin_ocr",
            "Google Vision API key is not configured",
            "missing_configuration",
        )

    request_body = {
        "requests": [{
            "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
            "features": [{"type": "TEXT_DETECTION"}],
        }],
    }
    try:
        async with _healer_ref.safe("kahin_ocr"):
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    _VISION_API_URL,
                    params={"key": api_key},
                    json=request_body,
                )
    except httpx.HTTPError:
        return _json_error("kahin_ocr", "Google Vision request failed", "provider_error")
    except Exception:
        logger.exception("Google Vision OCR failed")
        return _json_error("kahin_ocr", "OCR failed", "tool_failed")

    if response.status_code >= 400:
        return _json_error(
            "kahin_ocr",
            "Google Vision request failed",
            "provider_error",
            status=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError:
        return _json_error("kahin_ocr", "Google Vision returned invalid JSON", "invalid_provider_response")

    responses = payload.get("responses") if isinstance(payload, dict) else None
    first = responses[0] if isinstance(responses, list) and responses else {}
    if not isinstance(first, dict):
        return _json_error("kahin_ocr", "Google Vision returned an invalid response", "invalid_provider_response")
    provider_error = first.get("error")
    if isinstance(provider_error, dict):
        message = provider_error.get("message")
        return _json_error(
            "kahin_ocr",
            str(message) if isinstance(message, str) and message else "Google Vision OCR failed",
            "provider_error",
        )

    full_annotation = first.get("fullTextAnnotation")
    full_text = full_annotation.get("text") if isinstance(full_annotation, dict) else None
    if isinstance(full_text, str) and full_text.strip():
        return full_text.strip()
    annotations = first.get("textAnnotations")
    if isinstance(annotations, list) and annotations:
        description = annotations[0].get("description") if isinstance(annotations[0], dict) else None
        if isinstance(description, str):
            return description.strip()
    return ""


@mcp.tool(name="kahin_evaluate", annotations=_RW)
async def evaluate(expression: str) -> str:
    """Execute JavaScript in the browser context. Returns JSON-serializable result."""
    expression_value, error = _validate_text(
        expression, tool="kahin_evaluate", field="expression", maximum=_MAX_EVALUATE_LENGTH,
    )
    if error:
        return error
    assert expression_value is not None
    async with _healer_ref.safe("kahin_evaluate", expression=expression_value[:80]):
        result = await _safe_cdp("Runtime", "evaluate", {
            "expression": expression_value,
            "returnByValue": True,
            "awaitPromise": True,
        })
        try:
            parsed = orjson.loads(result)
        except orjson.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and not parsed.get("error") and not parsed.get("exceptionDetails"):
            await _auto_learn("Runtime", "evaluate", {"expression": expression_value[:50]})
        return result


@mcp.tool(name="kahin_execute_cdp", annotations=_DW)
async def execute_cdp(domain: str, command: str, parameters: dict[str, Any] | None = None) -> str:
    """Execute a raw CDP command directly (advanced). Auto-validates before sending."""
    if not isinstance(domain, str) or not domain or not isinstance(command, str) or not command:
        return _json_error(
            "kahin_execute_cdp",
            "domain and command must be non-empty strings",
            "invalid_argument",
        )
    if parameters is not None and not isinstance(parameters, dict):
        return _json_error(
            "kahin_execute_cdp", "parameters must be an object", "invalid_argument", field="parameters",
        )
    try:
        parameter_bytes = len(orjson.dumps(parameters or {}))
    except (TypeError, ValueError) as exc:
        return _json_error(
            "kahin_execute_cdp", f"parameters are not JSON-serializable: {exc}", "invalid_argument",
            field="parameters",
        )
    if parameter_bytes > _MAX_TOOL_PAYLOAD_BYTES:
        return _json_error(
            "kahin_execute_cdp",
            "parameters exceed Kahin's bounded tool payload",
            "argument_too_large",
            field="parameters",
            payloadBytes=parameter_bytes,
            maxPayloadBytes=_MAX_TOOL_PAYLOAD_BYTES,
        )
    async with _healer_ref.safe("kahin_execute_cdp", domain=domain, command=command):
        try:
            validation = _get_schema().validate_command(domain, command, parameters or {})
        except Exception as exc:  # noqa: BLE001 - schema is a public dependency
            return _json_error(
                "kahin_execute_cdp", f"CDP schema unavailable: {exc}", "schema_unavailable",
            )
        if not validation.get("valid"):
            errors = validation.get("errors", [])
            correction = list(validation.get("correction") or [])
            for item in errors:
                if isinstance(item, dict):
                    message = item.get("message")
                    if isinstance(message, str) and "Did you mean" in message:
                        correction.append(message)
            if not correction:
                correction.append("Fix validation_errors and retry the same command")
            result = {
                "error": "Command validation failed",
                "validation_errors": errors,
                "correction": correction,
            }
            return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
        return await _safe_cdp(domain, command, parameters or {})
