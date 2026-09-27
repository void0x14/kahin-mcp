"""Real-browser end-to-end tests for the Mirage file upload tools (Gap C).

Proves kahin_mirage_upload_files over REAL Camoufox + a stdlib localhost
HTTP server: interception is enabled (Page.setInterceptFileChooserDialog),
the file input is clicked (Page.fileChooserOpened fires with the element's
objectId), the files are set with Page.setFileInputFiles, and the page then
POSTs a multipart form to /upload — the server parses the parts, so the
assertions are on the actual file bytes that left the browser, not on tool
return values alone.

Fixture endpoints:
  /upload -> HTML page: <input type="file" id="f" multiple> + submit button
  POST /upload -> parse multipart body, store {count, parts[]} in /last
  /last   -> JSON of the last stored upload (thread-safe)

Scenarios:
  a. intercept enable -> click input -> upload_files(one absolute path)
     -> server sees the exact bytes under the original filename.
  b. upload_files called BEFORE the click (wait-for-chooser path) with two
     files -> both arrive in one multipart POST.
  c. no click at all -> upload_files times out with a clear error (no fake
     success).
  d. relative path -> rejected with a clear error.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage, trainman_mirage, upload_mirage

_UPLOAD_HTML = """<!doctype html><html><body>
<input type="file" id="f" multiple>
<button id="up">Upload</button>
<div id="out">idle</div>
<script>
  document.getElementById('up').addEventListener('click', async () => {
    const input = document.getElementById('f');
    const out = document.getElementById('out');
    if (!input.files || !input.files.length) { out.textContent = 'no-files'; return; }
    const fd = new FormData();
    for (const file of input.files) fd.append(file.name, file);
    try {
      const r = await fetch('/upload', { method: 'POST', body: fd });
      out.textContent = r.status + ':' + (await r.text());
    } catch (e) { out.textContent = 'err:' + e.message; }
  });
</script>
</body></html>"""

_LAST: dict[str, Any] = {}
_LAST_LOCK = threading.Lock()


def _parse_multipart(body: bytes, boundary: str) -> list[dict[str, Any]]:
    """Minimal multipart/form-data parser (stdlib only).

    Chunk shape after splitting on --boundary:
    ``\\r\\n`` + headers + ``\\r\\n\\r\\n`` + content + ``\\r\\n``
    (leading CRLF ends the boundary marker line, trailing CRLF precedes the
    next marker). Only those framing CRLFs are removed — content bytes are
    preserved exactly, trailing newlines included.
    """
    parts: list[dict[str, Any]] = []
    for chunk in body.split(("--" + boundary).encode()):
        chunk = chunk.lstrip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if b"\r\n\r\n" not in chunk:
            continue
        headers, _, content = chunk.partition(b"\r\n\r\n")
        name = ""
        filename: str | None = None
        for line in headers.split(b"\r\n"):
            if not line.lower().startswith(b"content-disposition:"):
                continue
            m = re.search(rb'name="([^"]*)"', line)
            if m:
                name = m.group(1).decode("utf-8", "replace")
            m = re.search(rb'filename="([^"]*)"', line)
            if m:
                filename = m.group(1).decode("utf-8", "replace")
        # latin-1 round-trips every byte 1:1 into a JSON-safe str.
        parts.append({"name": name, "filename": filename, "content": content.decode("latin-1")})
    return parts


class _Handler(BaseHTTPRequestHandler):
    """Minimal local server: /upload (page + POST target) and /last."""

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/upload":
            self._send(_UPLOAD_HTML.encode(), "text/html")
        elif self.path == "/last":
            with _LAST_LOCK:
                self._send(json.dumps(_LAST).encode(), "application/json")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/upload":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ct = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ct)
        parts = _parse_multipart(body, m.group(1)) if m else []
        with _LAST_LOCK:
            _LAST.clear()
            _LAST.update({"count": len(parts), "parts": parts})
        self._send(f"ok:{len(parts)}".encode(), "text/plain")

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
    with _LAST_LOCK:
        _LAST.clear()
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


async def _poll_last(base: str, pred, timeout: float = 15.0, interval: float = 0.2) -> Any:
    """Poll GET /last until pred(parsed json) is truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            resp = await client.get(f"{base}/last")
            data = resp.json()
            if pred(data):
                return data
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"/last condition not met within {timeout}s; saw {data!r}")
            await asyncio.sleep(interval)


async def _poll_text(selector: str, pred, timeout: float = 15.0, interval: float = 0.2) -> str:
    """Poll page text until pred(text) is truthy; returns the text."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        raw = await pilot_mirage.mirage_get_text(selector)
        text = _loads(raw)
        if isinstance(text, str) and pred(text):
            return text
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"text of {selector} not matching within {timeout}s; last: {text!r}")
        await asyncio.sleep(interval)


async def _enable_intercept() -> None:
    resp = _loads(await upload_mirage.mirage_set_file_chooser_intercept(True))
    assert resp["enabled"] is True, resp


# ---------------------------------------------------------------------------
# Scenario A: single file, chooser already open when upload_files is called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_single_file(mirage_tools: None, http_server: str, tmp_path: Any) -> None:
    """intercept -> click the input -> upload_files(one file) -> the server
    receives the exact file bytes under the original filename."""
    await _navigate(f"{http_server}/upload")
    await _enable_intercept()

    f = tmp_path / "test.txt"
    f.write_text("hello from kahin upload\n")

    clicked = _loads(await pilot_mirage.mirage_click("#f"))
    assert clicked["clicked"] == "#f", clicked

    up = _loads(await upload_mirage.mirage_upload_files([str(f)]))
    assert up.get("uploaded") is True, up
    assert up["files"] == 1, up
    assert up["names"] == ["test.txt"], up
    assert up["chooser"]["objectId"], up

    await pilot_mirage.mirage_click("#up")
    last = await _poll_last(http_server, lambda d: d.get("count") == 1)
    part = last["parts"][0]
    assert part["filename"] == "test.txt", last
    assert part["content"] == "hello from kahin upload\n", last

    text = await _poll_text("#out", lambda t: t.startswith("200:"))
    assert text == "200:ok:1", text

    resp = _loads(await upload_mirage.mirage_set_file_chooser_intercept(False))
    assert resp["enabled"] is False, resp


# ---------------------------------------------------------------------------
# Scenario B: two files in one call, chooser opens AFTER upload_files starts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_multiple_files_waits_for_chooser(
    mirage_tools: None, http_server: str, tmp_path: Any
) -> None:
    """upload_files is called BEFORE the click: it must wait for
    Page.fileChooserOpened, then set BOTH files in one call."""
    await _navigate(f"{http_server}/upload")
    await _enable_intercept()

    a = tmp_path / "alpha.txt"
    a.write_text("alpha content")
    b = tmp_path / "beta.txt"
    b.write_text("beta content 42")

    task = asyncio.create_task(
        upload_mirage.mirage_upload_files([str(a), str(b)], timeout=15.0)
    )
    await asyncio.sleep(0.4)  # tool must be waiting, not done
    assert not task.done(), "upload_files returned before any chooser opened"

    clicked = _loads(await pilot_mirage.mirage_click("#f"))
    assert clicked["clicked"] == "#f", clicked

    up = _loads(await task)
    assert up.get("uploaded") is True, up
    assert up["files"] == 2, up
    assert set(up["names"]) == {"alpha.txt", "beta.txt"}, up

    await pilot_mirage.mirage_click("#up")
    last = await _poll_last(http_server, lambda d: d.get("count") == 2)
    by_name = {p["filename"]: p["content"] for p in last["parts"]}
    assert by_name == {"alpha.txt": "alpha content", "beta.txt": "beta content 42"}, last

    text = await _poll_text("#out", lambda t: t.startswith("200:"))
    assert text == "200:ok:2", text


# ---------------------------------------------------------------------------
# Scenario C: no chooser opens -> clean timeout, no fake success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_timeout_no_chooser(mirage_tools: None, http_server: str, tmp_path: Any) -> None:
    """Interception on but the input is never clicked: upload_files must
    report a timeout error instead of claiming success."""
    await _navigate(f"{http_server}/upload")
    await _enable_intercept()

    f = tmp_path / "test.txt"
    f.write_text("x")

    up = _loads(await upload_mirage.mirage_upload_files([str(f)], timeout=1.5))
    assert "error" in up, up
    assert "no file chooser opened within 1.5s" in up["error"], up
    assert "uploaded" not in up, up


# ---------------------------------------------------------------------------
# Scenario D: relative paths are rejected up front
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_rejects_relative_paths(mirage_tools: None, http_server: str) -> None:
    """Relative file paths fail validation with a clear error (Juggler needs
    absolute paths — Playwright's driver rejects relatives too)."""
    await _navigate(f"{http_server}/upload")

    up = _loads(await upload_mirage.mirage_upload_files(["test.txt"]))
    assert "error" in up and "absolute" in up["error"], up
    assert "uploaded" not in up, up

    empty = _loads(await upload_mirage.mirage_upload_files([]))
    assert "error" in empty and "non-empty" in empty["error"], empty
