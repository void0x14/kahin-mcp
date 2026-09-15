"""cf_clear_mirage.py — embedded Cloudflare challenge solver (Mirage/Camoufox).

No external browser, no cookie cache, no replay proxy. Everything happens
inside Kahin's already-open session:

1. navigate to the URL,
2. probe with the same ``_CHALLENGE_STATUS_JS`` contract ``challenge_status``
   uses (no challenge -> return immediately),
3. wait out non-interactive managed challenges (they self-resolve),
4. click an interactive Turnstile checkbox human-like (Bézier travel +
   jittered press delay + real down/up) when one is on screen, then verify.

Live-verified findings baked into this design (nopecha.com/demo/cloudflare,
observed via Kahin screencast + a11y tree, not inferred from DOM dumps):

- The Turnstile iframe has an EMPTY url in ``Page.getFrameTree`` and its
  internals hide in a CLOSED shadow root: ``document.querySelector('iframe')``
  returns nothing, ``querySelectorAll('*')`` shows no ``shadowRoot``, and the
  a11y tree sees only an "internal frame" node with no coordinates. URL-based
  frame filtering and in-iframe checkbox JS can NEVER find it.
- The only measurable anchor is the widget mount div (``div#lVJB5``-style:
  x matches the visible widget box pixel-for-pixel). The checkbox sits at
  the mount box's left-center (~7% from left edge, vertically centered).
- Synthetic ``Page.dispatchMouseEvent`` without cursor travel makes CF
  rotate the Ray ID (bot signal). Travel + press delay are required.
- ``Browser.getCookies`` returns the WHOLE persistent profile (httpOnly
  included), so ``cf_clearance`` may belong to another site. A cookie match
  is only meaningful when its domain covers the target host AND
  ``document.cookie`` (page context) agrees. Ground truth for ``cleared``
  is the page itself (title gate), never the cookie jar.

Unsolvable states (block page, third-party image/audio CAPTCHA, CF refusing
clicks / rotating Ray IDs, timeout) return ``cleared: false`` with evidence
and the same ``pause_for_human`` contract ``challenge_status`` uses — never
hammer, never guess.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.humanize import bezier_trajectory, jittered_delay
from kahin.tools._common import (
    _RO,
    _RW,
    _healer_ref,
    _mirage_engine,
    _mirage_eval_result,
)
from kahin.tools.agent_mirage import _CHALLENGE_STATUS_JS
from kahin.tools.pilot_mirage import (
    _capture_page_session,
    _dispatch_mouse,
    _is_error_response,
    _json_error,
    get_last_mouse_position,
)

_TOOL = "kahin_cf_clear"

# Bounds: every wait is finite, every retry counted. Defaults cover a slow
# managed challenge (JS proof-of-work + a Turnstile click + settle).
_MAX_URL_LENGTH = 4096
_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 180.0
_POLL_SECONDS = 3.0
_INITIAL_SETTLE_SECONDS = 5.0
_MAX_CLICKS = 5
# Human-like press: travel the cursor (Bézier) then hold before release.
_PRESS_DELAY_MS = 120.0
_PRESS_JITTER_MS = 40.0

# Block-page markers. "cloudflare ray id" alone is NOT a signal — legit
# footers carry it. Mirrors the Türk repo's _BLOCK_MARKERS contract.
_BLOCK_MARKERS = (
    "you have been blocked",
    "sorry, you have been blocked",
    "error 1020",
    "access denied",
)

# Runs in the PAGE (not the iframe — the iframe is unreachable): finds the
# Turnstile mount div. The widget's closed shadow root renders INSIDE this
# box, so its rect is the only measurable anchor. Candidates are divs in the
# left half whose size fits a Turnstile box (~300x65 live-observed; bounds
# are deliberately loose) and which contain the cf-turnstile-response input.
_FIND_MOUNT_JS = """(() => {
    const out = [];
    for(const d of document.querySelectorAll('div')){
        if(!d.querySelector('input[name=cf-turnstile-response]')) continue;
        const r = d.getBoundingClientRect();
        // Width is NOT bounded: the mount stretches with the viewport
        // (300px at 1424w, 896px at 1920w live-observed). The response
        // input + sane height already disambiguate; innermost wins below.
        if(r.width < 150 || r.height < 30 || r.height > 200) continue;
        if(r.x < 0 || r.y < 50) continue;
        out.push({x:r.x, y:r.y, w:r.width, h:r.height});
    }
    // innermost first: nested mount wrappers collapse to the same box
    out.sort((a,b) => (a.w*a.h) - (b.w*b.h));
    return out.slice(0,3);
})()"""

# Checkbox geometry, live-measured at two viewports (1424x771 and
# 1920x~920, modlens-verified): the visible widget (~300px) sits at the
# mount div's LEFT edge, checkbox centre ~19px right of it, vertically
# centred. A fraction would drift as the mount stretches (299px -> 896px
# wide); a pixel offset does not (19px vs 18px observed).
_CHECKBOX_LEFT_PX = 19.0

# Page-level bypass probe: title gate + block markers. Returns a dict.
_BLOCK_MARKERS_JSON = orjson.dumps(list(_BLOCK_MARKERS)).decode()
_IS_BYPASSED_JS = (
    "(() => {"
    "    const title = String(document.title || '').toLowerCase();"
    "    if(title.includes('just a moment')) return {bypassed:false, reason:'title'};"
    "    const html = String(document.documentElement ? document.documentElement.innerHTML : '')"
    ".slice(0, 60000).toLowerCase();"
    "    if(html.includes('please complete the captcha')) return {bypassed:false, reason:'captcha-text'};"
    f"    const markers = {_BLOCK_MARKERS_JSON};"
    "    for(const m of markers){ if(html.includes(m)) return {bypassed:false, reason:'block:'+m}; }"
    "    return {bypassed:true};"
    "})()"
)


def _dump(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()


async def _eval_value(expression: str, session_id: str, frame_id: str | None = None) -> Any | None:
    """Evaluate and unwrap ``result.value``; None on any failure."""
    raw = await _mirage_eval_result(expression, frame_id, session_id=session_id)
    if isinstance(raw, str) or not isinstance(raw, dict):
        return None
    if raw.get("exceptionDetails"):
        return None
    return (raw.get("result") or {}).get("value")


async def _challenge_probe(session_id: str) -> dict[str, Any] | None:
    value = await _eval_value(_CHALLENGE_STATUS_JS, session_id)
    return value if isinstance(value, dict) else None


async def _is_bypassed(session_id: str) -> bool:
    value = await _eval_value(_IS_BYPASSED_JS, session_id)
    return bool(isinstance(value, dict) and value.get("bypassed"))


async def _is_blocked(session_id: str) -> str | None:
    """Block-page reason string, or None when the page is not a block page."""
    value = await _eval_value(_IS_BYPASSED_JS, session_id)
    if isinstance(value, dict) and not value.get("bypassed"):
        reason = value.get("reason")
        if isinstance(reason, str) and reason.startswith("block:"):
            return reason[len("block:"):]
    return None


async def _turnstile_mount(session_id: str) -> dict[str, float] | None:
    """Page-space rect of the Turnstile mount div, or None when absent.

    The mount div (holding ``input[name=cf-turnstile-response]``) is the
    only JS-visible anchor: the widget renders in a closed shadow root /
    opaque iframe inside this box.
    """
    boxes = await _eval_value(_FIND_MOUNT_JS, session_id)
    if not isinstance(boxes, list) or not boxes:
        return None
    box = boxes[0]
    if not isinstance(box, dict):
        return None
    try:
        x, y = float(box.get("x") or 0), float(box.get("y") or 0)
        w, h = float(box.get("w") or 0), float(box.get("h") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


async def _click_turnstile(session_id: str) -> bool:
    """Human-like click on the Turnstile checkbox. True if dispatched.

    Travels the cursor along a jittered Bézier to the checkbox point
    (mount-box left + fraction, vertical centre), presses with a jittered
    hold, releases. Ray-ID rotation after a click means CF rejected it —
    the caller detects that via title/cookie, not here.
    """
    mount = await _turnstile_mount(session_id)
    if mount is None:
        return False
    tx = mount["x"] + _CHECKBOX_LEFT_PX
    ty = mount["y"] + mount["h"] / 2
    start_x, start_y = get_last_mouse_position()
    for px, py in bezier_trajectory(start_x, start_y, tx, ty, steps=24, jitter=2.0):
        move = await _dispatch_mouse("mousemove", px, py, session_id=session_id)
        if _is_error_response(move):
            return False
    down = await _dispatch_mouse("mousedown", tx, ty, buttons=1, session_id=session_id)
    if _is_error_response(down):
        return False
    await asyncio.sleep(jittered_delay(_PRESS_DELAY_MS, _PRESS_JITTER_MS) / 1000.0)
    up = await _dispatch_mouse("mouseup", tx, ty, session_id=session_id)
    return not _is_error_response(up)


async def _cf_cookies(session_id: str, host: str) -> dict[str, str]:
    """Cloudflare cookies (cf_ / __cf prefix) scoped to the target host.

    ``Browser.getCookies`` returns the whole persistent profile (every
    site's httpOnly cookies included), so an unscoped match proves nothing.
    A cookie counts only when its domain covers ``host`` — and even then it
    is supporting evidence, not clearance: page context (``document.cookie``)
    must agree and the title gate must pass.
    """
    engine = _mirage_engine()
    try:
        payload = await engine.call("Browser.getCookies", {}, session_id=session_id)
    except Exception:  # noqa: BLE001
        return {}
    cookies = (payload or {}).get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list):
        return {}
    host = host.lower()
    out: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if not (isinstance(name, str) and isinstance(value, str)):
            continue
        if not name.startswith(("cf_", "__cf")):
            continue
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        out[name] = value
    return out


async def _page_cf_cookie_names(session_id: str) -> list[str]:
    """cf_ cookies visible in page context (non-httpOnly, this document)."""
    value = await _eval_value(
        "document.cookie.split(';').map(c=>c.trim().split('=')[0]).filter(n=>/cf_/i.test(n))",
        session_id,
    )
    return [str(n) for n in value] if isinstance(value, list) else []


async def _ray_id(session_id: str) -> str | None:
    """CF Ray ID from page text; None when unreadable. Rotation detector."""
    value = await _eval_value(
        "(document.body ? (document.body.innerText.match(/Ray ID:\\s*([0-9a-f]+)/i) || [])[1] : null)",
        session_id,
    )
    return str(value) if isinstance(value, str) and value else None


@mcp.tool(name="kahin_cf_clear", annotations=_RW)
async def cf_clear(url: str, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Clear a Cloudflare challenge in the current session (embedded solver).

    Navigates to ``url`` inside Kahin's already-open Camoufox browser, waits
    out non-interactive managed challenges, human-like clicks an interactive
    Turnstile checkbox when one is on screen, and verifies the page itself
    opened (title gate). No second browser, no cookie cache, no replay proxy.

    Returns ``{cleared, method, url, cfCookies, elapsedMs}``. ``cleared`` is
    true ONLY when the interstitial title is gone; host-scoped cf_ cookies
    are reported as supporting evidence. CF may refuse clicks (Ray-ID
    rotation) — then ``cleared`` is false with evidence and the
    ``pause_for_human`` contract ``challenge_status`` uses.
    """
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        return _json_error(_TOOL, "url must be a non-empty string", "invalid_argument", field="url")
    try:
        budget = float(timeout)
    except (TypeError, ValueError, OverflowError):
        budget = _DEFAULT_TIMEOUT
    import math as _math
    if not _math.isfinite(budget):
        budget = _DEFAULT_TIMEOUT
    budget = max(5.0, min(_MAX_TIMEOUT, budget))
    from urllib.parse import urlparse as _urlparse
    try:
        host = (_urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 - unparsable URL has no cookie scope
        host = ""
    if not host:
        return _json_error(_TOOL, "url must include a hostname", "invalid_argument", field="url")
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        return _json_error(_TOOL, "url must be a non-empty string", "invalid_argument", field="url")
    try:
        budget = float(timeout)
    except (TypeError, ValueError, OverflowError):
        budget = _DEFAULT_TIMEOUT
    import math as _math
    if not _math.isfinite(budget):
        budget = _DEFAULT_TIMEOUT
    budget = max(5.0, min(_MAX_TIMEOUT, budget))

    async with _healer_ref.safe(_TOOL, url=url[:80], timeout=budget):
        session_id, error = await _capture_page_session(_TOOL)
        if error:
            return error
        assert session_id is not None
        engine = _mirage_engine()
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            await engine.call("Page.navigate", {"url": url}, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - public tool returns JSON
            return _json_error(_TOOL, f"navigation failed: {exc}", "navigation_failed")

        await asyncio.sleep(_INITIAL_SETTLE_SECONDS)

        # Fast path: no challenge signals AND the interstitial title is gone.
        # Cookie presence alone proves nothing (profile-wide jar, other
        # sites' cookies) — the page is the ground truth.
        probe = await _challenge_probe(session_id)
        if (
            probe is not None
            and not probe.get("detected")
            and await _is_bypassed(session_id)
        ):
            cf = await _cf_cookies(session_id, host)
            return _dump({"cleared": True, "method": "none", "url": url,
                          "cfCookies": sorted(cf), "elapsedMs": elapsed_ms()})

        blocked = await _is_blocked(session_id)
        if blocked is not None:
            return _dump({"cleared": False, "method": "blocked", "url": url,
                          "reason": f"block page ({blocked}) — IP/ASN decision, nothing to click",
                          "action": "stop_and_review_authorization",
                          "elapsedMs": elapsed_ms()})

        clicks = 0
        ray_before = await _ray_id(session_id)
        deadline = started + budget
        while time.monotonic() < deadline:
            # Ground truth: the interstitial title is gone. Cookies are only
            # supporting evidence (host-scoped + page-context agreement).
            if await _is_bypassed(session_id):
                cf = await _cf_cookies(session_id, host)
                page_names = await _page_cf_cookie_names(session_id)
                return _dump({"cleared": True,
                              "method": "click" if clicks else "auto",
                              "url": url, "cfCookies": sorted(cf),
                              "pageCfCookies": sorted(page_names),
                              "clicks": clicks, "elapsedMs": elapsed_ms()})
            if clicks < _MAX_CLICKS and await _click_turnstile(session_id):
                clicks += 1
                await asyncio.sleep(_POLL_SECONDS)
                # CF rejecting the click rotates the Ray ID instead of
                # advancing the challenge. Two rotations = refused.
                ray_after = await _ray_id(session_id)
                if ray_before is not None and ray_after is not None and ray_after != ray_before:
                    ray_before = ray_after
                    if clicks >= 2:
                        cf = await _cf_cookies(session_id, host)
                        return _dump({
                            "cleared": False, "method": "refused", "url": url,
                            "reason": "CF rotated Ray ID after clicks — engine fingerprinted, clicks rejected",
                            "rayId": ray_after, "clicks": clicks,
                            "cfCookies": sorted(cf),
                            "action": "pause_for_human_or_authorized_provider",
                            "elapsedMs": elapsed_ms()})
                continue
            await asyncio.sleep(_POLL_SECONDS)

        cf = await _cf_cookies(session_id, host)
        probe = await _challenge_probe(session_id)
        kind = (probe or {}).get("kind") if isinstance(probe, dict) else None
        return _dump({"cleared": False, "method": "timeout", "url": url,
                      "kind": kind, "cfCookies": sorted(cf), "clicks": clicks,
                      "action": "pause_for_human_or_authorized_provider",
                      "elapsedMs": elapsed_ms()})


@mcp.tool(name="kahin_cf_status", annotations=_RO)
async def cf_status() -> str:
    """Report Cloudflare clearance state of the current page (read-only).

    Returns ``{bypassed, blocked, cfCookies, pageCfCookies, challenge}``
    where ``challenge`` is the live ``challenge_status`` probe payload.
    ``cfCookies`` are host-scoped (current page's host only) — never the
    whole profile jar. Never navigates, never clicks — the observational
    companion to ``kahin_cf_clear``.
    """
    async with _healer_ref.safe("kahin_cf_status"):
        session_id, error = await _capture_page_session("kahin_cf_status")
        if error:
            return error
        assert session_id is not None
        bypassed = await _is_bypassed(session_id)
        blocked = await _is_blocked(session_id)
        page_url = await _eval_value("location.hostname", session_id)
        host = str(page_url).lower() if isinstance(page_url, str) else ""
        cf = await _cf_cookies(session_id, host) if host else {}
        page_names = await _page_cf_cookie_names(session_id)
        probe = await _challenge_probe(session_id)
        return _dump({"bypassed": bypassed, "blocked": blocked,
                      "cfCookies": sorted(cf),
                      "pageCfCookies": sorted(page_names),
                      "hasClearance": "cf_clearance" in cf,
                      "challenge": probe})
