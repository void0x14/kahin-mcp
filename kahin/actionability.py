"""actionability.py — Playwright-style actionability polling.

Checks, in order, on every tick (all inside one page evaluate):

1. attached  — the element exists in the document
2. visible   — has a bounding box and is not display:none / visibility:hidden
3. enabled   — not ``disabled`` and not ``aria-disabled="true"``
4. in-view   — scrolled into view (center)
5. hit-test  — the point under the element center is the element itself
              (elementFromPoint), i.e. not obscured by another layer
6. stable    — the element rect did not change across ``stability``
              consecutive polls (animation/animation-freeze guard)

Only the first element lookup goes through the locator engine; everything
else is a single page-evaluate probe, so a fast page can never block the
poll loop behind multiple round trips.

All non-ok probe states remain retryable because page actionability can change
while an application is settling (for example, a disabled button can become
enabled). The latest failure code and reason are retained for an actionable
timeout response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from kahin.locators import selector_js

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_INTERVAL = 0.1
_DEFAULT_STABILITY = 2

_TRANSIENT_CODES = frozenset({"element_not_found", "probe_error", "timeout"})

# Codes that keep the poll loop running; everything else aborts immediately.
# ${ELEMENT_JS} is replaced with the locator-derived expression. The probe
# returns a plain dict: {ok:true, x, y, width, height} or
# {code, reason?} on failure.
ACTIONABILITY_CHECK_JS = r"""
(() => {
  const el = ${ELEMENT_JS};
  if (!el || !(el instanceof Element)) return {code: "element_not_found"};
  const r = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  if (r.width <= 0 || r.height <= 0 || style.display === "none" || style.visibility === "hidden")
    return {code: "element_not_actionable", reason: "not_visible"};
  if (el.disabled === true || (el.matches && el.matches(":disabled")) || el.getAttribute("aria-disabled") === "true")
    return {code: "element_not_actionable", reason: "disabled"};
  el.scrollIntoView({block: "center", inline: "center"});
  const r2 = el.getBoundingClientRect();
  const x = r2.left + r2.width / 2, y = r2.top + r2.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit && hit !== el && !el.contains(hit))
    return {code: "element_not_actionable", reason: "obscured", hit: String(hit.tagName || "").toLowerCase()};
  return {ok: true, x, y, width: r2.width, height: r2.height};
})()
"""


def _build_probe_js(selector: str) -> str:
    """Compose the full actionability probe expression for a locator string."""
    element_js = selector_js(selector)
    return ACTIONABILITY_CHECK_JS.replace("${ELEMENT_JS}", element_js)


async def wait_for_ready(
    probe: Callable[[str], Awaitable[dict[str, Any]]],
    selector: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    interval: float = _DEFAULT_INTERVAL,
    stability: int = _DEFAULT_STABILITY,
) -> dict[str, Any]:
    """Poll until the element passes actionability checks.

    ``probe`` receives the full JS expression and must return the parsed
    value dict (or ``{"error": ...}`` — treated as not-ready).

    Returns ``{"ok": True, "x", "y"}`` on success, or
    ``{"ok": False, "code", "reason", "selector", "timeout"}``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    expression = _build_probe_js(selector)
    last: dict[str, Any] = {"code": "element_not_found"}
    last_failure: dict[str, Any] | None = None
    stable_hits = 0
    last_point: tuple[float, float, float, float] | None = None
    while True:
        try:
            last = await probe(expression)
        except Exception:  # noqa: BLE001 - probe failures degrade to not-ready
            last = {"code": "probe_error"}
        if not isinstance(last, dict):
            last = {"code": "probe_error"}
        if (
            last.get("ok")
            and isinstance(last.get("x"), (int, float))
            and isinstance(last.get("y"), (int, float))
        ):
            point = (
                float(last["x"]),
                float(last["y"]),
                float(last.get("width", 0.0)),
                float(last.get("height", 0.0)),
            )
            stable_hits = stable_hits + 1 if point == last_point else 1
            last_point = point
            if stability <= 0 or stable_hits >= stability:
                return {"ok": True, "x": point[0], "y": point[1], "selector": selector}
        else:
            if last.get("abort") is True:
                return {
                    "ok": False,
                    "aborted": True,
                    "raw": last.get("raw"),
                    "code": str(last.get("code") or "aborted"),
                    "reason": last.get("reason"),
                    "selector": selector,
                    "timeout": timeout,
                }
            # Actionability can change while the page is settling: a hidden,
            # disabled, or obscured control may become usable on a later tick.
            # Keep polling every non-ok state, but retain the latest useful
            # failure so a timeout remains actionable instead of becoming a
            # generic ``element_not_found`` after a transient probe result.
            code = str(last.get("code") or "timeout")
            previous_code = str((last_failure or {}).get("code") or "")
            if code not in _TRANSIENT_CODES or previous_code in _TRANSIENT_CODES or last_failure is None:
                last_failure = last
        if loop.time() > deadline:
            break
        await asyncio.sleep(max(0.0, interval))
    failure = last_failure or last
    return {
        "ok": False,
        "code": str(failure.get("code") or "timeout"),
        "reason": failure.get("reason"),
        "selector": selector,
        "timeout": timeout,
    }
