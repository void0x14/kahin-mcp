"""Real-browser end-to-end tests for the Mirage live screencast tools (Gap D).

Proves kahin_mirage_screencast_* over REAL Camoufox + a stdlib localhost
HTTP server serving an ANIMATED page (a canvas redrawn every 50ms — moving
rect + frame counter), so consecutive frames are guaranteed to differ and
the JPEG frames prove the stream is live, not a static capture.

Juggler screencast facts (camoufox-harness/vendor/Protocol.js + camoufox
nsScreencastService.cpp):
- Page.startScreencast {width, height, quality} -> {screencastId}
- Page.screencastFrame {data, deviceWidth, deviceHeight} — data is a
  base64 JPEG (NOT PNG); the event carries NO screencastId.
- Page.screencastFrameAck {screencastId} — REQUIRED per frame; camoufox
  holds the stream at kMaxFramesInFlight=1 unacked frame, so a missing ack
  stalls the stream. The frame tool acks automatically on every consume.
- Page.stopScreencast {} ends the stream; queued frames are discarded.

Scenarios:
  a. start returns a non-empty screencastId and echoes clamped/normalized
     size (odd -> even) and quality.
  b. frame() returns a real JPEG (FF D8 FF magic, Pillow decodes, dims > 0),
     ack is sent, and a SECOND frame arrives — the ack kept the stream
     flowing (without ack the stream would stall at one frame).
  c. unacked frames queue: pending() reports them, frame() then consumes the
     oldest (the ack-less window proves the queue holds frames pre-ack).
  d. stop() discards queued frames, pending() reports zero, and the next
     frame() call times out (stream is closed).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, screencast_mirage, trainman_mirage

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow optional (magic check still runs)
    Image = None

_ANIM_HTML = """<!doctype html><html><body>
<canvas id="c" width="1280" height="720"></canvas>
<script>
  const c = document.getElementById('c');
  const x = c.getContext('2d');
  let n = 0;
  setInterval(() => {
    n++;
    x.fillStyle = 'rgb(' + ((n * 37) % 256) + ',80,120)';
    x.fillRect(0, 0, c.width, c.height);
    x.fillStyle = '#fff';
    x.fillRect((n * 7) % (c.width - 100), (n * 5) % (c.height - 100), 100, 100);
    x.fillText('frame ' + n, 20, 40);
  }, 50);
</script>
</body></html>"""

_JPEG_MAGIC = b"\xff\xd8\xff"


class _Handler(BaseHTTPRequestHandler):
    """Minimal local server: /anim (animated canvas page)."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/anim":
            body = _ANIM_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

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


async def _start() -> dict[str, Any]:
    resp = _loads(await screencast_mirage.mirage_screencast_start())
    assert resp.get("screencastId"), resp
    return resp


async def _paint_distinct_canvas() -> None:
    """Make the next capture visibly different without relying on timer jitter."""
    result = _loads(await pilot.evaluate(expression="""(() => {
      const c = document.querySelector('#c');
      const ctx = c && c.getContext('2d');
      if (!ctx) return false;
      ctx.fillStyle = '#00e676';
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.fillStyle = '#111';
      ctx.fillRect(80, 80, 240, 160);
      return true;
    })()"""))
    assert result["result"]["value"] is True, result
    await asyncio.sleep(0.2)


def _assert_jpeg(frame: dict[str, Any]) -> None:
    """frame must be a real base64 JPEG with non-empty dimensions."""
    assert "data" in frame and frame["data"], frame
    raw = base64.b64decode(frame["data"])
    assert raw[:3] == _JPEG_MAGIC, f"not a JPEG (magic {raw[:3]!r})"
    assert frame["dataLength"] == len(frame["data"]), frame
    assert frame["deviceWidth"] and frame["deviceHeight"], frame
    if Image is not None:
        img = Image.open(io.BytesIO(raw))
        assert img.format == "JPEG", img.format
        assert img.size[0] > 0 and img.size[1] > 0, img.size


async def _poll_pending(pred, timeout: float = 10.0, interval: float = 0.2) -> dict[str, Any]:
    """Poll kahin_mirage_screencast_pending until pred(dict) is truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        data = _loads(await screencast_mirage.mirage_screencast_pending())
        if pred(data):
            return data
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"pending condition not met within {timeout}s; last: {data!r}")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Scenario A: start returns an id and echoes clamped/normalized parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screencast_start_id_and_clamps(mirage_tools: None) -> None:
    """Odd sizes normalize to even, oversized/clamped values clamp, and the
    returned screencastId is a non-empty string."""
    resp = _loads(await screencast_mirage.mirage_screencast_start(
        width=801, height=719, quality=500
    ))
    assert isinstance(resp["screencastId"], str) and resp["screencastId"], resp
    assert resp["width"] == 800, resp   # odd -> even
    assert resp["height"] == 718, resp  # odd -> even
    assert resp["quality"] == 100, resp  # clamped 1..100
    assert resp["format"] == "jpeg", resp

    tiny = _loads(await screencast_mirage.mirage_screencast_start(width=3, height=4, quality=0))
    assert tiny["width"] == 10 and tiny["height"] == 10, tiny  # floor clamp 10..10000
    assert tiny["quality"] == 1, tiny

    stop = _loads(await screencast_mirage.mirage_screencast_stop())
    assert stop["stopped"] is True, stop


# ---------------------------------------------------------------------------
# Scenario B: real JPEG frames flow, ack keeps the stream alive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screencast_frames_flow_and_ack(mirage_tools: None, http_server: str) -> None:
    """Two consecutive frames arrive (ack works — a stalled stream would
    never deliver the second), both are real JPEGs, and their bytes differ
    (the canvas animation is actually being captured)."""
    await _navigate(f"{http_server}/anim")
    started = await _start()

    f1 = _loads(await screencast_mirage.mirage_screencast_frame(timeout=10.0))
    assert "data" in f1, f1
    assert f1["ack"]["sent"] is True, f1["ack"]
    assert f1["ack"]["screencastId"] == started["screencastId"], f1["ack"]
    _assert_jpeg(f1)

    await _paint_distinct_canvas()
    f2 = _loads(await screencast_mirage.mirage_screencast_frame(timeout=10.0, fresh=True))
    assert "data" in f2, f2
    assert f2["ack"]["sent"] is True, f2["ack"]
    _assert_jpeg(f2)

    assert f1["data"] != f2["data"], "two live frames must differ (page animates)"
    assert f1["deviceWidth"] == f2["deviceWidth"], (f1, f2)
    assert f1["deviceHeight"] == f2["deviceHeight"], (f1, f2)


# ---------------------------------------------------------------------------
# Scenario C: unacked frames queue until consumed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screencast_pending_queues_unacked_frames(
    mirage_tools: None, http_server: str
) -> None:
    """While nobody consumes frames, pending() reports the queued (unacked)
    frame with its stream id; the next frame() call consumes the OLDEST one
    and acks it (the frame bytes arrive even though no ack was sent before)."""
    await _navigate(f"{http_server}/anim")
    started = await _start()

    # No ack yet: the frame sits in the queue (Camoufox holds at 1 unacked).
    pending = await _poll_pending(lambda d: d["pending"] >= 1)
    assert pending["pending"] == 1, pending
    assert pending["screencastId"] == started["screencastId"], pending
    assert pending["active"] is True, pending
    assert pending["last"] and pending["last"]["dataLength"] > 0, pending

    # Consuming the queued frame acks it and hands it over.
    f1 = _loads(await screencast_mirage.mirage_screencast_frame(timeout=10.0))
    assert "data" in f1 and f1["ack"]["sent"] is True, f1
    _assert_jpeg(f1)

    # Stream resumes after the ack: a fresh frame arrives.
    await _paint_distinct_canvas()
    f2 = _loads(await screencast_mirage.mirage_screencast_frame(timeout=10.0, fresh=True))
    assert "data" in f2 and f2["ack"]["sent"] is True, f2
    assert f1["data"] != f2["data"], f1


# ---------------------------------------------------------------------------
# Scenario D: stop discards queued frames and closes the stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screencast_stop_clears_queue_and_halts_stream(
    mirage_tools: None, http_server: str
) -> None:
    """stop() reports the discarded queued frames, pending() drops to zero
    with the stream inactive, and a later frame() call times out — the
    stream is genuinely closed, no stale frames survive."""
    await _navigate(f"{http_server}/anim")
    await _start()

    await _poll_pending(lambda d: d["pending"] >= 1)

    stop = _loads(await screencast_mirage.mirage_screencast_stop())
    assert stop["stopped"] is True, stop
    assert stop["discardedFrames"] >= 1, stop

    pending = _loads(await screencast_mirage.mirage_screencast_pending())
    assert pending["pending"] == 0, pending
    assert pending["active"] is False, pending
    assert pending["screencastId"] is None, pending

    late = _loads(await screencast_mirage.mirage_screencast_frame(timeout=3.0))
    assert "error" in late and "no screencast frame within 3s" in late["error"], late
    assert "data" not in late, late
