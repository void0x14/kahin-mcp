"""cf_clear_mirage.py — embedded Cloudflare challenge solver (Mirage/Camoufox).

No external browser, no cookie cache, no replay proxy. Everything happens
inside Kahin's already-open session:

1. navigate to the URL,
2. probe with the same ``_CHALLENGE_STATUS_JS`` contract ``challenge_status``
   uses (no challenge -> return immediately),
3. wait out non-interactive managed challenges (they self-resolve),
4. click an interactive Turnstile checkbox with native press/release events
   when one is on screen, then re-evaluate to verify.

Trust contract (mirrors the Türk bypasser ``cf_bypasser/core/bypasser.py``):
frames filtered by ``"challenges.cloudflare" in frame.url``, checkbox found
by walking open + closed shadow roots (``el.fakeShadowRoot || el.shadowRoot``)
for ``input[type=checkbox]``, click gated on ``w > 0`` and not ``checked``,
checkbox page coords = frame-element bounding-box origin + in-frame centre,
native ``Page.dispatchMouseEvent`` press/release (no Bézier travel, no press
delay — the reference passes without humanization), re-eval after click where
success = checkbox not found or checked, up to 5 attempts with jittered 3s
poll sleeps after a 5s post-navigation settle.

Ground truth for ``cleared`` is the page itself (title gate) plus
host-scoped ``cf_clearance`` presence as supporting evidence — never cookie
presence alone (``Browser.getCookies`` returns the WHOLE persistent profile,
so a cookie match is only meaningful when its domain covers the target host
AND ``document.cookie`` agrees).

Unsolvable states (block page, third-party image/audio CAPTCHA, CF refusing
clicks, timeout) return ``cleared: false`` with evidence and the same
``pause_for_human`` contract ``challenge_status`` uses — never hammer, never
guess.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.humanize import jittered_delay
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
)

_TOOL = "kahin_cf_clear"

# Bounds: every wait is finite, every retry counted. Defaults cover a slow
# managed challenge (JS proof-of-work + a Turnstile click + settle).
# Mirrors the Türk bypasser constants (cf_bypasser/utils/constants.py):
# DEFAULT_MAX_RETRIES=5, CHALLENGE_SETTLE_SECONDS=5, RETRY_POLL_SECONDS=3.
_MAX_URL_LENGTH = 4096
_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 180.0
_RETRY_POLL_SECONDS = 3.0
_RETRY_POLL_JITTER_SECONDS = 1.0
_INITIAL_SETTLE_SECONDS = 5.0
_MAX_ATTEMPTS = 5

# Frame-URL filter: the Turnstile challenge frame serves from this host
# (cf_bypasser/core/bypasser.py:171 — ``"challenges.cloudflare" in f.url``).
_CF_FRAME_MARKER = "challenges.cloudflare"

# Block-page markers. "cloudflare ray id" alone is NOT a signal — legit
# footers carry it. Mirrors the Türk repo's _BLOCK_MARKERS contract.
_BLOCK_MARKERS = (
    "you have been blocked",
    "sorry, you have been blocked",
    "error 1020",
    "access denied",
)

# Runs INSIDE the Turnstile challenge frame: walks open + closed shadow
# roots (``el.fakeShadowRoot || el.shadowRoot``) for ``input[type=checkbox]``
# and returns its centre relative to the frame viewport. Verbatim mirror of
# the Türk bypasser ``_FIND_CHECKBOX_JS`` (cf_bypasser/core/bypasser.py:58-73).
_FIND_CHECKBOX_JS = """() => {
    function find(root){
        if(!root) return null;
        const direct = root.querySelector && root.querySelector('input[type=checkbox]');
        if(direct) return direct;
        for(const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])){
            const sr = el.fakeShadowRoot || el.shadowRoot;
            if(sr){ const r = find(sr); if(r) return r; }
        }
        return null;
    }
    const cb = find(document);
    if(!cb) return {found:false};
    const r = cb.getBoundingClientRect();
    return {found:true, checked:cb.checked, x:r.x+r.width/2, y:r.y+r.height/2, w:r.width};
}"""

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


def _iter_frame_entries(frame_tree: Any) -> Any:
    """Yield ``(frame_id, url)`` pairs from a ``Page.getFrameTree`` payload."""
    stack = [frame_tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        frame = node.get("frame")
        if isinstance(frame, dict):
            yield frame.get("id"), str(frame.get("url") or "")
        children = node.get("childFrames")
        if isinstance(children, list):
            stack.extend(children)


async def _cf_frame_ids(session_id: str) -> list[str]:
    """Frame ids whose URL serves the Turnstile challenge, or []."""
    engine = _mirage_engine()
    try:
        tree = await engine.call("Page.getFrameTree", session_id=session_id)
    except Exception:  # noqa: BLE001 - no frame tree means no click target
        return []
    frame_tree = tree.get("frameTree") if isinstance(tree, dict) else None
    if not isinstance(frame_tree, dict):
        return []
    return [
        frame_id
        for frame_id, url in _iter_frame_entries(frame_tree)
        if isinstance(frame_id, str) and frame_id and _CF_FRAME_MARKER in url
    ]


async def _checkbox_in_frame(session_id: str, frame_id: str) -> dict[str, float] | None:
    """In-frame checkbox centre (frame-viewport coords), or None.

    Click gate lives here in Python (not in JS) for testability: skip when
    the checkbox is missing, has no width, or is already checked — verbatim
    mirror of cf_bypasser/core/bypasser.py:175.
    """
    raw = await _mirage_eval_result(
        f"({ _FIND_CHECKBOX_JS })()", frame_id, session_id=session_id,
    )
    if isinstance(raw, str) or not isinstance(raw, dict):
        return None
    if raw.get("exceptionDetails"):
        return None
    info = (raw.get("result") or {}).get("value")
    if not isinstance(info, dict) or not info.get("found"):
        return None
    try:
        w = float(info.get("w") or 0)
        x = float(info.get("x") or 0)
        y = float(info.get("y") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or info.get("checked"):
        return None
    return {"x": x, "y": y}


async def _find_checkbox(session_id: str) -> tuple[str, dict[str, float]] | None:
    """First clickable Turnstile checkbox: ``(frame_id, page coords)``.

    The in-frame centre becomes page coordinates via the hosting
    ``<iframe>`` element's bounding-box origin (cf_bypasser:177-182 — the
    frame's own JS world has no ``window.frameElement`` back-reference, so
    the page measures the frame element instead).
    """
    for frame_id in await _cf_frame_ids(session_id):
        box = await _checkbox_in_frame(session_id, frame_id)
        if box is None:
            continue
        origin = await _eval_value(
            "(el => { if(!el) return null;"
            " const r = el.getBoundingClientRect();"
            " return {x:r.x, y:r.y}; })"
            f"(document.querySelector('iframe[src*=\"{_CF_FRAME_MARKER}\"]'))",
            session_id,
        )
        if not isinstance(origin, dict):
            continue
        try:
            ox, oy = float(origin.get("x") or 0), float(origin.get("y") or 0)
        except (TypeError, ValueError):
            continue
        return frame_id, {"x": ox + box["x"], "y": oy + box["y"]}
    return None


async def _click_at(session_id: str, x: float, y: float) -> bool:
    """Native press/release at page coords. True when both dispatched."""
    down = await _dispatch_mouse("mousedown", x, y, buttons=1, session_id=session_id)
    if _is_error_response(down):
        return False
    up = await _dispatch_mouse("mouseup", x, y, session_id=session_id)
    return not _is_error_response(up)


async def _verify_checkbox(session_id: str, frame_id: str) -> bool:
    """Re-evaluate after click: success = checkbox gone or checked."""
    raw = await _mirage_eval_result(
        f"({ _FIND_CHECKBOX_JS })()", frame_id, session_id=session_id,
    )
    if isinstance(raw, str) or not isinstance(raw, dict):
        return False
    if raw.get("exceptionDetails"):
        return False
    info = (raw.get("result") or {}).get("value")
    if not isinstance(info, dict):
        return False
    return (not info.get("found")) or bool(info.get("checked"))


async def _click_turnstile(session_id: str) -> tuple[bool, bool]:
    """Find the Turnstile checkbox via frame filter + shadow walk and click
    it natively. Returns ``(dispatched, verified)``: dispatched is True once
    the native press/release dispatches, verified reflects the post-click
    re-eval (checkbox gone or checked).
    """
    found = await _find_checkbox(session_id)
    if found is None:
        return False, False
    frame_id, point = found
    if not await _click_at(session_id, point["x"], point["y"]):
        return False, False
    return True, await _verify_checkbox(session_id, frame_id)


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


# kept for future refusal-evidence / debugging; currently unused.
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
    out non-interactive managed challenges, clicks an interactive Turnstile
    checkbox with native events when one is on screen, and verifies the page
    itself opened (title gate). No second browser, no cookie cache, no replay
    proxy.

    Returns ``{cleared, method, url, cfCookies, elapsedMs}``. ``cleared`` is
    true ONLY when the interstitial title is gone AND host-scoped
    ``cf_clearance`` is present; host-scoped cf_ cookies are reported as
    supporting evidence. CF may refuse clicks — then ``cleared`` is false
    with evidence and the ``pause_for_human`` contract ``challenge_status``
    uses.
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
        deadline = started + budget
        # TR retry loop (cf_bypasser:232-239): up to _MAX_ATTEMPTS passes of
        # verify -> click -> jittered retry-poll sleep. ``clicks`` counts
        # dispatched native press/release pairs even when the post-click
        # re-eval does not verify, so timeout evidence shows real attempts.
        for _ in range(_MAX_ATTEMPTS):
            # Ground truth: the interstitial title is gone. Cookies are only
            # supporting evidence (host-scoped + page-context agreement).
            if await _is_bypassed(session_id):
                cf = await _cf_cookies(session_id, host)
                page_names = await _page_cf_cookie_names(session_id)
                if "cf_clearance" in cf:
                    return _dump({"cleared": True,
                                  "method": "click" if clicks else "auto",
                                  "url": url, "cfCookies": sorted(cf),
                                  "pageCfCookies": sorted(page_names),
                                  "clicks": clicks, "elapsedMs": elapsed_ms()})
            if time.monotonic() >= deadline:
                break
            dispatched, _verified = await _click_turnstile(session_id)
            if dispatched:
                clicks += 1
            await asyncio.sleep(
                jittered_delay(_RETRY_POLL_SECONDS * 1000.0,
                               _RETRY_POLL_JITTER_SECONDS * 1000.0) / 1000.0,
            )

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
