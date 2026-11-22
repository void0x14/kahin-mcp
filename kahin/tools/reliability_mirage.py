"""Reliability-oriented Mirage tools.

These tools keep the public MCP contract JSON-string based while composing the
shared locator, actionability, and retrying assertion primitives.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import re
from typing import Any, Callable

import orjson

from kahin._mcp import mcp
from kahin.actionability import wait_for_ready
from kahin.expect import (
    ExpectationError,
    attribute_js,
    enabled,
    expect_until,
    has_attribute,
    has_text,
    has_value,
    text_js,
    value_js,
    visible,
)
from kahin.locators import selector_js
from kahin.tools._common import _DW, _RO, _RW, _healer_ref, _mirage_engine
from kahin.tools.pilot_mirage import (
    _MAX_INPUT_TEXT_LENGTH,
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _bounded_float,
    _bounded_int,
    _capture_page_session,
    _dispatch_mouse,
    _element_point,
    _frame_alive_live,
    _frame_context_changed,
    _frame_context_id,
    _frame_nav_seq,
    _frame_parent_map,
    _is_error_response,
    _json_error,
    _probe_result_or_abort,
    _safe_mirage_eval_result,
    _text_arg,
    _validate_frame_id,
)


def _parsed_value(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        return {"error": "probe_failed"}
    if not isinstance(result, dict):
        return {"error": "probe_failed"}
    value = result.get("result") or {}
    parsed = value.get("value") if isinstance(value, dict) else None
    return parsed if isinstance(parsed, dict) else {"error": "probe_failed"}


async def _ensure_frame_exists(tool: str, frame_id: str | None, session_id: str) -> str | None:
    if frame_id is None:
        return None
    if _mirage_engine().resolve_context(frame_id, session_id=session_id) is not None:
        return None
    try:
        tree_res = await _mirage_engine().call("Page.getFrameTree", session_id=session_id)
        parents = _frame_parent_map(tree_res.get("frameTree") if isinstance(tree_res, dict) else None)
    except Exception:  # noqa: BLE001
        parents = {}
    if frame_id not in parents:
        return _json_error(tool, f"frame {frame_id!r} not found in frame tree", "frame_not_found", frame_id=frame_id)
    return None


async def _probe_for(tool: str, frame_id: str | None, session_id: str):
    async def probe(expression: str) -> dict[str, Any]:
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        return _parsed_value(result)

    return probe


def _state_js(selector: str, state: str) -> str:
    element = selector_js(selector)
    if state == "visible":
        return (
            "(() => { const el = "
            + element
            + "; if (!el) return {ok:false, code:'element_not_found'}; "
            + "const r=el.getBoundingClientRect(), s=getComputedStyle(el); "
            + "return {ok:r.width>0 && r.height>0 && s.display !== 'none' && s.visibility !== 'hidden'}; })()"
        )
    return (
        "(() => { const el = "
        + element
        + "; if (!el) return {ok:false, code:'element_not_found'}; "
        + "return {ok: !(el.disabled === true || (el.matches && el.matches(':disabled')) || el.getAttribute('aria-disabled') === 'true')}; })()"
    )


def _expect_expression(
    selector: str,
    *,
    state: str | None,
    wants_text: bool,
    wants_value: bool,
    attribute: str | None,
) -> str:
    parts: list[str] = []
    if state:
        parts.append(f"ok: ({_state_js(selector, state)}) .ok")
    if wants_text:
        parts.append(f"text: {text_js(selector)}")
    if wants_value:
        parts.append(f"value: {value_js(selector)}")
    if attribute:
        parts.append(f"attributes: {{ {orjson.dumps(attribute).decode()}: {attribute_js(selector, attribute)} }}")
    return "({ " + ", ".join(parts or ["ok: false"]) + " })"


@mcp.tool(name="kahin_mirage_expect", annotations=_RO)
async def mirage_expect(
    selector: str,
    state: str | None = None,
    text: str | None = None,
    value: str | None = None,
    attribute: str | None = None,
    attribute_value: str | None = None,
    timeout: float = 5.0,
    frame_id: str | None = None,
) -> str:
    """Retry a visible/enabled/text/value/attribute assertion until timeout."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_expect", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    assert selector_value is not None
    if not selector_value:
        return _json_error("kahin_mirage_expect", "selector must not be empty", "invalid_argument", field="selector")
    if frame_error := _validate_frame_id(frame_id, "kahin_mirage_expect"):
        return frame_error
    if state is not None and state not in {"visible", "enabled"}:
        return _json_error("kahin_mirage_expect", "state must be visible or enabled", "invalid_argument", field="state", received=state)
    if attribute_value is not None and attribute is None:
        return _json_error("kahin_mirage_expect", "attribute_value requires attribute", "invalid_argument", field="attribute_value")
    optional_values: dict[str, str | None] = {}
    for field, candidate, maximum in (
        ("text", text, _MAX_INPUT_TEXT_LENGTH),
        ("value", value, _MAX_INPUT_TEXT_LENGTH),
        ("attribute", attribute, _MAX_INPUT_TEXT_LENGTH),
        ("attribute_value", attribute_value, _MAX_INPUT_TEXT_LENGTH),
    ):
        checked, check_error = _text_arg(candidate, tool="kahin_mirage_expect", field=field, maximum=maximum, allow_none=True)
        if check_error:
            return check_error
        optional_values[field] = checked
    text_value = optional_values["text"]
    value_value = optional_values["value"]
    attribute_value_name = optional_values["attribute"]
    attribute_expected = optional_values["attribute_value"]
    if not (state or text_value is not None or value_value is not None or attribute_value_name is not None):
        return _json_error("kahin_mirage_expect", "one of state/text/value/attribute is required", "invalid_argument")
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=5.0)
    async with _healer_ref.safe(
        "kahin_mirage_expect", selector=selector_value[:80], state=state, timeout=timeout_value, frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_expect")
        if capture_error:
            return capture_error
        assert session_id is not None
        if missing_frame := await _ensure_frame_exists("kahin_mirage_expect", frame_id, session_id):
            return missing_frame
        expression = _expect_expression(
            selector_value,
            state=state,
            wants_text=text_value is not None,
            wants_value=value_value is not None,
            attribute=attribute_value_name,
        )
        predicates: list[tuple[str, Callable[[dict[str, Any]], bool]]] = []
        if state == "visible":
            predicates.append(("visible", visible()))
        elif state == "enabled":
            predicates.append(("enabled", enabled()))
        if text_value is not None:
            predicates.append((f"text contains {text_value!r}", has_text(text_value)))
        if value_value is not None:
            predicates.append((f"value equals {value_value!r}", has_value(value_value)))
        if attribute_value_name is not None:
            predicates.append((f"attribute {attribute_value_name!r}", has_attribute(attribute_value_name, attribute_expected)))
        probe = await _probe_for("kahin_mirage_expect", frame_id, session_id)
        description = "; ".join(name for name, _ in predicates)
        try:
            actual = await expect_until(
                probe,
                expression,
                lambda state_value: all(predicate(state_value) for _, predicate in predicates),
                timeout=timeout_value,
                description=description,
            )
        except ExpectationError as exc:
            return orjson.dumps({
                "error": str(exc),
                "code": "expectation_failed",
                "selector": selector_value,
                "expected": exc.description,
                "actual": exc.actual,
                "timeout": exc.timeout,
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "matched": True,
            "selector": selector_value,
            "expected": description,
            "actual": actual,
            "timeout": timeout_value,
        }, option=orjson.OPT_INDENT_2).decode()


_CHECKABLE_JS = """
(() => {
  const el = ${ELEMENT_JS};
  if (!el) return {code: "element_not_found"};
  const tag = String(el.tagName || "").toLowerCase();
  const type = String(el.getAttribute && el.getAttribute("type") || "").toLowerCase();
  if (tag !== "input" || (type !== "checkbox" && type !== "radio")) return {code: "not_checkable", tag, type};
  if (el.disabled === true || (el.matches && el.matches(":disabled")) || el.getAttribute("aria-disabled") === "true") return {code: "element_not_actionable", reason: "disabled"};
  return {ok: true, type, checked: Boolean(el.checked)};
})()
"""


async def _check_flow(
    tool: str,
    selector: str,
    want: bool,
    *,
    timeout: float,
    frame_id: str | None,
    session_id: str,
) -> dict[str, Any]:
    async def probe(expression: str) -> dict[str, Any]:
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        return await _probe_result_or_abort(tool, result, frame_id, session_id)

    ready = await wait_for_ready(probe, selector, timeout=timeout)
    if not ready.get("ok"):
        if ready.get("aborted") and isinstance(ready.get("raw"), str):
            return orjson.loads(ready["raw"])
        return {"error": "element is not actionable", "code": ready.get("code") or "timeout", "reason": ready.get("reason")}
    check_js = _CHECKABLE_JS.replace("${ELEMENT_JS}", selector_js(selector))
    kind = await probe(check_js)
    if kind.get("code") == "not_checkable":
        return {"error": "element is not a checkbox or radio", "code": "not_checkable", "selector": selector}
    if kind.get("code"):
        return {"error": str(kind.get("code")), "code": str(kind.get("code")), "selector": selector}
    if not want and kind.get("type") == "radio":
        return {"error": "radio inputs cannot be unchecked", "code": "not_checkable", "selector": selector}
    point = await _element_point(selector, frame_id, session_id=session_id, tool=tool)
    if not isinstance(point, tuple):
        try:
            err_payload = orjson.loads(point)
        except orjson.JSONDecodeError:
            err_payload = None
        if isinstance(err_payload, dict) and err_payload.get("error"):
            return err_payload
        return {"error": "element point failed", "code": "element_not_found", "selector": selector}
    x, y = point
    initial_ctx = _frame_context_id(frame_id, session_id)
    initial_nav = _frame_nav_seq(frame_id, session_id)
    down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=1, session_id=session_id)
    if _is_error_response(down):
        return {"error": "mousedown failed", "code": "tool_failed"}
    if not await _frame_alive_live(frame_id, initial_ctx, session_id, initial_nav_seq=initial_nav):
        return {"error": "target frame detached or navigated during check", "code": "frame_detached", "frame_id": frame_id}
    up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0, session_id=session_id)
    if _is_error_response(up):
        return {"error": "mouseup failed", "code": "tool_failed"}
    changed = False
    current = kind
    for _ in range(20):
        await asyncio.sleep(0.05)
        current = await probe(check_js)
        if current.get("code"):
            break
        if bool(current.get("checked")) == want:
            changed = True
            break
    return {
        "checked": bool(current.get("checked")),
        "changed": changed,
        "selector": selector,
        "x": x,
        "y": y,
    }


async def _session_tool(tool: str, selector: str, timeout: float, frame_id: str | None) -> tuple[str | None, str | None, str | None, float]:
    selector_value, error = _text_arg(selector, tool=tool, field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return None, error, None, timeout
    assert selector_value is not None
    if not selector_value:
        return None, _json_error(tool, "selector must not be empty", "invalid_argument", field="selector"), None, timeout
    if frame_error := _validate_frame_id(frame_id, tool):
        return None, frame_error, None, timeout
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    session_id, capture_error = await _capture_page_session(tool)
    return selector_value, capture_error, session_id, timeout_value


@mcp.tool(name="kahin_mirage_check", annotations=_RW)
async def mirage_check(selector: str, timeout: float = 10.0, return_snapshot: bool = False, frame_id: str | None = None) -> str:
    """Check a checkbox or radio input and verify its final state.
    ``return_snapshot`` appends a fresh ``kahin_mirage_snapshot`` to the result."""
    selector_value, error, session_id, timeout_value = await _session_tool("kahin_mirage_check", selector, timeout, frame_id)
    if error:
        return error
    assert selector_value is not None and session_id is not None
    async with _healer_ref.safe("kahin_mirage_check", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        payload = await _check_flow("kahin_mirage_check", selector_value, True, timeout=timeout_value, frame_id=frame_id, session_id=session_id)
        if not isinstance(payload, dict) or payload.get("error"):
            return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
        if return_snapshot:
            from kahin.tools.agent_mirage import _snapshot_for_session  # noqa: PLC0415
            payload["snapshot"] = orjson.loads(await _snapshot_for_session(session_id, max_tokens=1500, frame_id=frame_id))
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_uncheck", annotations=_RW)
async def mirage_uncheck(selector: str, timeout: float = 10.0, return_snapshot: bool = False, frame_id: str | None = None) -> str:
    """Uncheck a checkbox and verify its final state.
    ``return_snapshot`` appends a fresh ``kahin_mirage_snapshot`` to the result."""
    selector_value, error, session_id, timeout_value = await _session_tool("kahin_mirage_uncheck", selector, timeout, frame_id)
    if error:
        return error
    assert selector_value is not None and session_id is not None
    async with _healer_ref.safe("kahin_mirage_uncheck", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        payload = await _check_flow("kahin_mirage_uncheck", selector_value, False, timeout=timeout_value, frame_id=frame_id, session_id=session_id)
        if not isinstance(payload, dict) or payload.get("error"):
            return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
        if return_snapshot:
            from kahin.tools.agent_mirage import _snapshot_for_session  # noqa: PLC0415
            payload["snapshot"] = orjson.loads(await _snapshot_for_session(session_id, max_tokens=1500, frame_id=frame_id))
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_select_option", annotations=_RW)
async def mirage_select_option(
    selector: str,
    value: str | None = None,
    label: str | None = None,
    index: int | None = None,
    timeout: float = 10.0,
    return_snapshot: bool = False,
    frame_id: str | None = None,
) -> str:
    """Select an option by value, exact visible label, or zero-based index.
    ``return_snapshot`` appends a fresh ``kahin_mirage_snapshot`` to the result."""
    selector_value, error = _text_arg(selector, tool="kahin_mirage_select_option", field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    assert selector_value is not None
    chosen = sum(candidate is not None for candidate in (value, label, index))
    if chosen != 1:
        return _json_error("kahin_mirage_select_option", "exactly one of value/label/index is required", "invalid_argument")
    for field, candidate in (("value", value), ("label", label)):
        if candidate is not None:
            _, error = _text_arg(candidate, tool="kahin_mirage_select_option", field=field, maximum=_MAX_INPUT_TEXT_LENGTH)
            if error:
                return error
    if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
        return _json_error("kahin_mirage_select_option", "index must be a non-negative integer", "invalid_argument", field="index")
    if frame_error := _validate_frame_id(frame_id, "kahin_mirage_select_option"):
        return frame_error
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_select_option", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_select_option")
        if capture_error:
            return capture_error
        assert session_id is not None

        async def probe(expression: str) -> dict[str, Any]:
            result = await _safe_mirage_eval_result("kahin_mirage_select_option", expression, frame_id, session_id=session_id)
            return await _probe_result_or_abort("kahin_mirage_select_option", result, frame_id, session_id)

        ready = await wait_for_ready(probe, selector_value, timeout=timeout_value)
        if not ready.get("ok"):
            if ready.get("aborted") and isinstance(ready.get("raw"), str):
                return ready["raw"]
            return _json_error("kahin_mirage_select_option", "element is not actionable", str(ready.get("code") or "timeout"), selector=selector_value)
        criterion = "value" if value is not None else "label" if label is not None else "index"
        wanted: Any = value if value is not None else label if label is not None else index
        expression = (
            "(() => { const el = " + selector_js(selector_value) + "; "
            "if (!el) return {code:'element_not_found'}; "
            "if (String(el.tagName).toLowerCase() !== 'select') return {code:'not_select'}; "
            f"const wanted = {orjson.dumps(wanted).decode()}; let option = null; "
            f"if ({orjson.dumps(criterion).decode()} === 'index') option = el.options[Number(wanted)] || null; "
            f"else if ({orjson.dumps(criterion).decode()} === 'label') option = Array.from(el.options).find(o => o.textContent.trim() === wanted) || null; "
            "else option = Array.from(el.options).find(o => o.value === wanted) || null; "
            "if (!option) return {code:'option_not_found', wanted}; "
            "el.value = option.value; el.dispatchEvent(new Event('input',{bubbles:true})); "
            "el.dispatchEvent(new Event('change',{bubbles:true})); "
            "return {selected: el.value, label: option.textContent.trim()}; })()"
        )
        result = await _safe_mirage_eval_result("kahin_mirage_select_option", expression, frame_id, session_id=session_id)
        parsed = _parsed_value(result)
        if parsed.get("code"):
            code = str(parsed["code"])
            return _json_error("kahin_mirage_select_option", code, code, selector=selector_value, wanted=parsed.get("wanted"))
        payload = {"selected": parsed.get("selected"), "label": parsed.get("label"), "selector": selector_value}
        if return_snapshot:
            from kahin.tools.agent_mirage import _snapshot_for_session  # noqa: PLC0415
            payload["snapshot"] = orjson.loads(await _snapshot_for_session(session_id, max_tokens=1500, frame_id=frame_id))
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_dblclick", annotations=_DW)
async def mirage_dblclick(selector: str, timeout: float = 10.0, return_snapshot: bool = False, frame_id: str | None = None) -> str:
    """Double-click a live, actionable element with real mouse events.
    ``return_snapshot`` appends a fresh ``kahin_mirage_snapshot`` to the result."""
    selector_value, error, session_id, timeout_value = await _session_tool("kahin_mirage_dblclick", selector, timeout, frame_id)
    if error:
        return error
    assert selector_value is not None and session_id is not None
    async with _healer_ref.safe("kahin_mirage_dblclick", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        async def probe(expression: str) -> dict[str, Any]:
            result = await _safe_mirage_eval_result("kahin_mirage_dblclick", expression, frame_id, session_id=session_id)
            return await _probe_result_or_abort("kahin_mirage_dblclick", result, frame_id, session_id)

        ready = await wait_for_ready(probe, selector_value, timeout=timeout_value)
        if not ready.get("ok"):
            if ready.get("aborted") and isinstance(ready.get("raw"), str):
                return ready["raw"]
            return _json_error("kahin_mirage_dblclick", "element is not actionable", str(ready.get("code") or "timeout"), selector=selector_value)
        point = await _element_point(selector_value, frame_id, session_id=session_id, tool="kahin_mirage_dblclick")
        if not isinstance(point, tuple):
            return point
        x, y = point
        initial_ctx = _frame_context_id(frame_id, session_id)
        initial_nav = _frame_nav_seq(frame_id, session_id)
        for click_count in (1, 2):
            if click_count == 2 and not await _frame_alive_live(frame_id, initial_ctx, session_id, initial_nav_seq=initial_nav):
                return _json_error("kahin_mirage_dblclick", "target frame detached or navigated during dblclick", "frame_detached", frame_id=frame_id)
            down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=1, click_count=click_count, session_id=session_id)
            if _is_error_response(down):
                return down
            if not await _frame_alive_live(frame_id, initial_ctx, session_id, initial_nav_seq=initial_nav):
                return _json_error("kahin_mirage_dblclick", "target frame detached or navigated during dblclick", "frame_detached", frame_id=frame_id)
            up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0, click_count=click_count, session_id=session_id)
            if _is_error_response(up):
                return up
        payload = {"dblclicked": selector_value, "x": x, "y": y}
        if return_snapshot:
            from kahin.tools.agent_mirage import _snapshot_for_session  # noqa: PLC0415
            payload["snapshot"] = orjson.loads(await _snapshot_for_session(session_id, max_tokens=1500, frame_id=frame_id))
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_drag", annotations=_DW)
async def mirage_drag(selector_from: str, selector_to: str, timeout: float = 10.0, steps: int = 10, return_snapshot: bool = False, frame_id: str | None = None) -> str:
    """Drag from one actionable element to another with bounded mouse steps.
    ``return_snapshot`` appends a fresh ``kahin_mirage_snapshot`` to the result."""
    source, error = _text_arg(selector_from, tool="kahin_mirage_drag", field="selector_from", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    target, error = _text_arg(selector_to, tool="kahin_mirage_drag", field="selector_to", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    assert source is not None and target is not None
    if frame_error := _validate_frame_id(frame_id, "kahin_mirage_drag"):
        return frame_error
    steps_value = _bounded_int(steps, minimum=2, maximum=50, default=10)
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_drag", selector_from=source[:80], selector_to=target[:80], steps=steps_value, timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_drag")
        if capture_error:
            return capture_error
        assert session_id is not None

        async def probe(expression: str) -> dict[str, Any]:
            result = await _safe_mirage_eval_result("kahin_mirage_drag", expression, frame_id, session_id=session_id)
            return await _probe_result_or_abort("kahin_mirage_drag", result, frame_id, session_id)

        for selector in (source, target):
            ready = await wait_for_ready(probe, selector, timeout=timeout_value)
            if not ready.get("ok"):
                if ready.get("aborted") and isinstance(ready.get("raw"), str):
                    return ready["raw"]
                return _json_error("kahin_mirage_drag", "element is not actionable", str(ready.get("code") or "timeout"), selector=selector)
        source_point = await _element_point(source, frame_id, session_id=session_id, tool="kahin_mirage_drag")
        if not isinstance(source_point, tuple):
            return source_point
        x0, y0 = source_point
        initial_ctx = _frame_context_id(frame_id, session_id)
        initial_nav = _frame_nav_seq(frame_id, session_id)
        down = await _dispatch_mouse("mousedown", x0, y0, button=0, buttons=1, session_id=session_id)
        if _is_error_response(down):
            return down
        if not await _frame_alive_live(frame_id, initial_ctx, session_id, initial_nav_seq=initial_nav):
            return _json_error("kahin_mirage_drag", "target frame detached or navigated during drag", "frame_detached", frame_id=frame_id)
        target_point = await _element_point(target, frame_id, session_id=session_id, tool="kahin_mirage_drag")
        if _frame_context_changed(frame_id, initial_ctx, session_id) or _frame_nav_seq(frame_id, session_id) != initial_nav:
            return _json_error("kahin_mirage_drag", "target frame detached or navigated during drag", "frame_detached", frame_id=frame_id)
        if not isinstance(target_point, tuple):
            return target_point
        x1, y1 = target_point
        last_x, last_y = x0, y0
        for step in range(1, steps_value + 1):
            if _frame_context_changed(frame_id, initial_ctx, session_id) or _frame_nav_seq(frame_id, session_id) != initial_nav:
                return _json_error("kahin_mirage_drag", "target frame detached or navigated during drag", "frame_detached", frame_id=frame_id)
            ratio = step / steps_value
            last_x = x0 + (x1 - x0) * ratio
            last_y = y0 + (y1 - y0) * ratio
            move = await _dispatch_mouse("mousemove", last_x, last_y, button=0, buttons=1, session_id=session_id)
            if _is_error_response(move):
                return move
            await asyncio.sleep(0.01)
        if not await _frame_alive_live(frame_id, initial_ctx, session_id, initial_nav_seq=initial_nav):
            return _json_error("kahin_mirage_drag", "target frame detached or navigated during drag", "frame_detached", frame_id=frame_id)
        up = await _dispatch_mouse("mouseup", last_x, last_y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        payload = {"dragged": source, "to": target, "steps": steps_value, "x": last_x, "y": last_y}
        if return_snapshot:
            from kahin.tools.agent_mirage import _snapshot_for_session  # noqa: PLC0415
            payload["snapshot"] = orjson.loads(await _snapshot_for_session(session_id, max_tokens=1500, frame_id=frame_id))
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_wait_for_text", annotations=_RO)
async def mirage_wait_for_text(text: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Wait until the current document body contains the requested text."""
    text_value, error = _text_arg(text, tool="kahin_mirage_wait_for_text", field="text", maximum=_MAX_INPUT_TEXT_LENGTH)
    if error:
        return error
    assert text_value is not None
    if not text_value:
        return _json_error("kahin_mirage_wait_for_text", "text must not be empty", "invalid_argument", field="text")
    if frame_error := _validate_frame_id(frame_id, "kahin_mirage_wait_for_text"):
        return frame_error
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_wait_for_text", text=text_value[:80], timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_wait_for_text")
        if capture_error:
            return capture_error
        assert session_id is not None
        if missing_frame := await _ensure_frame_exists("kahin_mirage_wait_for_text", frame_id, session_id):
            return missing_frame
        expression = (
            "(() => { const t = (document.body ? document.body.innerText : '') || ''; "
            f"return {{present: t.includes({orjson.dumps(text_value).decode()})}}; }})()"
        )
        deadline = asyncio.get_running_loop().time() + timeout_value
        while True:
            raw_result = await _safe_mirage_eval_result("kahin_mirage_wait_for_text", expression, frame_id, session_id=session_id)
            parsed = await _probe_result_or_abort("kahin_mirage_wait_for_text", raw_result, frame_id, session_id)
            if parsed.get("abort") and isinstance(parsed.get("raw"), str):
                return parsed["raw"]
            if parsed.get("present"):
                return orjson.dumps({"found": True, "text": text_value, "timeout": timeout_value}, option=orjson.OPT_INDENT_2).decode()
            if asyncio.get_running_loop().time() >= deadline:
                return orjson.dumps({"found": False, "text": text_value, "timeout": timeout_value, "code": "timeout"}, option=orjson.OPT_INDENT_2).decode()
            await asyncio.sleep(0.1)


@mcp.tool(name="kahin_mirage_wait_for_timeout", annotations=_RO)
async def mirage_wait_for_timeout(ms: int = 1000) -> str:
    """Wait a bounded number of milliseconds and report the actual request."""
    if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= 60_000:
        return _json_error("kahin_mirage_wait_for_timeout", "ms must be an integer in 1..60000", "invalid_argument", field="ms")
    async with _healer_ref.safe("kahin_mirage_wait_for_timeout", ms=str(ms)):
        await asyncio.sleep(ms / 1000.0)
    return orjson.dumps({"waited_ms": ms}, option=orjson.OPT_INDENT_2).decode()


_ROUTE_PATTERN_MAX = 8_192
_ROUTE_FRAME_ID_MAX = 512
_ROUTE_WAIT_MAX = 60_000.0


def _route_matcher(pattern: str) -> Callable[[str], bool]:
    if pattern.startswith("re:"):
        try:
            compiled = re.compile(pattern[3:])
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        return lambda url: bool(compiled.search(url or ""))
    return lambda url: fnmatch.fnmatchcase(url or "", pattern)


@mcp.tool(name="kahin_mirage_route", annotations=_RW)
async def mirage_route(
    pattern: str,
    action: str = "abort",
    method: str | None = None,
    body: str | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content_type: str = "text/plain",
    wait_ms: float = 10_000.0,
    frame_id: str | None = None,
) -> str:
    """Apply one abort/continue/fulfill decision to the next matching request.

    This is a bounded waiting tool: issue it concurrently with the navigate,
    click, or other request-producing tool that should be routed. A serial
    client must use ``kahin_mirage_intercept_requests`` first and decide with
    ``kahin_mirage_network_continue``/``kahin_mirage_network_abort`` after
    reading the intercepted request.
    """
    pattern_value, error = _text_arg("" if pattern is None else pattern, tool="kahin_mirage_route", field="pattern", maximum=_ROUTE_PATTERN_MAX)
    if error:
        return error
    assert pattern_value is not None
    frame_value, error = _text_arg(
        frame_id,
        tool="kahin_mirage_route",
        field="frame_id",
        maximum=_ROUTE_FRAME_ID_MAX,
        allow_none=True,
    )
    if error:
        return error
    if not pattern_value:
        return _json_error("kahin_mirage_route", "pattern must not be empty", "invalid_argument", field="pattern")
    if action not in {"abort", "continue", "fulfill"}:
        return _json_error("kahin_mirage_route", "action must be abort|continue|fulfill", "invalid_argument", field="action", received=action)
    if action == "fulfill" and not isinstance(body, str):
        return _json_error("kahin_mirage_route", "fulfill requires a body string", "invalid_argument", field="body")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return _json_error("kahin_mirage_route", "status must be an integer in 100..599", "invalid_argument", field="status")
    if headers is not None and (not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())):
        return _json_error("kahin_mirage_route", "headers must be an object of string values", "invalid_argument", field="headers")
    if method is not None and (not isinstance(method, str) or any(char.isspace() for char in method)):
        return _json_error("kahin_mirage_route", "method must be a non-whitespace string", "invalid_argument", field="method")
    checked_content_type, error = _text_arg(content_type, tool="kahin_mirage_route", field="content_type", maximum=256)
    if error:
        return error
    wait_value = _bounded_float(wait_ms, minimum=0.0, maximum=_ROUTE_WAIT_MAX, default=10_000.0) / 1000.0
    try:
        matcher = _route_matcher(pattern_value)
    except ValueError as exc:
        return _json_error("kahin_mirage_route", str(exc), "invalid_argument", field="pattern")
    async with _healer_ref.safe("kahin_mirage_route", pattern=pattern_value[:80], action=action, wait_ms=wait_value * 1000, frame_id=frame_value):
        session_id, capture_error = await _capture_page_session("kahin_mirage_route")
        if capture_error:
            return capture_error
        assert session_id is not None
        engine = _mirage_engine()
        route_started = asyncio.get_running_loop().time()
        # The route is one-shot but self-sufficient: make sure the page pauses
        # requests before waiting, so a caller does not need a separate
        # mirage_intercept_requests() first (Faz 1 Task 11 Step 4).
        try:
            await engine.call("Network.setRequestInterception", {"enabled": True}, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - public tool returns JSON
            return _json_error(
                "kahin_mirage_route",
                f"could not enable request interception: {exc}",
                "route_interception_failed",
            )

        async def disable_interception() -> None:
            # ``kahin_mirage_route`` is explicitly one-shot. Leaving the
            # page in interception mode after a match or timeout pauses every
            # later crawl request until another tool happens to resume it.
            try:
                await engine.call(
                    "Network.setRequestInterception",
                    {"enabled": False},
                    session_id=session_id,
                )
            except Exception:  # noqa: BLE001 - cleanup must not mask the route result
                pass

        def matches(event: dict[str, Any]) -> bool:
            # Routes are page-session scoped. Root/browser events and another
            # tab's request must never satisfy a route armed for this page.
            if event.get("session_id") != session_id:
                return False
            params = event.get("params") or {}
            request = params.get("request") or {}
            url = params.get("url") or request.get("url")
            request_method = params.get("method") or request.get("method")
            request_frame = params.get("frameId") or request.get("frameId")
            if frame_value is not None and request_frame != frame_value:
                return False
            return matcher(str(url or "")) and (method is None or str(request_method or "").upper() == method.upper())

        try:
            event = await engine.wait_for_intercepted(
                matches,
                wait_value,
                after_monotonic=route_started,
            )
        except Exception:
            await disable_interception()
            raise
        if event is None:
            await disable_interception()
            return orjson.dumps({"matched": False, "code": "timeout", "timeout": wait_value * 1000, "pattern": pattern_value}, option=orjson.OPT_INDENT_2).decode()
        params = event.get("params") or {}
        request = params.get("request") or {}
        request_id = params.get("requestId")
        url = params.get("url") or request.get("url") or ""
        owner_session = event.get("session_id") or session_id
        if not isinstance(request_id, str) or not request_id:
            await disable_interception()
            return _json_error("kahin_mirage_route", "intercepted event has no requestId", "invalid_engine_response")
        call_params: dict[str, Any] = {"requestId": request_id}
        try:
            if action == "abort":
                await engine.call(
                    "Network.abortInterceptedRequest",
                    {"requestId": request_id, "errorCode": "NS_ERROR_ABORT"},
                    session_id=owner_session,
                )
            elif action == "continue":
                if method is not None:
                    call_params["method"] = method
                await engine.call("Network.resumeInterceptedRequest", call_params, session_id=owner_session)
            else:
                response_headers = [{"name": "Content-Type", "value": checked_content_type or "text/plain"}]
                response_headers.extend({"name": name, "value": value} for name, value in (headers or {}).items())
                fulfill_params = {
                    "requestId": request_id,
                    "status": status,
                    "statusText": "OK" if status < 400 else "Kahin route",
                    "headers": response_headers,
                    "base64body": base64.b64encode((body or "").encode("utf-8")).decode("ascii"),
                }
                await engine.call("Network.fulfillInterceptedRequest", fulfill_params, session_id=owner_session)
        except Exception as exc:  # noqa: BLE001 - public tool returns JSON
            await disable_interception()
            return orjson.dumps({
                "matched": True,
                "requestId": request_id,
                "url": url,
                "action": action,
                "error": f"{type(exc).__name__}: {exc}",
                "code": "route_action_failed",
            }, option=orjson.OPT_INDENT_2).decode()
        await disable_interception()
        return orjson.dumps({
            "matched": True,
            "url": url,
            "requestId": request_id,
            "action": action,
            "method": request.get("method") or params.get("method"),
        }, option=orjson.OPT_INDENT_2).decode()
