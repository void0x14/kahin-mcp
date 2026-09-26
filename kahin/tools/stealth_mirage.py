"""stealth_mirage.py — Stealth & anti-detect tools (Faz 3).

Task 1: read-only self-audit probe (`kahin_stealth_audit`). Humanized
input tools (Task 3): jittered Bézier mouse travel, humanized clicks and
cadence typing. Task 4: per-domain identity rotation policy
(`kahin_identity_pin`/`unpin`/`pins`/`for_domain`) backed by the bounded
pin store. Task 5: `kahin_fingerprint_report` (live page fingerprint a
site would observe) and `kahin_proxy_resolve` (proxy exit-IP geo + sync
recommendations); `browser_start(proxy=...)` applies the proxy env.
"""

from __future__ import annotations

import asyncio
import math

import orjson

from kahin._mcp import mcp
from kahin.humanize import bezier_trajectory, jittered_delay, step_delays, typing_cadence
from kahin.stealth import (
    STEALTH_PROBE_JS,
    _redact_proxy,
    load_pins,
    normalize_domain,
    pin_identity,
    proxy_env,
    resolve_proxy_geo,
    score_checks,
    unpin_identity,
)
from kahin.tools._common import _DW, _RO, _RW, _healer_ref
from kahin.tools.pilot_mirage import (
    _MAX_COORDINATE,
    _MAX_KEY_TEXT_LENGTH,
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _action_ready,
    _bounded_float,
    _bounded_int,
    _capture_page_session,
    _dispatch_mouse,
    _is_error_response,
    _json_error,
    _key_defaults,
    _safe_mirage_call,
    _safe_mirage_eval_result,
    _strict_float,
    _text_arg,
    get_last_mouse_position,
    mirage_key_text_fast,
)


# Momentum state: unit direction of the last generated trajectory so
# consecutive calls form one continuous gesture instead of independent
# arcs. Bounded: a single unit tuple, updated in place.
_last_direction: tuple[float, float] | None = None


def _remember_direction(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Store and return the unit direction of the last few movement points."""
    global _last_direction
    if len(points) < 2:
        return _last_direction
    tail = points[-min(6, len(points)):]
    dx = tail[-1][0] - tail[0][0]
    dy = tail[-1][1] - tail[0][1]
    length = math.hypot(dx, dy)
    if length > 1e-6:
        _last_direction = (dx / length, dy / length)
    return _last_direction


@mcp.tool(name="kahin_stealth_audit", annotations=_RO)
async def stealth_audit(frame_id: str | None = None) -> str:
    """Mirage: run the read-only stealth probe package on the current page.
    Returns {audited, engine, score: {passed, total, ratio}, checks:
    [{check, passed, detail}]}. A low ratio pinpoints leak vectors to fix;
    every check is always reported, unknown probes fail with a detail."""
    async with _healer_ref.safe("kahin_stealth_audit", frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_stealth_audit")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _safe_mirage_eval_result(
            "kahin_stealth_audit", STEALTH_PROBE_JS, frame_id, session_id=session_id,
        )
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return _json_error(
                "kahin_stealth_audit",
                "JavaScript evaluation failed",
                "javascript_error",
            )
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        if not isinstance(parsed, dict):
            return _json_error(
                "kahin_stealth_audit",
                "probe returned no value",
                "invalid_engine_response",
            )
        checks = parsed.get("checks")
        if not isinstance(checks, list):
            return _json_error(
                "kahin_stealth_audit",
                "probe returned no checks list",
                "invalid_engine_response",
            )
        return orjson.dumps({
            "audited": True,
            "engine": "mirage",
            "score": score_checks(checks),
            "checks": checks,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_mouse_trajectory", annotations=_DW)
async def mirage_mouse_trajectory(
    x: float, y: float, steps: int = 0, jitter: float = 2.0, seed: int | None = None,
    delay_ms: float = 4.0, delay_jitter_ms: float = 2.0,
) -> str:
    """Mirage: move the mouse from its last position to (x, y) with one
    continuous, human-like gesture — distance-adaptive step count,
    ease-in-out speed profile (slow → fast → slow), tapered organic
    wobble (no constant per-point jitter), momentum blended from the
    previous movement so chained calls read as a single path, and an
    optional slight overshoot with a natural settle.
    ``steps=0`` auto-adapts to the distance; an explicit count 2..200 is
    honoured. Returns {moved, from, to, points:[...]} containing the
    REAL dispatched points. Bounds: steps 0..200, jitter 0..20px,
    delay 0.5..30ms."""
    x_value, error = _strict_float(
        x, tool="kahin_mirage_mouse_trajectory", field="x",
        minimum=0.0, maximum=_MAX_COORDINATE,
    )
    if error:
        return error
    y_value, error = _strict_float(
        y, tool="kahin_mirage_mouse_trajectory", field="y",
        minimum=0.0, maximum=_MAX_COORDINATE,
    )
    if error:
        return error
    steps_value = _bounded_int(steps, minimum=0, maximum=200, default=0)
    jitter_value = _bounded_float(jitter, minimum=0.0, maximum=20.0, default=2.0)
    delay_value = _bounded_float(delay_ms, minimum=0.5, maximum=30.0, default=4.0)
    delay_jitter = _bounded_float(delay_jitter_ms, minimum=0.0, maximum=15.0, default=2.0)
    async with _healer_ref.safe(
        "kahin_mirage_mouse_trajectory", x=x_value, y=y_value,
        steps=steps_value, jitter=jitter_value, seed=seed,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_mouse_trajectory")
        if capture_error:
            return capture_error
        assert session_id is not None
        start_x, start_y = get_last_mouse_position()
        entry_dir = _last_direction
        points = bezier_trajectory(
            start_x, start_y, x_value, y_value,
            steps=(steps_value if steps_value > 0 else None),
            jitter=jitter_value, seed=seed, entry_dir=entry_dir,
        )
        _remember_direction(points)
        if points and points[0] == (start_x, start_y):
            next_point = next((point for point in points[1:] if point != points[0]), None)
            if next_point is None:
                # A same-coordinate trajectory has no real movement to send;
                # avoid the Juggler no-op dispatch that wedges the input pipe.
                points = []
            else:
                # The generator deliberately includes its origin. Replace
                # that no-op with a midpoint toward the first real point so
                # the public step count remains intact without dispatching a
                # duplicate coordinate.
                points[0] = (
                    (start_x + next_point[0]) / 2,
                    (start_y + next_point[1]) / 2,
                )
        delays = step_delays(len(points), base_ms=delay_value, jitter_ms=delay_jitter, seed=seed)
        for index, (px, py) in enumerate(points):
            move = await _dispatch_mouse("mousemove", px, py, button=0, buttons=0, session_id=session_id)
            if _is_error_response(move):
                return move
            if index < len(points) - 1:
                await asyncio.sleep(delays[index] / 1000.0)
        return orjson.dumps({
            "moved": len(points), "from": [start_x, start_y], "to": [x_value, y_value],
            "points": [[p[0], p[1]] for p in points],
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_click_humanized", annotations=_DW)
async def mirage_click_humanized(
    selector: str,
    steps: int = 0,
    jitter: float = 2.0,
    click_delay_ms: float = 80.0,
    click_jitter_ms: float = 20.0,
    seed: int | None = None,
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: humanized click — actionability wait, one continuous
    ease-in-out mouse travel to the element center (distance-adaptive
    steps, momentum from the previous movement, tapered wobble), then
    real mousedown, a jittered press delay, and mouseup. The DOM click
    fires exactly like mirage_click's.
    Returns {clicked, moved, from, to, press_delay_ms, points:[...]}.
    ``steps=0`` auto-adapts to the distance."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_click_humanized", field="selector",
        maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error(
            "kahin_mirage_click_humanized",
            "selector must not be empty",
            "invalid_argument",
            field="selector",
        )
    steps_value = _bounded_int(steps, minimum=0, maximum=200, default=0)
    jitter_value = _bounded_float(jitter, minimum=0.0, maximum=20.0, default=2.0)
    delay_value = _bounded_float(click_delay_ms, minimum=10.0, maximum=2_000.0, default=80.0)
    delay_jitter = _bounded_float(click_jitter_ms, minimum=0.0, maximum=500.0, default=20.0)
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe(
        "kahin_mirage_click_humanized", selector=selector_value[:80], steps=steps_value,
        jitter=jitter_value, click_delay_ms=delay_value, timeout=timeout_value, frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_click_humanized")
        if capture_error:
            return capture_error
        assert session_id is not None
        ready = await _action_ready(
            "kahin_mirage_click_humanized", selector_value,
            timeout=timeout_value, frame_id=frame_id, session_id=session_id,
        )
        if isinstance(ready, str):
            return ready
        target_x, target_y = ready
        start_x, start_y = get_last_mouse_position()
        entry_dir = _last_direction
        points = bezier_trajectory(
            start_x, start_y, target_x, target_y,
            steps=(steps_value if steps_value > 0 else None),
            jitter=jitter_value, seed=seed, entry_dir=entry_dir,
        )
        _remember_direction(points)
        travel_delays = step_delays(len(points), base_ms=4.0, jitter_ms=2.0, seed=seed)
        for index, (px, py) in enumerate(points):
            if index == len(points) - 1:
                down = await _dispatch_mouse("mousedown", px, py, button=0, buttons=1, session_id=session_id)
                if _is_error_response(down):
                    return down
            else:
                move = await _dispatch_mouse("mousemove", px, py, button=0, buttons=0, session_id=session_id)
                if _is_error_response(move):
                    return move
                await asyncio.sleep(travel_delays[index] / 1000.0)
        await asyncio.sleep(jittered_delay(delay_value, delay_jitter, seed=seed) / 1000.0)
        up = await _dispatch_mouse("mouseup", target_x, target_y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({
            "clicked": selector_value, "moved": len(points),
            "from": [start_x, start_y], "to": [target_x, target_y],
            "press_delay_ms": delay_value,
            "points": [[p[0], p[1]] for p in points],
        }, option=orjson.OPT_INDENT_2).decode()


_OVERLAY_CLEAR_JS = r"""
((x, y) => {
  // Residual overlay classes seen blocking real clicks (semantic-ui dimmers
  // and modals, generic dimmers/modals/overlays/tips popups).
  const KNOWN_OVERLAY_SELECTOR = [
    ".ui.dimmer", ".ui.modal",
    "[class*='dimmer']", "[class*='modal']", "[class*='overlay']", "[class*='tips']",
  ].join(",");
  const isOverlay = (el) => {
    if (!el || el.nodeType !== 1) return false;
    try {
      if (el.matches && el.matches(KNOWN_OVERLAY_SELECTOR)) return true;
    } catch (e) { /* ignore selector errors */ }
    // Generic full-viewport fixed/absolute layer above the page content.
    const style = getComputedStyle(el);
    if (style.position !== "fixed" && style.position !== "absolute") return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (Number(style.zIndex) <= 0) return false;
    const rect = el.getBoundingClientRect();
    const vw = window.innerWidth || 0;
    const vh = window.innerHeight || 0;
    if (vw <= 0 || vh <= 0) return false;
    return Math.max(0, rect.width) * Math.max(0, rect.height) >= 0.7 * vw * vh;
  };
  const overlayRootOf = (el) => {
    // Outermost overlay-classified ancestor-or-self: hiding this root clears
    // the whole residual layer instead of its individual children.
    let root = null;
    let node = el;
    while (node && node.nodeType === 1) {
      if (isOverlay(node)) root = node;
      node = node.parentElement;
    }
    return root;
  };
  const isVisible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0;
  };
  const label = (el) => {
    const tag = (el.tagName || "node").toLowerCase();
    const id = el.id ? "#" + el.id : "";
    const raw = typeof el.className === "string" ? el.className : "";
    const cls = raw.trim() ? "." + raw.trim().split(/\s+/).slice(0, 3).join(".") : "";
    return tag + id + cls;
  };
  const stack = (typeof document.elementsFromPoint === "function")
    ? Array.from(document.elementsFromPoint(x, y) || [])
    : [];
  // The intended target is the topmost element that is not part of a
  // residual overlay; every overlay root painted above it is a foreign layer.
  let target = null;
  let targetIndex = -1;
  for (let i = 0; i < stack.length; i++) {
    if (!overlayRootOf(stack[i])) { target = stack[i]; targetIndex = i; break; }
  }
  const hidden = [];
  const seen = new Set();
  const limit = targetIndex === -1 ? stack.length : targetIndex;
  for (let i = 0; i < limit; i++) {
    const root = overlayRootOf(stack[i]);
    if (!root || seen.has(root)) continue;
    seen.add(root);
    if (!isVisible(root)) continue;
    root.style.setProperty("display", "none", "important");
    hidden.push(label(root));
  }
  return {
    cleared: hidden.length,
    hidden: hidden,
    point: [x, y],
    target: target ? label(target) : null,
    stackSize: stack.length,
    stack: stack.slice(0, 12).map(label),
  };
})(__X__, __Y__)
"""


@mcp.tool(name="kahin_mirage_clear_overlays", annotations=_DW)
async def mirage_clear_overlays(x: float, y: float, frame_id: str | None = None) -> str:
    """Mirage: deterministically clear residual overlays blocking a point.

    Reads the live paint stack at (x, y) with document.elementsFromPoint,
    finds the topmost element that is not part of a residual overlay (the
    intended target), and sets ``display:none !important`` on every overlay
    layer painted above it — semantic-ui ``.ui.dimmer``/``.ui.modal``
    residues, generic dimmer/modal/overlay/tips layers, and full-viewport
    fixed/absolute blockers. Idempotent: already-hidden layers leave the
    stack, so a repeat call clears 0. Returns {cleared, hidden:[selectors],
    point, target, stackSize, stack}. Destructive only to the overlay
    layers above the target."""
    tool = "kahin_mirage_clear_overlays"
    x_value, error = _strict_float(x, tool=tool, field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool=tool, field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    async with _healer_ref.safe(tool, x=x_value, y=y_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session(tool)
        if capture_error:
            return capture_error
        assert session_id is not None
        expression = _OVERLAY_CLEAR_JS.replace(
            "__X__", orjson.dumps(x_value).decode(),
        ).replace("__Y__", orjson.dumps(y_value).decode())
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return _json_error(tool, "JavaScript evaluation failed", "javascript_error")
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        if not isinstance(parsed, dict):
            return _json_error(tool, "overlay probe returned no value", "invalid_engine_response")
        return orjson.dumps({
            "engine": "mirage",
            "cleared": parsed.get("cleared", 0),
            "hidden": parsed.get("hidden", []),
            "point": parsed.get("point", [x_value, y_value]),
            "target": parsed.get("target"),
            "stackSize": parsed.get("stackSize", 0),
            "stack": parsed.get("stack", []),
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_key_text", annotations=_RW)
async def mirage_key_text(
    text: str,
    delay_ms: float = 45.0,
    jitter_ms: float = 25.0,
    seed: int | None = None,
    frame_id: str | None = None,
) -> str:
    """Mirage: type text with a humanized per-character cadence. Each char
    is dispatched as real keydown/keyup pairs (Juggler lowercase types,
    browser-native codes via _key_defaults) separated by jittered delays.
    delay_ms=0 disables the cadence and falls back to the fast path; the
    text lands in the currently focused element (use mirage_focus/
    mirage_type for selector-driven typing). Returns {typed, cadence}."""
    text_value, error = _text_arg(
        text, tool="kahin_mirage_key_text", field="text", maximum=_MAX_KEY_TEXT_LENGTH,
    )
    if error:
        return error
    base = _bounded_float(delay_ms, minimum=0.0, maximum=500.0, default=45.0)
    jitter = _bounded_float(jitter_ms, minimum=0.0, maximum=200.0, default=25.0)
    async with _healer_ref.safe(
        "kahin_mirage_key_text", text=text_value[:80],
        delay_ms=base, jitter_ms=jitter, seed=seed, frame_id=frame_id,
    ):
        if base == 0.0:
            return await mirage_key_text_fast(text_value)
        session_id, capture_error = await _capture_page_session("kahin_mirage_key_text")
        if capture_error:
            return capture_error
        assert session_id is not None
        cadence = typing_cadence(len(text_value), base_ms=base, jitter_ms=jitter, seed=seed)
        for index, char in enumerate(text_value):
            code, key_code = _key_defaults(char)
            key_code = _bounded_int(key_code, minimum=0, maximum=65_535, default=0)
            down = await _safe_mirage_call("kahin_mirage_key_text", "Page.dispatchKeyEvent", {
                "type": "keydown", "key": char, "keyCode": key_code,
                "location": 0, "code": code, "repeat": False, "text": char,
            }, session_id=session_id)
            if _is_error_response(down):
                return down
            up = await _safe_mirage_call("kahin_mirage_key_text", "Page.dispatchKeyEvent", {
                "type": "keyup", "key": char, "keyCode": key_code,
                "location": 0, "code": code, "repeat": False,
            }, session_id=session_id)
            if _is_error_response(up):
                return up
            if cadence[index] > 0:
                await asyncio.sleep(cadence[index] / 1000.0)
        return orjson.dumps({"typed": text_value, "cadence": cadence}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_pin", annotations=_RW)
async def identity_pin(domain: str, name: str) -> str:
    """Mirage: pin a saved identity (Faz 2) to a canonical domain so
    rotation is deterministic per site. Only identities that exist in the
    Faz 2 store can be pinned; the map lives at ~/.config/kahin/pins.json.
    Returns {pinned, domain, name}."""
    tool = "kahin_identity_pin"
    domain_value, error = _text_arg(domain, tool=tool, field="domain", maximum=253)
    if error:
        return error
    name_value, error = _text_arg(name, tool=tool, field="name", maximum=64)
    if error:
        return error
    assert domain_value is not None and name_value is not None
    # Lazy import mirrors pilot.py's precedent: agent_mirage pulls the DOM
    # stream tooling, which this module must not load at import time.
    from kahin.tools.agent_mirage import _identity_path  # noqa: PLC0415

    path = _identity_path(name_value)
    if path is None or not path.is_file():
        return _json_error(tool, f"unknown identity: {name_value!r}", "invalid_argument", field="name")
    async with _healer_ref.safe(tool, domain=domain_value, name=name_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error(tool, "invalid domain", "invalid_argument", field="domain")
        try:
            failed = pin_identity(domain_value, name_value)
        except OSError as exc:
            return _json_error(tool, f"cannot write pins: {exc}", "tool_failed")
        if failed:
            return _json_error(tool, failed["error"], failed["code"], field="domain")
        return orjson.dumps({
            "pinned": True, "domain": normalized, "name": name_value,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_unpin", annotations=_DW)
async def identity_unpin(domain: str) -> str:
    """Mirage: remove the identity pin for a domain (rotation policy
    change). Destructive only to the pin mapping, never to identities or
    the engine. Returns {unpinned, domain}."""
    tool = "kahin_identity_unpin"
    domain_value, error = _text_arg(domain, tool=tool, field="domain", maximum=253)
    if error:
        return error
    assert domain_value is not None
    async with _healer_ref.safe(tool, domain=domain_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error(tool, "invalid domain", "invalid_argument", field="domain")
        try:
            failed = unpin_identity(domain_value)
        except OSError as exc:
            return _json_error(tool, f"cannot write pins: {exc}", "tool_failed")
        if failed:
            return _json_error(tool, failed["error"], failed["code"], field="domain")
        return orjson.dumps({
            "unpinned": True, "domain": normalized,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_pins", annotations=_RO)
async def identity_pins() -> str:
    """Mirage: list the current per-domain identity pin map
    ({pins: {domain: name}}). Read-only."""
    async with _healer_ref.safe("kahin_identity_pins"):
        return orjson.dumps({"pins": load_pins()}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_for_domain", annotations=_RO)
async def identity_for_domain(domain: str) -> str:
    """Mirage: report which saved identity is pinned to a domain, plus an
    actionable next step ({domain, name|null, hint}). Read-only."""
    tool = "kahin_identity_for_domain"
    domain_value, error = _text_arg(domain, tool=tool, field="domain", maximum=253)
    if error:
        return error
    assert domain_value is not None
    async with _healer_ref.safe(tool, domain=domain_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error(tool, "invalid domain", "invalid_argument", field="domain")
        name = load_pins().get(normalized)
        hint = (
            f"start with: kahin_browser_start(identity={name!r})"
            if name
            else "no pin — rotate freely"
        )
        return orjson.dumps({
            "domain": normalized, "name": name, "hint": hint,
        }, option=orjson.OPT_INDENT_2).decode()


_FINGERPRINT_REPORT_JS = r"""
((tz, gl) => ({
  userAgent: navigator.userAgent,
  platform: navigator.platform,
  oscpu: navigator.oscpu || "",
  languages: navigator.languages || [],
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory,
  timezone: tz,
  locale: (navigator.language || ""),
  screen: {width: screen.width, height: screen.height, colorDepth: screen.colorDepth},
  viewport: {width: innerWidth, height: innerHeight},
  webgl: gl,
}))(
  (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ""; } })(),
  (() => {
    try {
      const c = document.createElement("canvas");
      const g = c.getContext("webgl") || c.getContext("experimental-webgl");
      if (!g) return {available: false, vendor: null, renderer: null, reason: "context_unavailable"};
      const e = g.getExtension("WEBGL_debug_renderer_info");
      return {
        available: true,
        vendor: e ? g.getParameter(e.UNMASKED_VENDOR_WEBGL) : null,
        renderer: e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : null,
        debugInfo: Boolean(e),
      };
    } catch (e) {
      return {available: false, vendor: null, renderer: null, reason: "context_error"};
    }
  })()
)
"""


@mcp.tool(name="kahin_fingerprint_report", annotations=_RO)
async def fingerprint_report(frame_id: str | None = None) -> str:
    """Mirage: evaluate the REAL current page and report the fingerprint a
    site would observe — userAgent, platform, oscpu, languages,
    hardwareConcurrency, deviceMemory, timezone, locale, screen, viewport
    and WebGL vendor/renderer. Evidence always comes from live page
    evaluation, never from an emulation command acknowledgement."""
    async with _healer_ref.safe("kahin_fingerprint_report", frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_fingerprint_report")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _safe_mirage_eval_result(
            "kahin_fingerprint_report", _FINGERPRINT_REPORT_JS, frame_id, session_id=session_id,
        )
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return _json_error(
                "kahin_fingerprint_report",
                "JavaScript evaluation failed",
                "javascript_error",
            )
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        if not isinstance(parsed, dict):
            return _json_error(
                "kahin_fingerprint_report",
                "report evaluate returned no value",
                "invalid_engine_response",
            )
        return orjson.dumps({
            "engine": "mirage",
            "summary": parsed,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_proxy_resolve", annotations=_RO)
async def proxy_resolve(proxy_url: str, timeout: float = 5.0) -> str:
    """Resolve a proxy's exit IP geo THROUGH the proxy (http/https; socks4/
    socks5 need the optional socksio package) and recommend matching
    timezone/locale/geolocation overrides. Credentials in the URL are never
    echoed back. Failures are structured with code=proxy_resolve_failed."""
    proxy_value, error = _text_arg(
        proxy_url, tool="kahin_proxy_resolve", field="proxy_url", maximum=1_024,
    )
    if error:
        return error
    timeout_value = _bounded_float(timeout, minimum=1.0, maximum=30.0, default=5.0)
    redacted = _redact_proxy(proxy_value)
    async with _healer_ref.safe(
        "kahin_proxy_resolve", proxy_url=redacted, timeout=timeout_value,
    ):
        try:
            proxy_env(proxy_value)
        except ValueError as exc:
            return _json_error(
                "kahin_proxy_resolve", str(exc), "invalid_argument", field="proxy_url",
            )
        geo = await asyncio.to_thread(resolve_proxy_geo, proxy_value, timeout_value)
        if geo.get("error"):
            return orjson.dumps({
                "proxy": redacted,
                **geo,
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "proxy": redacted,
            "geo": geo,
            "recommended": {
                "timezone": geo.get("timezone") or None,
                "locale": geo.get("country_code") or None,
                "geolocation": {
                    "latitude": geo.get("latitude"),
                    "longitude": geo.get("longitude"),
                },
            },
            "hint": "pass proxy=... to kahin_browser_start; set timezone/locale/geolocation with the emulation tools",
        }, option=orjson.OPT_INDENT_2).decode()
