"""Real-browser end-to-end tests for iframe targeting (Gap B).

Proves the Mirage DOM tools can reach elements INSIDE an iframe through
their new ``frame_id`` parameter. The page is served by a stdlib
``http.server`` thread on 127.0.0.1 — no external network deps.

Fixture endpoints:
  /      -> main page embedding <iframe src="/inner"> (no #btn / input#name
            in the main document — the inner elements exist ONLY in the
            iframe document)
  /inner -> iframe document with <button id="btn"> and <input id="name">;
            the button's click handler rewrites <div id="out">

Mechanics under test (see kahin/tools/_common.py + the_twins/mirage.py):
- Runtime.executionContextCreated events (forwarded verbatim by the
  sidecar) feed a frameId -> executionContextId map (main world only).
- frame_id on a DOM tool resolves that map; the expression then runs via
  Runtime.callFunction with the explicit executionContextId (the sidecar's
  Runtime.evaluate handler always resolves the MAIN frame's context, so a
  passthrough method is required for non-main frames).
- Without frame_id every tool keeps the old Runtime.evaluate path — main
  frame only, unchanged.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypeVar

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage, trainman_mirage

_MAIN_HTML = """<!doctype html><html><body style="margin:0">
<iframe id="frame" src="/inner" width="600" height="400"></iframe>
<div id="main-out">main-page</div>
</body></html>"""

_INNER_HTML = """<!doctype html><html><body>
<button id="btn">inner-button</button>
<input id="name" placeholder="name">
<div id="out">idle</div>
<script>
  document.getElementById('btn').addEventListener('click', () => {
    document.getElementById('out').textContent = 'clicked';
  });
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    """Minimal local server: / (main page with iframe) and /inner."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/":
            body = _MAIN_HTML.encode()
            ctype = "text/html"
        elif self.path == "/inner":
            body = _INNER_HTML.encode()
            ctype = "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, N802
        del format, args  # silence access logs


def _real_available() -> bool:
    """True when the sidecar binary and a real Camoufox are both present."""
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing; build core/ipc_main.zig first"
)


def _loads(text: str) -> Any:
    return json.loads(text)


_T = TypeVar("_T")


async def _poll(
    probe: Callable[[], Awaitable[_T]],
    pred: Callable[[_T], bool],
    timeout: float = 20.0,
    interval: float = 0.2,
) -> _T:
    """Poll an async probe until pred(value) is truthy; else AssertionError."""
    deadline = asyncio.get_running_loop().time() + timeout
    last: _T | None = None
    while True:
        last = await probe()
        if pred(last):
            return last
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s; last: {last!r}")
        await asyncio.sleep(interval)


@async_fixture
async def http_server() -> AsyncGenerator[str, None]:
    """Local HTTP server thread; yields the base URL, shuts down on teardown."""
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
    """Start real Camoufox through kahin_browser_start with its one tab, then stop."""
    resp = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tabs = _loads(await trainman_mirage.mirage_tab_list())
        assert len(tabs) == 1 and tabs[0].get("targetId"), tabs
        await asyncio.sleep(0.5)  # session/frame events settle (mirrors test_e2e_mirage)
        yield
    finally:
        await pilot.browser_stop()


async def _navigate(url: str) -> None:
    resp = _loads(await pilot.navigate(url=url))
    assert isinstance(resp, dict) and resp.get("frameId"), resp
    await asyncio.sleep(0.5)  # sink-flushed events land while idle


async def _iframe_frame_id() -> str:
    """Frame id of the /inner iframe from kahin_mirage_frame_tree."""

    async def probe() -> str | None:
        tree = _loads(await pilot_mirage.mirage_frame_tree())
        node = tree.get("frameTree") or {}
        for child in node.get("childFrames") or []:
            frame = child.get("frame") or {}
            if "/inner" in (frame.get("url") or ""):
                return frame.get("id")
        return None

    frame_id = await _poll(probe, lambda f: f is not None)
    assert isinstance(frame_id, str)
    return frame_id


# ---------------------------------------------------------------------------
# Scenario a: frame tree lists the iframe as a child frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_tree_shows_iframe_child(mirage_tools: None, http_server: str) -> None:
    """mirage_frame_tree -> the /inner iframe appears as a childFrame."""
    await _navigate(f"{http_server}/")

    tree = _loads(await pilot_mirage.mirage_frame_tree())
    node = tree.get("frameTree") or {}
    children = node.get("childFrames") or []
    inner = [ch for ch in children if "/inner" in ((ch.get("frame") or {}).get("url") or "")]
    assert inner, f"iframe missing from frame tree: {tree}"
    assert inner[0]["frame"]["id"], inner


# ---------------------------------------------------------------------------
# Scenario b: query inside the iframe via frame_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_finds_element_inside_iframe(mirage_tools: None, http_server: str) -> None:
    """frame_id -> button#btn found; without frame_id it is NOT (main frame)."""
    await _navigate(f"{http_server}/")
    frame_id = await _iframe_frame_id()

    # Main frame cannot see the iframe's button (default path unchanged).
    main_hit = _loads(await pilot_mirage.mirage_query("button#btn"))
    assert main_hit is None, f"main frame should not see iframe button: {main_hit}"

    # The iframe's own context can.
    hit = await _poll(
        lambda: pilot_mirage.mirage_query("button#btn", frame_id=frame_id),
        lambda r: isinstance(_loads(r), dict),
    )
    parsed = _loads(hit)
    assert parsed["tag"] == "button", parsed
    assert parsed["id"] == "btn", parsed
    assert parsed["text"] == "inner-button", parsed
    assert parsed["visible"] is True, parsed


# ---------------------------------------------------------------------------
# Scenario c: click inside the iframe via frame_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_inside_iframe_changes_dom(mirage_tools: None, http_server: str) -> None:
    """Real mouse click on the iframe's button; DOM change proves it."""
    await _navigate(f"{http_server}/")
    frame_id = await _iframe_frame_id()

    clicked = _loads(await pilot_mirage.mirage_click("#btn", frame_id=frame_id))
    assert clicked["clicked"] == "#btn", clicked

    # The iframe's #out div flipped from idle to clicked.
    text = await _poll(
        lambda: pilot_mirage.mirage_get_text("#out", frame_id=frame_id),
        lambda r: isinstance(_loads(r), str) and _loads(r) == "clicked",
    )
    assert _loads(text) == "clicked"


# ---------------------------------------------------------------------------
# Scenario d: type into the iframe's input via frame_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_into_iframe_input(mirage_tools: None, http_server: str) -> None:
    """Page.insertText lands in the iframe's focused input."""
    await _navigate(f"{http_server}/")
    frame_id = await _iframe_frame_id()

    typed = _loads(await pilot_mirage.mirage_type("input#name", "kahin", frame_id=frame_id))
    assert typed["typed"] == 5, typed

    value = await _poll(
        lambda: pilot_mirage.mirage_get_value("input#name", frame_id=frame_id),
        lambda r: _loads(r) == "kahin",
    )
    assert _loads(value) == "kahin"
