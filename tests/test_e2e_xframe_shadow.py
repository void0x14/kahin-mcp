"""Permanent regression: cross-origin iframe inside a closed shadow root.

This is the acceptance scenario for the 2026-09-19 frame-access fix. The
page embeds, inside a CLOSED shadow root, a sandbox="allow-scripts" iframe
(opaque origin — cross-origin from the parent). The iframe document holds
<input type=checkbox id="chk">.

Guards the three root fixes end-to-end:
  1. fission.autostart=false in the launch policy — otherwise the iframe
     is out-of-process and has no execution context at all (Door A).
  2. config.forceScopeAccess=true — the main frame's default world is the
     system-principal master sandbox, so Element.shadowRootUnl and the
     cross-origin iframe's contentDocument are readable from evaluate
     (Goal A).
  3. frame_id evaluations route through the Runtime.callFunction
     passthrough — the sidecar intercepts Runtime.evaluate and would
     silently re-target the main frame (Door C engine half).

Acceptance object (exact task contract):
  {frameFound, frameId, iframeRect{x,y,w,h}, checkboxRect{x,y,w,h},
   absoluteCenter{x,y}}
plus the end-to-end proof: a raw coordinate click on absoluteCenter flips
the checkbox inside the cross-origin iframe.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage
from kahin.tools._common import _mirage_eval_result

_MAIN_HTML = """<!doctype html><html><body style="margin:0">
<div id="host" style="position:fixed;left:120px;top:80px;width:300px;height:150px"></div>
<div id="main-out">main</div>
<script>
(function () {
  var host = document.getElementById('host');
  var root = host.attachShadow({mode: 'closed'});
  var ifr = document.createElement('iframe');
  ifr.id = 'xframe';
  ifr.src = '/inner';
  ifr.setAttribute('sandbox', 'allow-scripts');
  ifr.style.cssText = 'width:300px;height:150px;border:0';
  root.appendChild(ifr);
  window.__PAGE_MARKER = 'page-world-only';
})();
</script>
</body></html>"""

_INNER_HTML = """<!doctype html><html><body style="margin:0">
<input type="checkbox" id="chk" style="position:absolute;left:20px;top:30px">
<div id="out">idle</div>
<script>
  document.getElementById('chk').addEventListener('click', function () {
    document.getElementById('out').textContent =
      'checked:' + document.getElementById('chk').checked;
  });
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            body, ctype = _MAIN_HTML.encode(), "text/html"
        elif self.path == "/inner":
            body, ctype = _INNER_HTML.encode(), "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, N802
        del format, args


def _loads(text: str) -> Any:
    return json.loads(text)

def _raw(result: Any) -> dict:
    """_mirage_eval_result returns the raw Juggler dict or a JSON error string."""
    if isinstance(result, dict):
        return result
    return _loads(result)


def _real_available() -> bool:
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing; build core/ipc_main.zig first"
)


async def _poll(probe, pred, timeout: float = 25.0, interval: float = 0.25):
    import time

    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = await probe()
        if pred(last):
            return last
        if time.monotonic() >= deadline:
            return last
        await asyncio.sleep(interval)


@async_fixture
async def http_server() -> AsyncGenerator[str, None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    resp = _loads(await pilot.browser_start(engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        await asyncio.sleep(0.5)
        yield
    finally:
        await pilot.browser_stop()


async def _acceptance(base: str) -> dict:
    """Drive the full scenario; return the acceptance object."""
    nav = _loads(await pilot.navigate(url=f"{base}/"))
    assert nav.get("frameId"), nav
    await asyncio.sleep(1.0)

    async def frame_probe():
        tree = _loads(await pilot_mirage.mirage_frame_tree())
        node = tree.get("frameTree") or {}
        for child in node.get("childFrames") or []:
            frame = child.get("frame") or {}
            if "/inner" in (frame.get("url") or ""):
                return frame.get("id")
        return None

    frame_id = await _poll(frame_probe, lambda f: bool(f))
    assert frame_id, "sandboxed iframe missing from frame tree (fission off?)"

    iframe_rect = (
        _raw(await _mirage_eval_result(
            "document.getElementById('host').shadowRootUnl.getElementById('xframe')"
            ".getBoundingClientRect().toJSON()",
        )).get("result") or {}
    ).get("value")
    assert iframe_rect, iframe_rect

    chk = await _poll(
        lambda: _mirage_eval_result(
            "document.getElementById('chk').getBoundingClientRect().toJSON()",
            frame_id=frame_id,
        ),
        lambda r: isinstance(r, dict) and isinstance((r.get("result") or {}).get("value"), dict),
    )
    checkbox_rect = chk["result"]["value"]

    return {
        "frameFound": True,
        "frameId": frame_id,
        "iframeRect": {
            "x": iframe_rect["x"], "y": iframe_rect["y"],
            "w": iframe_rect["width"], "h": iframe_rect["height"],
        },
        "checkboxRect": {
            "x": checkbox_rect["x"], "y": checkbox_rect["y"],
            "w": checkbox_rect["width"], "h": checkbox_rect["height"],
        },
        "absoluteCenter": {
            "x": iframe_rect["x"] + checkbox_rect["x"] + checkbox_rect["width"] / 2,
            "y": iframe_rect["y"] + checkbox_rect["y"] + checkbox_rect["height"] / 2,
        },
    }


@pytest.mark.asyncio
async def test_acceptance_object(mirage_tools: None, http_server: str) -> None:
    """Exact task contract: the acceptance JSON for the shadow-embedded
    cross-origin checkbox."""
    acc = await _acceptance(http_server)
    assert set(acc) == {"frameFound", "frameId", "iframeRect", "checkboxRect", "absoluteCenter"}
    assert acc["frameFound"] is True
    assert acc["frameId"].startswith("subframe-"), acc
    assert acc["iframeRect"] == {"x": 120, "y": 80, "w": 300, "h": 150}, acc
    assert acc["checkboxRect"]["w"] > 0 and acc["checkboxRect"]["h"] > 0, acc
    assert acc["absoluteCenter"]["x"] == pytest.approx(151.0, abs=2)
    assert acc["absoluteCenter"]["y"] == pytest.approx(120.0, abs=2)


@pytest.mark.asyncio
async def test_goal_a_cross_origin_content_document(mirage_tools: None, http_server: str) -> None:
    """Goal A: main-context master sandbox reads the iframe's DOM through
    shadowRootUnl + contentDocument despite the opaque sandbox origin."""
    await pilot.navigate(url=f"{http_server}/")
    await asyncio.sleep(1.0)
    res = _raw(await _mirage_eval_result(
        "(() => { var ifr = document.getElementById('host').shadowRootUnl"
        ".getElementById('xframe'); var d = ifr.contentDocument;"
        " var chk = d.getElementById('chk');"
        " return chk ? {tag: chk.tagName, type: chk.type} : null; })()",
    ))
    value = (res.get("result") or {}).get("value")
    assert value == {"tag": "INPUT", "type": "checkbox"}, res


@pytest.mark.asyncio
async def test_goal_b_frame_context_is_the_iframe_world(mirage_tools: None, http_server: str) -> None:
    """Goal B: frame_id evaluation runs INSIDE the iframe (location is /inner,
    the main page is not visible) — proves the callFunction passthrough
    routing, not the sidecar's main-frame re-target."""
    await pilot.navigate(url=f"{http_server}/")
    await asyncio.sleep(1.0)

    async def frame_probe():
        tree = _loads(await pilot_mirage.mirage_frame_tree())
        node = tree.get("frameTree") or {}
        for child in node.get("childFrames") or []:
            frame = child.get("frame") or {}
            if "/inner" in (frame.get("url") or ""):
                return frame.get("id")
        return None

    frame_id = await _poll(frame_probe, lambda f: bool(f))
    assert frame_id

    ident = await _poll(
        lambda: _mirage_eval_result(
            "({href: location.href, isTop: window.top === window.self,"
            " seesHost: !!document.getElementById('host')})",
            frame_id=frame_id,
        ),
        lambda r: isinstance(r, dict) and isinstance((r.get("result") or {}).get("value"), dict),
    )
    value = ident["result"]["value"]
    assert "/inner" in value["href"], value
    assert value["isTop"] is False, value
    assert value["seesHost"] is False, value


@pytest.mark.asyncio
async def test_coordinate_click_flips_shadow_crossorigin_checkbox(
    mirage_tools: None, http_server: str,
) -> None:
    """End-to-end: raw mouse click at the computed absoluteCenter flips the
    checkbox inside the closed-shadow cross-origin iframe."""
    acc = await _acceptance(http_server)
    center = acc["absoluteCenter"]
    clicked = _loads(await pilot_mirage.mirage_mouse_click(center["x"], center["y"]))
    assert clicked.get("clicked") or clicked.get("x") is not None, clicked

    out = await _poll(
        lambda: _mirage_eval_result(
            "document.getElementById('out').textContent", frame_id=acc["frameId"],
        ),
        lambda r: isinstance(r, dict)
        and (r.get("result") or {}).get("value") == "checked:true",
    )
    assert (out.get("result") or {}).get("value") == "checked:true", out


@pytest.mark.asyncio
async def test_master_world_isolation_intact(mirage_tools: None, http_server: str) -> None:
    """Stealth guard: page-world globals stay invisible from the evaluate
    world (the master sandbox keeps world isolation)."""
    await pilot.navigate(url=f"{http_server}/")
    await asyncio.sleep(1.0)
    res = _raw(await _mirage_eval_result("window.__PAGE_MARKER"))
    value = (res.get("result") or {}).get("value")
    assert value is None, res
