"""emulation_mirage.py — Mirage (Camoufox/Juggler) emulation tools.

Faz 9 Task 3: every emulation override maps 1:1 to a real Juggler method
(verified against Protocol.js):

- set_user_agent            -> Browser.setUserAgentOverride
- set_viewport / dsf        -> Browser.setDefaultViewport
- set_media / color / motion-> Page.setEmulatedMedia / Browser.setColorScheme
                               / Browser.setReducedMotion
- set_touch                 -> Browser.setTouchOverride
- set_locale / timezone     -> Browser.setLocaleOverride / Browser.setTimezoneOverride
- set_geolocation           -> Browser.setGeolocationOverride

Juggler has NO CDP-style Emulation.* domain — the overrides live on the
Browser (context level) and Page domains, and that is what these tools use.
"""

from __future__ import annotations

import math
from numbers import Integral, Real
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.tools._common import (
    _DW,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_engine,
    _require_mirage,
)

# Last viewportSize sent by mirage_set_viewport — lets
# mirage_set_device_scale_factor rebuild a full viewport without needing a
# loaded document (Runtime.evaluate requires a page). Keyed by engine and
# browser context so a second isolated context never inherits the first one's
# device metrics.
_last_viewport: tuple[Any, str | None, dict[str, Any]] | None = None

_MIN_VIEWPORT = 1
_MAX_VIEWPORT = 10000
_MIN_SCALE_FACTOR = 0.1
_MAX_SCALE_FACTOR = 8.0
_MAX_USER_AGENT = 4096
_MAX_LOCALE = 64
_MAX_TIMEZONE = 128
_MAX_GEO_ACCURACY = 10_000_000.0
_MEDIA_TYPES = frozenset({"screen", "print"})
_COLOR_SCHEMES = frozenset({"dark", "light", "no-preference"})
_REDUCED_MOTION = frozenset({"reduce", "no-preference"})


def _ctx(value: Any, limit: int = 160) -> str:
    try:
        text = value if isinstance(value, str) else repr(value)
    except Exception:  # pragma: no cover - defensive logging path
        text = f"<{type(value).__name__}>"
    return text[:limit]


def _dump(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()


def _context_params(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scope Browser-domain overrides to the selected tab's context."""
    result = dict(params or {})
    try:
        context_id = _mirage_engine().current_browser_context_id()
    except Exception:  # liveness is reported by _mirage_call_checked
        context_id = None
    if context_id:
        result["browserContextId"] = context_id
    return result


def _current_context_id() -> str | None:
    try:
        return _mirage_engine().current_browser_context_id()
    except Exception:  # liveness is reported by the actual Juggler call
        return None


def _error(
    tool: str,
    code: str,
    message: str,
    *,
    method: str | None = None,
    **details: Any,
) -> str:
    payload: dict[str, Any] = {
        "error": message,
        "code": code,
        "tool": tool,
        "engine": "mirage",
    }
    if method is not None:
        payload["method"] = method
    payload.update(details)
    return _dump(payload)


def _normalise_error(raw: Any, tool: str, method: str | None = None) -> str:
    if isinstance(raw, str):
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            payload = None
    else:
        payload = raw
    if not isinstance(payload, dict) or "error" not in payload:
        return _error(
            tool,
            "engine_unavailable",
            str(raw)[:500] or "Mirage engine is unavailable",
            method=method,
            hint="Check kahin_engine_health, then restart the browser if needed.",
        )
    payload = dict(payload)
    payload.setdefault("tool", tool)
    payload.setdefault("engine", "mirage")
    if method is not None:
        payload.setdefault("method", method)
    if "code" not in payload:
        message = str(payload.get("error", ""))
        payload["code"] = (
            "connection_lost"
            if "connection" in message.lower() or "closed" in message.lower()
            else "juggler_call_failed"
        )
    return _dump(payload)


async def _require_mirage_structured(tool: str) -> str | None:
    try:
        err = await _require_mirage()
    except RuntimeError as exc:
        return _error(
            tool,
            "juggler_health_failed",
            f"Mirage health check failed: {exc}",
            hint="Check kahin_engine_health, then restart the browser if needed.",
        )
    except Exception as exc:  # noqa: BLE001
        return _error(
            tool,
            "connection_lost",
            f"Mirage health check lost the browser connection: {exc}",
            hint="Use kahin_browser_stop, then kahin_browser_start.",
        )
    return _normalise_error(err, tool) if err else None


async def _mirage_call_checked(
    tool: str, method: str, params: dict[str, Any] | None = None,
) -> str:
    err = await _require_mirage_structured(tool)
    if err:
        return err
    try:
        result = await _mirage_call(method, params)
    except RuntimeError as exc:
        return _error(tool, "juggler_call_failed", f"Juggler call failed: {exc}", method=method)
    except Exception as exc:  # noqa: BLE001
        return _error(
            tool,
            "connection_lost",
            f"Connection lost while calling {method}: {exc}",
            method=method,
            hint="Use kahin_browser_stop, then kahin_browser_start.",
        )
    try:
        payload = orjson.loads(result)
    except (TypeError, orjson.JSONDecodeError):
        return _normalise_error(result, tool, method)
    if isinstance(payload, dict) and "error" in payload:
        return _normalise_error(payload, tool, method)
    return result


def _validate_text(
    tool: str, field: str, value: Any, *, max_length: int, allow_empty: bool = False,
) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, _error(tool, "invalid_parameter", f"{field} must be a string", field=field)
    if not allow_empty and not value.strip():
        return None, _error(tool, "invalid_parameter", f"{field} must not be empty", field=field)
    if len(value) > max_length:
        return None, _error(
            tool, "invalid_parameter", f"{field} exceeds the {max_length}-character limit", field=field,
        )
    if any(ord(char) < 32 and char not in "\t" for char in value):
        return None, _error(tool, "invalid_parameter", f"{field} contains control characters", field=field)
    return value, None


def _validate_bool(tool: str, field: str, value: Any) -> str | None:
    if not isinstance(value, bool):
        return _error(tool, "invalid_parameter", f"{field} must be a boolean", field=field)
    return None


def _validate_dimension(tool: str, field: str, value: Any) -> tuple[int, bool, str | None]:
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0, False, _error(tool, "invalid_parameter", f"{field} must be an integer", field=field)
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return 0, False, _error(tool, "invalid_parameter", f"{field} must be a finite integer", field=field)
    if not math.isfinite(numeric) or not numeric.is_integer():
        return 0, False, _error(tool, "invalid_parameter", f"{field} must be a finite integer", field=field)
    requested = int(numeric)
    if requested < _MIN_VIEWPORT:
        return 0, False, _error(
            tool, "invalid_parameter", f"{field} must be at least {_MIN_VIEWPORT}", field=field,
        )
    effective = min(requested, _MAX_VIEWPORT)
    return effective, effective != requested, None


def _validate_scale(tool: str, field: str, value: Any) -> tuple[float, bool, str | None]:
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0.0, False, _error(tool, "invalid_parameter", f"{field} must be a number", field=field)
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return 0.0, False, _error(
            tool, "invalid_parameter", f"{field} must be a finite number greater than zero", field=field,
        )
    if not math.isfinite(numeric) or numeric <= 0:
        return 0.0, False, _error(
            tool, "invalid_parameter", f"{field} must be a finite number greater than zero", field=field,
        )
    if numeric < _MIN_SCALE_FACTOR:
        return _MIN_SCALE_FACTOR, True, None
    effective = min(numeric, _MAX_SCALE_FACTOR)
    return effective, effective != numeric, None


def _with_clamp_metadata(
    tool: str, raw: str, requested: dict[str, Any], applied: dict[str, Any], clamped: bool,
) -> str:
    if not clamped:
        return raw
    try:
        payload = orjson.loads(raw)
    except (TypeError, orjson.JSONDecodeError):
        return _error(tool, "invalid_native_response", "Native emulation call returned invalid JSON")
    if isinstance(payload, dict) and "error" in payload:
        return raw
    return _dump({
        "result": payload,
        "clamped": True,
        "requested": requested,
        "applied": applied,
    })


def _is_error(raw: str) -> bool:
    try:
        payload = orjson.loads(raw)
    except (TypeError, orjson.JSONDecodeError):
        return True
    return isinstance(payload, dict) and "error" in payload


def _validate_geo_number(
    tool: str, field: str, value: Any, *, minimum: float, maximum: float,
) -> tuple[float, str | None]:
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0.0, _error(tool, "invalid_parameter", f"{field} must be a number", field=field)
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return 0.0, _error(tool, "invalid_parameter", f"{field} must be finite", field=field)
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        return 0.0, _error(
            tool,
            "invalid_parameter",
            f"{field} must be between {minimum} and {maximum}",
            field=field,
        )
    return numeric, None


def _report_value(value: Any) -> Any:
    if isinstance(value, int) and value.bit_length() > 256:
        return "<integer-too-large>"
    return value


async def _read_viewport_size(tool: str) -> tuple[dict[str, Any] | None, str | None]:
    """Current viewport size via Runtime.evaluate (innerWidth/innerHeight)."""
    err = await _require_mirage_structured(tool)
    if err:
        return None, err
    try:
        engine = _mirage_engine()
        await engine.ensure_page()
        raw = await engine.call("Runtime.evaluate", {
            "expression": "(() => ({width: window.innerWidth, height: window.innerHeight}))()",
        })
    except RuntimeError as exc:
        return None, _error(tool, "juggler_call_failed", f"Juggler call failed: {exc}", method="Runtime.evaluate")
    except Exception as exc:  # noqa: BLE001
        return None, _error(
            tool,
            "connection_lost",
            f"Connection lost while reading the viewport: {exc}",
            method="Runtime.evaluate",
            hint="Use kahin_browser_stop, then kahin_browser_start.",
        )
    if not isinstance(raw, dict):
        return None, _error(tool, "invalid_native_response", "Runtime.evaluate returned a non-object response")
    result_payload = raw.get("result")
    if not isinstance(result_payload, dict):
        return None, _error(tool, "invalid_native_response", "Runtime.evaluate returned no result object")
    size = result_payload.get("value")
    if not isinstance(size, dict):
        return None, _error(tool, "invalid_native_response", "Runtime.evaluate returned no viewport object")
    width = size.get("width")
    height = size.get("height")
    if (
        isinstance(width, bool) or not isinstance(width, Integral)
        or isinstance(height, bool) or not isinstance(height, Integral)
        or int(width) < _MIN_VIEWPORT or int(height) < _MIN_VIEWPORT
    ):
        return None, _error(tool, "invalid_native_response", "Runtime.evaluate returned invalid viewport dimensions")
    return {"width": min(int(width), _MAX_VIEWPORT), "height": min(int(height), _MAX_VIEWPORT)}, None


@mcp.tool(name="kahin_mirage_set_user_agent", annotations=_RW)
async def mirage_set_user_agent(user_agent: str) -> str:
    """Mirage: override the User-Agent header (Browser.setUserAgentOverride).
    Empty string resets to the real UA."""
    tool = "kahin_mirage_set_user_agent"
    async with _healer_ref.safe(tool, user_agent=_ctx(user_agent)):
        checked, validation_error = _validate_text(
            tool, "user_agent", user_agent, max_length=_MAX_USER_AGENT, allow_empty=True,
        )
        if validation_error:
            return validation_error
        res = await _mirage_call_checked(
            tool, "Browser.setUserAgentOverride", _context_params({"userAgent": checked}),
        )
        if checked:
            try:
                engine = _mirage_engine()
                sid = engine.current_session_id()
                if sid:
                    js = f"try {{ Object.defineProperty(Navigator.prototype, 'userAgent', {{ get: () => {json.dumps(checked)}, configurable: true }}); }} catch (e) {{}}"
                    await engine.call("Runtime.evaluate", {"expression": js}, session_id=sid)
            except Exception:
                pass
        else:
            try:
                engine = _mirage_engine()
                sid = engine.current_session_id()
                if sid:
                    js = "try { delete Navigator.prototype.userAgent; } catch (e) {}"
                    await engine.call("Runtime.evaluate", {"expression": js}, session_id=sid)
            except Exception:
                pass
        return res


@mcp.tool(name="kahin_mirage_set_viewport", annotations=_RW)
async def mirage_set_viewport(
    width: int, height: int, device_scale_factor: float | None = None,
) -> str:
    """Mirage: set the default viewport size (Browser.setDefaultViewport)."""
    tool = "kahin_mirage_set_viewport"
    async with _healer_ref.safe(tool, width=_ctx(width), height=_ctx(height)):
        checked_width, width_clamped, validation_error = _validate_dimension(tool, "width", width)
        if validation_error:
            return validation_error
        checked_height, height_clamped, validation_error = _validate_dimension(tool, "height", height)
        if validation_error:
            return validation_error
        checked_scale: float | None = None
        scale_clamped = False
        if device_scale_factor is not None:
            checked_scale, scale_clamped, validation_error = _validate_scale(
                tool, "device_scale_factor", device_scale_factor,
            )
            if validation_error:
                return validation_error
        global _last_viewport
        viewport_size = {"width": checked_width, "height": checked_height}
        viewport: dict[str, Any] = {"viewportSize": viewport_size}
        if checked_scale is not None:
            viewport["deviceScaleFactor"] = checked_scale
        context_id = _current_context_id()
        result = await _mirage_call_checked(
            tool, "Browser.setDefaultViewport", _context_params({"viewport": viewport}),
        )
        if _is_error(result):
            return result
        _last_viewport = (state._current_engine, context_id, viewport_size)
        return _with_clamp_metadata(
            tool,
            result,
            {
                "width": _report_value(width),
                "height": _report_value(height),
                "deviceScaleFactor": _report_value(device_scale_factor),
            },
            {"width": checked_width, "height": checked_height, "deviceScaleFactor": checked_scale},
            width_clamped or height_clamped or scale_clamped,
        )


@mcp.tool(name="kahin_mirage_set_device_scale_factor", annotations=_RW)
async def mirage_set_device_scale_factor(device_scale_factor: float) -> str:
    """Mirage: override devicePixelRatio (Browser.setDefaultViewport with the
    current viewportSize + the new deviceScaleFactor)."""
    global _last_viewport
    tool = "kahin_mirage_set_device_scale_factor"
    async with _healer_ref.safe(tool, device_scale_factor=_ctx(device_scale_factor)):
        checked_scale, scale_clamped, validation_error = _validate_scale(
            tool, "device_scale_factor", device_scale_factor,
        )
        if validation_error:
            return validation_error
        # Cache only counts for the SAME engine instance: a restarted browser
        # gets the real viewport (measured) instead of the previous one.
        size = None
        context_id = _current_context_id()
        if (
            _last_viewport is not None
            and _last_viewport[0] is state._current_engine
            and _last_viewport[1] == context_id
        ):
            size = _last_viewport[2]
        if size is None:
            size, read_error = await _read_viewport_size(tool)
            if read_error:
                return read_error
        viewport: dict[str, Any] = {"deviceScaleFactor": checked_scale}
        if size:
            viewport["viewportSize"] = size
        result = await _mirage_call_checked(
            tool, "Browser.setDefaultViewport", _context_params({"viewport": viewport}),
        )
        if _is_error(result):
            return result
        if size:
            _last_viewport = (state._current_engine, context_id, size)
        return _with_clamp_metadata(
            tool,
            result,
            {"deviceScaleFactor": _report_value(device_scale_factor)},
            {"deviceScaleFactor": checked_scale},
            scale_clamped,
        )


@mcp.tool(name="kahin_mirage_set_media", annotations=_RW)
async def mirage_set_media(media: str = "screen") -> str:
    """Mirage: emulate a media type (Page.setEmulatedMedia type=screen|print)."""
    tool = "kahin_mirage_set_media"
    async with _healer_ref.safe(tool, media=_ctx(media, 40)):
        checked, validation_error = _validate_text(tool, "media", media, max_length=16)
        if validation_error:
            return validation_error
        if checked not in _MEDIA_TYPES:
            return _error(tool, "invalid_parameter", "media must be one of: screen, print", field="media")
        return await _mirage_call_checked(tool, "Page.setEmulatedMedia", {"type": checked})


@mcp.tool(name="kahin_mirage_set_touch", annotations=_RW)
async def mirage_set_touch(has_touch: bool) -> str:
    """Mirage: emulate touch support (Browser.setTouchOverride hasTouch)."""
    tool = "kahin_mirage_set_touch"
    async with _healer_ref.safe(tool, has_touch=_ctx(has_touch, 40)):
        validation_error = _validate_bool(tool, "has_touch", has_touch)
        if validation_error:
            return validation_error
        return await _mirage_call_checked(
            tool, "Browser.setTouchOverride", _context_params({"hasTouch": has_touch}),
        )


@mcp.tool(name="kahin_mirage_set_color_scheme", annotations=_RW)
async def mirage_set_color_scheme(color_scheme: str = "dark") -> str:
    """Mirage: emulate prefers-color-scheme (Browser.setColorScheme:
    dark|light|no-preference)."""
    tool = "kahin_mirage_set_color_scheme"
    async with _healer_ref.safe(tool, color_scheme=_ctx(color_scheme, 40)):
        checked, validation_error = _validate_text(tool, "color_scheme", color_scheme, max_length=16)
        if validation_error:
            return validation_error
        if checked not in _COLOR_SCHEMES:
            return _error(
                tool, "invalid_parameter", "color_scheme must be one of: dark, light, no-preference",
                field="color_scheme",
            )
        return await _mirage_call_checked(
            tool, "Browser.setColorScheme", _context_params({"colorScheme": checked}),
        )


@mcp.tool(name="kahin_mirage_set_reduced_motion", annotations=_RW)
async def mirage_set_reduced_motion(reduced_motion: str = "reduce") -> str:
    """Mirage: emulate prefers-reduced-motion (Browser.setReducedMotion:
    reduce|no-preference)."""
    tool = "kahin_mirage_set_reduced_motion"
    async with _healer_ref.safe(tool, reduced_motion=_ctx(reduced_motion, 40)):
        checked, validation_error = _validate_text(tool, "reduced_motion", reduced_motion, max_length=16)
        if validation_error:
            return validation_error
        if checked not in _REDUCED_MOTION:
            return _error(
                tool, "invalid_parameter", "reduced_motion must be one of: reduce, no-preference",
                field="reduced_motion",
            )
        return await _mirage_call_checked(
            tool, "Browser.setReducedMotion", _context_params({"reducedMotion": checked}),
        )


@mcp.tool(name="kahin_mirage_set_locale", annotations=_RW)
async def mirage_set_locale(locale: str) -> str:
    """Mirage: override the browser locale (Browser.setLocaleOverride)."""
    tool = "kahin_mirage_set_locale"
    async with _healer_ref.safe(tool, locale=_ctx(locale, 40)):
        checked, validation_error = _validate_text(tool, "locale", locale, max_length=_MAX_LOCALE)
        if validation_error:
            return validation_error
        return await _mirage_call_checked(
            tool, "Browser.setLocaleOverride", _context_params({"locale": checked}),
        )


@mcp.tool(name="kahin_mirage_set_timezone", annotations=_RW)
async def mirage_set_timezone(timezone_id: str) -> str:
    """Mirage: override the timezone, e.g. Europe/Istanbul
    (Browser.setTimezoneOverride timezoneId)."""
    tool = "kahin_mirage_set_timezone"
    async with _healer_ref.safe(tool, timezone_id=_ctx(timezone_id, 60)):
        checked, validation_error = _validate_text(tool, "timezone_id", timezone_id, max_length=_MAX_TIMEZONE)
        if validation_error:
            return validation_error
        assert checked is not None
        try:
            ZoneInfo(checked)
        except ZoneInfoNotFoundError:
            return _error(
                tool, "invalid_parameter", f"Unknown IANA timezone: {checked}", field="timezone_id",
            )
        return await _mirage_call_checked(
            tool, "Browser.setTimezoneOverride", _context_params({"timezoneId": checked}),
        )


@mcp.tool(name="kahin_mirage_set_geolocation", annotations=_DW)
async def mirage_set_geolocation(
    latitude: float = 0.0, longitude: float = 0.0, accuracy: float | None = None, clear: bool = False,
) -> str:
    """Mirage: override geolocation (Browser.setGeolocationOverride). Set
    clear=true to reset to the real position."""
    tool = "kahin_mirage_set_geolocation"
    async with _healer_ref.safe(
        tool,
        latitude=_ctx(latitude),
        longitude=_ctx(longitude),
        accuracy=_ctx(accuracy),
        clear=clear,
    ):
        validation_error = _validate_bool(tool, "clear", clear)
        if validation_error:
            return validation_error
        if clear:
            return await _mirage_call_checked(
                tool, "Browser.setGeolocationOverride", _context_params({"geolocation": None}),
            )
        checked_latitude, validation_error = _validate_geo_number(
            tool, "latitude", latitude, minimum=-90.0, maximum=90.0,
        )
        if validation_error:
            return validation_error
        checked_longitude, validation_error = _validate_geo_number(
            tool, "longitude", longitude, minimum=-180.0, maximum=180.0,
        )
        if validation_error:
            return validation_error
        geolocation: dict[str, Any] = {"latitude": checked_latitude, "longitude": checked_longitude}
        if accuracy is not None:
            checked_accuracy, validation_error = _validate_geo_number(
                tool, "accuracy", accuracy, minimum=0.0, maximum=_MAX_GEO_ACCURACY,
            )
            if validation_error:
                return validation_error
            geolocation["accuracy"] = checked_accuracy
        return await _mirage_call_checked(
            tool, "Browser.setGeolocationOverride", _context_params({"geolocation": geolocation}),
        )
