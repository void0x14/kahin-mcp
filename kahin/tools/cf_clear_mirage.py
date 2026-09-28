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

Ground truth for ``cleared`` is the page itself (title gate + challenge
probe) plus host-scoped ``cf_clearance`` presence as supporting evidence —
never cookie presence alone (``Browser.getCookies`` returns the WHOLE
persistent profile, so a cookie match is only meaningful when its domain
covers the target host). ``cf_clearance`` is httpOnly, so it is never
visible in ``document.cookie``; page-context ``cf_`` names are reported as
non-httpOnly supporting evidence only and are never part of the gate.

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
from kahin.humanize import bezier_trajectory, jittered_delay, step_delays
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
# DEFAULT_MAX_RETRIES=5, CHALLENGE_SETTLE_SECONDS=5 (Kahin: 9.0), RETRY_POLL_SECONDS=3.
# Fast path (düz CF): curl_cffi impersonate ile tarayıcısız fingerprint
# geçişi dener (safari18_0 → chrome131). Başarırsa browser ASLA açılmaz;
# yalnızca JS/challenge/canvas gerektiğinde browser yoluna düşülür.
# managed challenge (JS proof-of-work + a Turnstile click + settle).
# Mirrors the Türk bypasser constants (cf_bypasser/utils/constants.py):
# DEFAULT_MAX_RETRIES=5, CHALLENGE_SETTLE_SECONDS=5 (Kahin: 9.0), RETRY_POLL_SECONDS=3.
_MAX_URL_LENGTH = 4096
_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 180.0
_RETRY_POLL_SECONDS = 3.0
_RETRY_POLL_JITTER_SECONDS = 1.0
_INITIAL_SETTLE_SECONDS = 9.0  # TR ref 5s; widget iframe appears before it is interactive
_MAX_ATTEMPTS = 5
# Hızlı yol: tarayıcısız fingerprint profilleri, sırayla deneinecek.
_FAST_IMPERSONATE = ("safari18_0", "chrome131")
_FAST_TIMEOUT = 30.0
# Frame-URL filter: the Turnstile challenge frame serves from this host
# (cf_bypasser/core/bypasser.py:171 — ``"challenges.cloudflare" in f.url``).
_CF_FRAME_MARKER = "challenges.cloudflare"

# Turnstile "normal" widget geometry (300x65 CSS iframe). Inside the widget
# the checkbox is a ~23x28 control whose centre sits at a fixed offset from
# the widget's top-left. Measured live on crackingx.com (dark theme): iframe
# CSS (202,280) 300x65 -> checkbox border span CSS x 211..234, y 301..324,
# centre (+20.4, +32.4); the vertical offset is the widget half-height
# (65/2 = 32.5), i.e. the checkbox is vertically centred. This is Turnstile
# widget geometry -- not a page coordinate -- so it is independent of where
# the widget is placed and of its colour theme.
_TURNSTILE_CHECKBOX_DX = 20.4

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
            const sr = el.shadowRootUnl || el.openOrClosedShadowRoot || el.fakeShadowRoot || el.shadowRoot;
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
    "    .slice(0, 60000).toLowerCase();"
    "    if(html.includes('please complete the captcha')) return {bypassed:false, reason:'captcha-text'};"
    f"    const markers = {_BLOCK_MARKERS_JSON};"
    "    for(const m of markers){ if(html.includes(m)) return {bypassed:false, reason:'block:'+m}; }"
    "    const respInput = document.querySelector('input[name=\"cf-turnstile-response\"], input[name=\"g-recaptcha-response\"]');"
    "    const token = respInput && respInput.value ? respInput.value : (window.__turnstile_token || '');"
    "    if(token && token.length > 20) return {bypassed:true, token: token};"
    "    return {bypassed:true};"
    "})()"
)


def _engine_failed(resp: Any) -> bool:
    """True when an engine response carries an error (dict-aware).

    ``_is_error_response`` parses a JSON *string*; the Mirage path already
    hands back parsed dicts, so feeding it a dict misjudges a perfectly valid
    payload (a real screenshot was being discarded as an error this way).
    Check the parsed form directly when the response is not a string.
    """
    if isinstance(resp, str):
        return _is_error_response(resp)
    return isinstance(resp, dict) and bool(resp.get("error"))


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
    """Yield ``(frame_id, url)`` pairs from a frame tree."""
    for frame_id, _parent_id, url in _iter_frame_nodes(frame_tree):
        yield frame_id, url


def _iter_frame_nodes(frame_tree: Any) -> Any:
    """Yield ``(frame_id, parent_id, url)`` entries from a frame tree."""
    stack = [(frame_tree, None)]
    while stack:
        node, parent_id = stack.pop()
        if not isinstance(node, dict):
            continue
        frame = node.get("frame")
        actual_parent = parent_id
        if isinstance(frame, dict):
            frame_id = frame.get("id")
            actual_parent = frame.get("parentId") or parent_id
            yield frame_id, actual_parent, str(frame.get("url") or "")
        children = node.get("childFrames")
        if isinstance(children, list):
            stack.extend(
                (child, frame.get("id") if isinstance(frame, dict) else actual_parent)
                for child in children
            )


async def _frame_tree(session_id: str) -> dict[str, Any] | None:
    """Return the live frame tree, or None when unavailable."""
    try:
        tree = await _mirage_engine().call("Page.getFrameTree", session_id=session_id)
    except Exception:  # noqa: BLE001
        return None
    frame_tree = tree.get("frameTree") if isinstance(tree, dict) else None
    return frame_tree if isinstance(frame_tree, dict) else None


async def _frame_origin(session_id: str, frame_id: str, frame_tree: dict[str, Any]) -> dict[str, float] | None:
    """Resolve a frame origin through its actual parent iframe relation."""
    parents: dict[str, str | None] = {}
    for candidate_id, parent_id, _url in _iter_frame_nodes(frame_tree):
        if isinstance(candidate_id, str) and candidate_id:
            parents[candidate_id] = parent_id if isinstance(parent_id, str) else None

    async def resolve(current_id: str, seen: set[str]) -> dict[str, float] | None:
        if current_id in seen:
            return None
        seen.add(current_id)
        parent_id = parents.get(current_id)
        if not parent_id:
            return {"x": 0.0, "y": 0.0}
        marker = f"__kahin_cf_frame_{current_id}"
        marker_json = orjson.dumps(marker).decode()
        old_name = await _eval_value(
            f"(() => {{ const old = window.name; window.name = {marker_json}; return old; }})()",
            session_id, frame_id=current_id,
        )
        if not isinstance(old_name, str):
            return None
        try:
            # The Turnstile iframe lives inside a CLOSED shadow root on the
            # challenge page, so a flat document.querySelectorAll('iframe')
            # never sees it and the precise DOM click was silently skipped
            # (only the heuristic screenshot/Tab fallbacks ran). Walk open and
            # closed shadow roots with the same accessor ladder the checkbox
            # finder uses, then match the frame by the name marker.
            local = await _eval_value(
                "(() => { const frames = [];"
                " const walk = (root) => {"
                "  for (const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])) {"
                "   if (el.tagName === 'IFRAME') frames.push(el);"
                "   const sr = el.shadowRootUnl || el.openOrClosedShadowRoot"
                "     || el.fakeShadowRoot || el.shadowRoot;"
                "   if (sr) walk(sr);"
                "  } };"
                " walk(document);"
                " for (const el of frames) {"
                f"  try {{ if (el.contentWindow && el.contentWindow.name === {marker_json}) {{"
                "   const r = el.getBoundingClientRect(); return {x:r.x, y:r.y}; } } catch (_) {}"
                " }"
                " for (const el of frames) {"
                "  if (String(el.src || '').indexOf('challenges.cloudflare') >= 0) {"
                "   const r = el.getBoundingClientRect(); return {x:r.x, y:r.y}; }"
                " } return null; })()",
                session_id, frame_id=parent_id,
            )
        finally:
            await _eval_value(f"window.name = {orjson.dumps(old_name).decode()}", session_id, frame_id=current_id)
        if not isinstance(local, dict):
            return None
        try:
            local_x, local_y = float(local.get("x") or 0), float(local.get("y") or 0)
        except (TypeError, ValueError):
            return None
        ancestor = await resolve(parent_id, seen)
        if ancestor is None:
            return None
        return {"x": ancestor["x"] + local_x, "y": ancestor["y"] + local_y}

    return await resolve(frame_id, set())


async def _cf_frame_ids(session_id: str) -> list[str]:
    """Frame ids for challenge frames, retaining URL-less candidates."""
    frame_tree = await _frame_tree(session_id)
    if frame_tree is None:
        return []
    return [
        frame_id for frame_id, parent_id, url in _iter_frame_nodes(frame_tree)
        if isinstance(frame_id, str) and frame_id and parent_id
        and (_CF_FRAME_MARKER in url or not url)
    ]


async def _checkbox_in_frame(session_id: str, frame_id: str) -> dict[str, float] | None:
    """In-frame checkbox centre (frame-viewport coords), or None."""
    raw = await _mirage_eval_result(f"({_FIND_CHECKBOX_JS})()", frame_id, session_id=session_id)
    if isinstance(raw, str) or not isinstance(raw, dict) or raw.get("exceptionDetails"):
        return None
    info = (raw.get("result") or {}).get("value")
    if not isinstance(info, dict) or not info.get("found"):
        return None
    try:
        w, x, y = float(info.get("w") or 0), float(info.get("x") or 0), float(info.get("y") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or info.get("checked"):
        return None
    return {"x": x, "y": y}


_WIDGET_IFRAME_JS = """() => {
    const frames = [];
    const walk = (root) => {
        for (const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])) {
            if (el.tagName === 'IFRAME') frames.push(el);
            const sr = el.shadowRootUnl || el.openOrClosedShadowRoot
                || el.fakeShadowRoot || el.shadowRoot;
            if (sr) walk(sr);
        }
    };
    walk(document);
    for (const el of frames) {
        if (String(el.src || '').indexOf('challenges.cloudflare') >= 0) {
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }
    }
    return null;
}"""


async def _widget_iframe_rect(session_id: str) -> dict[str, float] | None:
    """The challenges.cloudflare iframe rect, in page CSS coordinates.

    The widget iframe sits inside a CLOSED shadow root, so a flat
    ``document.querySelectorAll('iframe')`` never sees it. Walk open and
    closed shadow roots with the same accessor ladder the in-frame checkbox
    finder uses, then read the rect from the live DOM.
    """
    value = await _eval_value(f"({_WIDGET_IFRAME_JS})()", session_id)
    if not isinstance(value, dict):
        return None
    try:
        x = float(value.get("x") or 0)
        y = float(value.get("y") or 0)
        w = float(value.get("w") or 0)
        h = float(value.get("h") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


async def _widget_checkbox_point(session_id: str) -> dict[str, float] | None:
    """Checkbox centre in page CSS coords, from the live widget rect.

    Frame-level evaluate inside the challenges.cloudflare iframe fails on
    this Camoufox build (COOP+COEP force the widget out of process -- Juggler
    replies ``evaluate failed``), so the checkbox cannot be read from inside
    the frame and the solver used to fall through to the keyboard path. The
    widget iframe's own rect IS readable from the top-level document (the
    closed-shadow-piercing walk in ``_widget_iframe_rect``), and the checkbox
    sits at a fixed Turnstile-widget-relative offset inside that rect, so the
    click point is derived from geometry alone -- no frame evaluate, no
    screenshot, no pixel scale.
    """
    rect = await _widget_iframe_rect(session_id)
    if rect is None:
        return None
    return {"x": rect["x"] + _TURNSTILE_CHECKBOX_DX,
            "y": rect["y"] + rect["h"] / 2.0}


async def _key_press(session_id: str, key: str, code: str, key_code: int = 0) -> bool:
    """Trusted keyDown+keyUp via Page.dispatchKeyEvent. True when both land.

    ``Page.keyPress`` does not exist on Juggler — the real surface is
    ``Page.dispatchKeyEvent`` with lowercase keydown/keyup types (same
    contract ``mirage_key_press`` uses in pilot_mirage.py).
    """
    engine = _mirage_engine()
    base: dict[str, object] = {
        "key": key, "keyCode": key_code, "location": 0,
        "code": code, "repeat": False,
    }
    try:
        down = await engine.call(
            "Page.dispatchKeyEvent", {**base, "type": "keydown"},
            session_id=session_id)
        if isinstance(down, dict) and down.get("error"):
            return False
        up = await engine.call(
            "Page.dispatchKeyEvent", {**base, "type": "keyup"},
            session_id=session_id)
        if isinstance(up, dict) and up.get("error"):
            return False
        return True
    except Exception:  # noqa: BLE001 - key path returns bool, never raises
        return False
    """Single trusted key press via Juggler dispatch. True when accepted."""
    engine = _mirage_engine()
    params: dict[str, object] = {"key": key}
    if code:
        params["code"] = code
    try:
        resp = await engine.call("Page.keyPress", params, session_id=session_id)
    except Exception:  # noqa: BLE001
        return False
    return not _engine_failed(resp)


async def _tab_space_turnstile(session_id: str) -> tuple[bool, str]:
    """Tab-walk focus into the widget, Space toggles it. Single shot.

    CloakBrowser #343: mouse clicks rejected while keyboard-driven Space
    on the focused checkbox produced the trusted event that minted the
    token. No coordinates, no mouse travel — the browser's own toggle
    path fires, so there is no synthetic-pointer behavior to score.
    Returns (dispatched, focused_tag_before_space).
    """
    for _ in range(12):
        if not await _key_press(session_id, "Tab", "Tab", 9):
            return False, ""
        await asyncio.sleep(0.4)
        focused = await _eval_value(
            "document.activeElement ? document.activeElement.tagName : 'none'",
            session_id,
        )
        tag = str(focused) if isinstance(focused, str) else ""
        if tag.upper() in ("INPUT", "IFRAME", "BUTTON"):
            break
    else:
        tag = ""
    if not await _key_press(session_id, " ", "Space", 32):
        return False, tag
    await asyncio.sleep(2.0)
    return True, tag

async def _find_checkbox(session_id: str) -> tuple[str, dict[str, float]] | None:
    """Find a checkbox and map coordinates through its selected frame relation."""
    frame_tree = await _frame_tree(session_id)
    if frame_tree is None:
        return None
    for frame_id, parent_id, url in _iter_frame_nodes(frame_tree):
        if not (isinstance(frame_id, str) and frame_id and parent_id):
            continue
        if _CF_FRAME_MARKER not in url and url:
            continue
        box = await _checkbox_in_frame(session_id, frame_id)
        if box is None:
            continue
        origin = await _frame_origin(session_id, frame_id, frame_tree)
        if origin is None:
            continue
        return frame_id, {"x": origin["x"] + box["x"], "y": origin["y"] + box["y"]}
    return None


async def _flow_click(session_id: str, x: float, y: float) -> bool:
    """Human-flow click: sweep in from afar, then press.

    Live truth (2026-09-16): teleport clicks (dispatch at target with
    the cursor already parked there, long dead waits between moves)
    never clear; a continuous Bézier sweep from a far corner into
    the widget followed by an immediate press does. No dead sleeps
    between travel and press — a parked cursor reads as synthetic.

    The sweep is dispatched as a real Juggler ``Page.dispatchMouseEvent``
    mousemove per Bézier point (same generator ``mirage_mouse_trajectory``
    uses). There is no ``Page.dispatchMouseTrajectory`` command in Juggler,
    so the previous call was a no-op error and the solver silently degraded
    to a single teleport press that never minted the token.
    """
    start_x, start_y = 1400.0, 200.0
    await _dispatch_mouse("mousemove", start_x, start_y, session_id=session_id)
    points = bezier_trajectory(start_x, start_y, x, y, jitter=2.0, seed=903)
    delays = step_delays(len(points), base_ms=4.0, jitter_ms=2.0, seed=903)
    for index, (px, py) in enumerate(points):
        moved = await _dispatch_mouse("mousemove", px, py, session_id=session_id)
        if _engine_failed(moved):
            break
        if index < len(points) - 1:
            await asyncio.sleep(delays[index] / 1000.0)
    return await _click_at(session_id, x, y)


async def _click_at(session_id: str, x: float, y: float) -> bool:
    """Native press/release at page coords. True when both dispatched."""
    """Native press/release at page coords. True when both dispatched."""
    """Native press/release at page coords. True when both dispatched."""
    down = await _dispatch_mouse("mousedown", x, y, buttons=1, session_id=session_id)
    if _engine_failed(down):
        return False
    up = await _dispatch_mouse("mouseup", x, y, session_id=session_id)
    return not _engine_failed(up)


async def _verify_checkbox(session_id: str, frame_id: str) -> bool:
    """Re-evaluate after click: success = checkbox gone or checked."""
    raw = await _mirage_eval_result(f"({_FIND_CHECKBOX_JS})()", frame_id, session_id=session_id)
    if isinstance(raw, str) or not isinstance(raw, dict) or raw.get("exceptionDetails"):
        return False
    info = (raw.get("result") or {}).get("value")
    return isinstance(info, dict) and ((not info.get("found")) or bool(info.get("checked")))


async def _click_turnstile(session_id: str) -> tuple[bool, bool]:
    """Turnstile path: geometry click, then keyboard toggle. First hit wins.

    The in-frame DOM read is impossible on this build (the CF frame rejects
    ``evaluate``), so the checkbox point is derived from the live widget rect
    and that geometry click is attempted first. Tab+Space is the last resort
    for when the widget is on screen but the mouse dispatch itself fails.
    Single pass — no loops, no coordinate guessing.
    """
    found = await _find_checkbox(session_id)
    if found is not None:
        frame_id, point = found
        if await _flow_click(session_id, point["x"], point["y"]):
            return True, await _verify_checkbox(session_id, frame_id)
    point = await _widget_checkbox_point(session_id)
    if point is None:
        # The widget is not on screen yet. Report "not attempted" so the
        # caller's retry loop keeps polling instead of burning the keyboard
        # fallback on a page that has no widget to toggle.
        return False, False
    if await _flow_click(session_id, point["x"], point["y"]):
        return True, await _is_bypassed(session_id)
    tabbed, _focused = await _tab_space_turnstile(session_id)
    if tabbed:
        return True, await _is_bypassed(session_id)
    return False, False


async def _cf_cookies(session_id: str, host: str) -> dict[str, str]:
    """Cloudflare cookies (cf_ / __cf prefix) scoped to the target host.

    ``Browser.getCookies`` returns the whole persistent profile (every
    site's httpOnly cookies included), so an unscoped match proves nothing.
    A cookie counts only when its domain covers ``host`` — and even then it
    is supporting evidence, not clearance: the title gate + challenge probe
    must pass. ``cf_clearance`` is httpOnly, so it is never visible in
    ``document.cookie``; page-context ``cf_`` names are reported as
    non-httpOnly supporting evidence only and are never part of the gate.
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


async def _scrub_host_cf_cookies(session_id: str, host: str) -> int:
    """Expire host-scoped CF cookies so a voided token cannot pin the jar.

    Juggler has no ``Network.deleteCookies`` — the only cookie-write
    surface is ``Browser.setCookies`` (schema-verified: name/value plus
    optional domain/path/expires). Deletion = overwrite the same
    (name, domain, path) with ``value=""`` and ``expires=1`` — the
    standard expire-to-delete pattern Playwright documents for
    ``context.clearCookies({name, domain})`` scoping.
    Only CF names (``cf_`` / ``__cf`` prefix) whose domain covers ``host``
    are touched — every copy (each domain/path pair) is expired
    individually; every other cookie in the persistent profile survives.
    Best-effort: any failure returns how many were expired so far; the
    caller retries the challenge either way. Returns the count.
    """
    engine = _mirage_engine()
    cf = await _cf_cookies(session_id, host)
    if not cf:
        return 0
    try:
        payload = await engine.call("Browser.getCookies", {}, session_id=session_id)
    except Exception:  # noqa: BLE001
        return 0
    cookies = (payload or {}).get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list):
        return 0
    host = host.lower()
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        path = str(cookie.get("path") or "/")
        if not (isinstance(name, str) and name in cf):
            continue
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        key = (name, domain, path)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"name": name, "value": "", "domain": domain, "path": path})
    if not targets:
        return 0
    try:
        await engine.call(
            "Browser.setCookies",
            {"cookies": [{**t, "expires": 1} for t in targets]},
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 - expire write failed, jar untouched
        return 0
    return len(targets)


async def _page_cf_cookie_names(session_id: str) -> list[str]:
    """cf_ cookies visible in page context (non-httpOnly, this document)."""
    value = await _eval_value(
        "document.cookie.split(';').map(c=>c.trim().split('=')[0]).filter(n=>/cf_/i.test(n))",
        session_id,
    )
    return [str(n) for n in value] if isinstance(value, list) else []


async def _fast_fingerprint_clear(
    url: str, host: str, started: float, proxy: str | None = None
) -> dict[str, Any] | None:
    """Düz CF için tarayıcısız hızlı yol: curl_cffi impersonate.

    Browser ASLA açılmaz — yalnızca python komutu yürütülür. Profiller
    sırayla denenir (safari18_0 → chrome131). Başarı = HTTP 200/301/302
    + interstitial yok (title/body'de 'just a moment' / captcha metni
    yok). Başarısızlıkta None döner, çağıran browser yoluna düşer —
    yani JS/challenge/canvas gerektiği kanıtlanınca Kahin açılır.

    ``proxy`` motorun yapılandırılmış çıkışıdır ve ZORUNLU olarak
    kullanılır: onsuz curl_cffi hedefe doğrudan, host'un gerçek IP'sinden
    bağlanır — hedef sitelerde kullanılması yasak olan tek çıkış."""
    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)
    try:
        from curl_cffi import requests as _cf_requests
    except ImportError:
        return None
    from urllib.parse import urlparse as _up
    try:
        origin = f"{_up(url).scheme}://{_up(url).hostname}"
    except Exception:  # noqa: BLE001
        origin = f"https://{host}"
    for profile in _FAST_IMPERSONATE:
        try:
            session = _cf_requests.Session(impersonate=profile)
            session.headers.update({"Origin": origin, "Referer": origin + "/"})
            resp = session.get(url, timeout=_FAST_TIMEOUT, proxy=proxy)
            status = resp.status_code
            body = (resp.text or "")[:60000].lower()
            cookies = sorted({c.name for c in session.cookies.jar
                              if c.name.startswith(("cf_", "__cf"))})
            interstitial = ("just a moment" in body
                            or "please complete the captcha" in body
                            or "challenges.cloudflare" in body)
            if status in (200, 301, 302) and not interstitial:
                return {"cleared": True, "method": f"fast:{profile}",
                        "url": url, "status": status,
                        "cfCookies": cookies, "browser": False,
                        "elapsedMs": elapsed_ms()}
        except Exception:  # noqa: BLE001 - sıradaki profile düş
            continue
    return None


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
    """Clear a Cloudflare challenge, fast path first (no browser for düz CF).

    Önce tarayıcısız fingerprint geçişi denenir (curl_cffi impersonate:
    safari18_0 → chrome131) — yalnızca python komutu, browser açılmyor.
    Düz CF burada geçilir. Yalnızca JS/challenge/canvas gerektiği
    kanıtlanınca (hızlı yol 200 vermedi / interstitial sürdü) Kahin'in
    Camoufox browser yoluna düşülür: navigate + managed-challenge bekleme
    + Turnstile native click + title-gate doğrulama. No second browser,
    no cookie cache, no replay proxy.

    Returns ``{cleared, method, url, cfCookies, elapsedMs}``. ``method``
    ``fast:<profile>`` ile başlıyorsa browser açılmadan geçilmiştir
    (``browser: False``). ``cleared`` browser yolunda true ONLY when the
    interstitial title is gone AND host-scoped ``cf_clearance`` is present.
    CF may refuse clicks — then ``cleared`` is false with evidence and
    the ``pause_for_human`` contract ``challenge_status`` uses.
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

    # HIZLI YOL: düz CF — browser yok, healer yok, sadece python.
    # _healer_ref.safe ÖNCESİNDE: engine'siz çalışır, geçerse dön.
    # Motorun yapılandırılmış proxy'si ZORUNLU geçirilir: browserless yol
    # onsuz hedefe gerçek IP'den bağlanır ve bu, hedef sitelerde yasak
    # olan tek çıkıştır.
    _fast_started = time.monotonic()
    fast = await _fast_fingerprint_clear(
        url, host, _fast_started, getattr(_mirage_engine(), "_proxy_url", None)
    )
    if fast is not None:
        return _dump(fast)

    async with _healer_ref.safe(_TOOL, url=url[:80], timeout=budget):
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        # YAVAŞ YOL: JS/challenge/canvas kanıtlandı — şimdi browser açılır.

        # YAVAŞ YOL: JS/challenge/canvas kanıtlandı — şimdi browser açılır.
        session_id, error = await _capture_page_session(_TOOL)
        if error:
            return error
        assert session_id is not None
        assert session_id is not None
        engine = _mirage_engine()

        # Inject message listener in window BEFORE navigation/rendering to capture postMessage token instantly
        _INIT_LISTENER_JS = (
            "(() => {"
            "  if(window.__cf_listener_installed) return;"
            "  window.__cf_listener_installed = true;"
            "  window.__turnstile_token = '';"
            "  window.addEventListener('message', (e) => {"
            "    try {"
            "      const data = typeof e.data === 'string' ? JSON.parse(e.data) : e.data;"
            "      if(data && (data.source === 'cloudflare-challenge' || data.source === 'turnstile') && data.event === 'success') {"
            "        window.__turnstile_token = data.token || '';"
            "      }"
            "    } catch(_) {}"
            "  });"
            "})()"
        )
        try:
            await engine.call("Page.navigate", {"url": url}, session_id=session_id)
            await _eval_value(_INIT_LISTENER_JS, session_id)
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
            if "cf_clearance" in cf:
                return _dump({"cleared": True, "method": "none", "url": url,
                              "cfCookies": sorted(cf), "elapsedMs": elapsed_ms()})

        blocked = await _is_blocked(session_id)
        if blocked is not None:
            return _dump({"cleared": False, "method": "blocked", "url": url,
                          "reason": f"block page ({blocked}) — IP/ASN decision, nothing to click",
                          "action": "stop_and_review_authorization",
                          "elapsedMs": elapsed_ms()})

        # TR contract (cf_bypasser/core/bypasser.py:230-243): whether or
        # not a widget is on screen, poll up to _MAX_ATTEMPTS passes of
        # ``is_bypassed -> click -> sleep``. Non-interactive managed
        # challenges self-resolve during the wait; interactive ones need a
        # click. The click is re-attempted on every pass: the widget iframe
        # appears before it is interactive, so a single click fired on the
        # first pass can land on a widget that is not listening yet and be
        # lost -- the loop must keep clicking until the challenge clears.
        clicks = 0
        deadline = started + budget
        # TR retry loop (cf_bypasser:232-239): up to _MAX_ATTEMPTS passes of
        # verify -> click -> jittered retry-poll sleep. ``clicks`` counts
        # dispatched native press/release pairs even when the post-click
        # re-eval does not verify, so timeout evidence shows real attempts.
        for _ in range(_MAX_ATTEMPTS):
            # Ground truth: the interstitial title is gone. Cookies are only
            # supporting evidence (host-scoped + page-context agreement).
            bypassed = await _eval_value(_IS_BYPASSED_JS, session_id)
            if isinstance(bypassed, dict) and bypassed.get("bypassed"):
                token = bypassed.get("token")
                cf = await _cf_cookies(session_id, host)
                page_names = await _page_cf_cookie_names(session_id)
                if "cf_clearance" in cf or (token and len(token) > 20):
                    return _dump({"cleared": True,
                                  "method": "token" if token else ("click" if clicks else "auto"),
                                  "url": url, "cfCookies": sorted(cf),
                                  "pageCfCookies": sorted(page_names),
                                  "token": (token[:32] + "...") if token else None,
                                  "clicks": clicks, "elapsedMs": elapsed_ms()})
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
            # TR (bypasser.py:237-238): non-interactive challenges
            # auto-resolve; interactive ones need a click. Re-attempt the
            # click every pass -- an early click on a not-yet-interactive
            # widget must not consume the only attempt.
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
        # Live truth (Chromium control, 2026-09-16): CF can issue a
        # host-scoped cf_clearance yet keep serving the interstitial —
        # clearance written, page never opens, reload does not help.
        # That cookie is dead weight: the jar keeps sending a token CF
        # already voided, so every retry re-presents the same dead token.
        # Self-heal instead of stopping: scrub the host-scoped CF cookies
        # (this host only — the rest of the persistent profile survives),
        # re-navigate for ONE fresh-challenge attempt inside the same
        # budget, then fall through to the terminal diagnosis when the
        # wall still stands. No caller initiative required.
        if "cf_clearance" in cf:
            scrubbed = await _scrub_host_cf_cookies(session_id, host)
            remaining = time.monotonic()
            if scrubbed and remaining < deadline:
                try:
                    await engine.call("Page.navigate", {"url": url}, session_id=session_id)
                    await asyncio.sleep(_INITIAL_SETTLE_SECONDS)
                    if await _is_bypassed(session_id):
                        fresh = await _cf_cookies(session_id, host)
                        if "cf_clearance" in fresh:
                            return _dump({"cleared": True,
                                          "method": "stale_recovered",
                                          "url": url, "cfCookies": sorted(fresh),
                                          "clicks": clicks, "scrubbed": scrubbed,
                                          "elapsedMs": elapsed_ms()})
                except Exception:  # noqa: BLE001 - recovery is best-effort, diagnosis below is the floor
                    pass
            probe = await _challenge_probe(session_id)
            kind = (probe or {}).get("kind") if isinstance(probe, dict) else None
            cf = await _cf_cookies(session_id, host)
            return _dump({"cleared": False, "method": "stale_clearance",
                          "url": url,
                          "reason": "host-scoped cf_clearance present but the "
                                    "interstitial still serves — CF voided the "
                                    "token, further clicks will not revive it",
                          "kind": kind, "cfCookies": sorted(cf),
                          "clicks": clicks, "scrubbed": scrubbed,
                          "action": "pause_for_human_or_authorized_provider",
                          "elapsedMs": elapsed_ms()})
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
