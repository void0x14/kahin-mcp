"""Localhost-only MJPEG live watch for an already-running Mirage screencast.

The server requires an active screencast and never starts or stops Page's
screencast itself.  It races manual ``kahin_mirage_screencast_frame`` calls:
both consumers draw from the same engine frame pump.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kahin._mcp import mcp
from kahin.tools._common import _RW, _healer_ref, _mirage_engine, _require_mirage

_TOOL = "kahin_mirage_watch_start"
_BOUNDARY = b"kahinframe"
_state_lock = threading.RLock()
_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_pump_task: asyncio.Task[None] | None = None
_stop_event = threading.Event()
_latest_jpeg: bytes | None = None


def _mjpeg_part(jpeg: bytes, boundary: bytes = _BOUNDARY) -> bytes:
    """Return one complete multipart MJPEG chunk."""
    return (
        b"--" + boundary + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
        + jpeg + b"\r\n"
    )


def _json_error(message: str, code: str) -> str:
    return json.dumps({"error": message, "code": code}, separators=(",", ":"))


def _handler_class() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "KahinWatch/1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/", "/snapshot.jpg"):
                self._error(404, "not_found", "Unknown watch endpoint")
                return
            with _state_lock:
                jpeg = _latest_jpeg
            if jpeg is None:
                self._error(503, "no_frame", "No screencast frame is available yet")
                return
            if self.path == "/snapshot.jpg":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=kahinframe")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.connection.settimeout(1.0)
            while not _stop_event.is_set():
                with _state_lock:
                    current = _latest_jpeg
                if current is not None:
                    try:
                        self.wfile.write(_mjpeg_part(current))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                        break
                _stop_event.wait(0.2)

        def _error(self, status: int, code: str, message: str) -> None:
            body = _json_error(message, code).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


async def _pump(engine: Any, sid: str, owner_session: str | None, generation: int) -> None:
    global _latest_jpeg
    while not _stop_event.is_set() and engine.screencast_generation == generation:
        try:
            frame = await engine.wait_for_screencast_frame(timeout=5.0, generation=generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            break
        if frame is None:
            continue
        try:
            data = frame.get("data", "")
            jpeg = base64.b64decode(data, validate=True)
            if jpeg:
                with _state_lock:
                    _latest_jpeg = jpeg
        finally:
            try:
                await engine.call(
                    "Page.screencastFrameAck", {"screencastId": sid}, session_id=owner_session
                )
            except Exception:
                break


def _serve(server: ThreadingHTTPServer) -> None:
    server.serve_forever(poll_interval=0.1)


@mcp.tool(name="kahin_mirage_watch_start", annotations=_RW)
async def mirage_watch_start(port: int = 0) -> str:
    """Start a localhost-only MJPEG watch for the existing active screencast.

    This does not start a screencast; manual frame() calls race the watch pump.
    """
    global _server, _thread, _pump_task, _latest_jpeg
    async with _healer_ref.safe(_TOOL, port=port):
        err = await _require_mirage()
        if err:
            return err
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            return _json_error("port must be an integer from 0 to 65535", "invalid_argument")
        engine = _mirage_engine()
        async with engine._screencast_lock:
            sid = engine._screencast_id
            owner_session = engine.screencast_session_id
            generation = engine.screencast_generation
        if not sid:
            return _json_error("An active screencast is required", "screencast_not_running")
        with _state_lock:
            if _server is not None and _thread is not None and _thread.is_alive():
                actual = _server.server_address[1]
                return json.dumps({"watching": True, "url": f"http://127.0.0.1:{actual}/", "port": actual, "format": "mjpeg"})
            _stop_event.clear()
            _latest_jpeg = None
            server = ThreadingHTTPServer(("127.0.0.1", port), _handler_class())
            server.daemon_threads = True
            _server = server
            _thread = threading.Thread(target=_serve, args=(server,), daemon=True, name="kahin-mjpeg")
            _thread.start()
        _pump_task = asyncio.get_running_loop().create_task(_pump(engine, sid, owner_session, generation))
        actual = server.server_address[1]
        return json.dumps({"watching": True, "url": f"http://127.0.0.1:{actual}/", "port": actual, "format": "mjpeg"})


@mcp.tool(name="kahin_mirage_watch_stop", annotations=_RW)
async def mirage_watch_stop() -> str:
    """Stop the localhost MJPEG server and pump without stopping screencast."""
    global _server, _thread, _pump_task, _latest_jpeg
    async with _healer_ref.safe("kahin_mirage_watch_stop"):
        with _state_lock:
            server, thread, pump = _server, _thread, _pump_task
            _server = _thread = _pump_task = None
            _stop_event.set()
            _latest_jpeg = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        if pump is not None and not pump.done():
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        return json.dumps({"watching": False})

