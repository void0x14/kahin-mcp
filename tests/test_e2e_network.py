"""Real-HTTP end-to-end tests for the Mirage network tool layer (Gap A).

Proves the network tools over REAL HTTP traffic — the old suite could not
(data: URLs emit no Network.* events; that is a data:-URL artifact, NOT a
tool bug, see test_e2e_mirage.py). The page is served by a stdlib
``http.server`` thread on 127.0.0.1 — no external network deps.

Fixture endpoints:
  /json  -> {"hello": "world"} with Content-Type application/json
  /slow  -> sleeps 1s, then 200 (in-flight window for interception)
  /form  -> HTML with a button that fetches /json on click, plus a second
            button that fetches /slow (used by the interception scenarios)

Juggler Network facts (camoufox-harness/core/adapters/network.zig):
- There is NO Network.enable; requestWillBeSent/responseReceived/
  requestFinished/requestFailed fire by default once the page session
  attaches. Playwright never sends enable.
- requestWillBeSent flattens url/method/isIntercepted into top-level
  params; responseReceived carries status at top level too.
- The interception signal is requestWillBeSent with isIntercepted: true —
  the request stays paused until resumeInterceptedRequest /
  abortInterceptedRequest (or fulfillInterceptedRequest) is called with
  its requestId.
- Network.getResponseBody works WITHOUT interception and returns
  {base64body, evicted?}.

Timing note: the sidecar buffers events dispatched while a request is in
flight and flushes them only after that request completes (ipc_main.zig
eventSink), and Page.navigate waits for the load lifecycle. So the
interception scenarios navigate FIRST, enable interception, then click —
enabling interception before Page.navigate would pause the document
request while navigate() blocks on load (deadlock).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import dejavu_mirage, pilot, pilot_mirage, trainman_mirage

_FORM_HTML = """<!doctype html><html><body>
<button id="btn-json">Fetch JSON</button>
<button id="btn-slow">Fetch Slow</button>
<div id="json-out">idle</div>
<div id="slow-out">idle</div>
<script>
  document.getElementById('btn-json').addEventListener('click', async () => {
    try {
      const r = await fetch('/json');
      const t = await r.text();
      document.getElementById('json-out').textContent = r.status + ':' + t;
    } catch (e) {
      document.getElementById('json-out').textContent = 'err:' + e.message;
    }
  });
  document.getElementById('btn-slow').addEventListener('click', async () => {
    try {
      const r = await fetch('/slow');
      await r.text();
      document.getElementById('slow-out').textContent = 'ok:' + r.status;
    } catch (e) {
      document.getElementById('slow-out').textContent = 'err:' + e.message;
    }
  });
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    """Minimal local server: /json, /slow (1s delay), /form."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/json":
            body = b'{"hello": "world"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            time.sleep(1.0)
            body = b"slow-done"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/form":
            body = _FORM_HTML.encode()
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


def _sent_to(reqs: list[dict[str, Any]], url_part: str) -> dict[str, Any] | None:
    """First requestWillBeSent entry whose URL contains url_part."""
    for e in reqs:
        if e["event"] == "requestWillBeSent" and url_part in (e["params"].get("url") or ""):
            return e
    return None


def _response_for(reqs: list[dict[str, Any]], request_id: str) -> dict[str, Any] | None:
    """responseReceived entry for a requestId."""
    for e in reqs:
        if e["event"] == "responseReceived" and e["params"].get("requestId") == request_id:
            return e
    return None


def _intercepted_sent(reqs: list[dict[str, Any]], url_part: str) -> dict[str, Any] | None:
    """requestWillBeSent entry that isIntercepted and whose URL contains url_part."""
    for e in reqs:
        if (
            e["event"] == "requestWillBeSent"
            and e["params"].get("isIntercepted") is True
            and url_part in (e["params"].get("url") or "")
        ):
            return e
    return None


async def _poll_requests(pred, timeout: float = 15.0, interval: float = 0.2) -> Any:
    """Poll kahin_mirage_network_requests until pred(list) is truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        reqs = _loads(await dejavu_mirage.mirage_network_requests(detail=True))
        hit = pred(reqs)
        if hit:
            return hit
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"network condition not met within {timeout}s; saw {len(reqs)} entries"
            )
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


# ---------------------------------------------------------------------------
# Scenario A: request listing over real HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_requests_lists_real_fetch(mirage_tools: None, http_server: str) -> None:
    """navigate -> click fetch -> request entries carry the /json GET + 200."""
    await _navigate(f"{http_server}/form")

    clicked = await pilot_mirage.mirage_click("#btn-json")
    assert _loads(clicked)["clicked"] == "#btn-json", clicked

    sent = await _poll_requests(lambda r: _sent_to(r, "/json"))
    assert sent["params"]["method"] == "GET", sent
    request_id = sent["params"]["requestId"]
    assert request_id, sent

    recv = await _poll_requests(lambda r: _response_for(r, request_id))
    assert recv["params"]["status"] == 200, recv

    # The page itself saw the response too (independent evidence).
    text = await _poll_text("#json-out", lambda t: t.startswith("200:"))
    assert text == '200:{"hello": "world"}', text


# ---------------------------------------------------------------------------
# Scenario B: response body over real HTTP (base64 decode path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_get_response_body(mirage_tools: None, http_server: str) -> None:
    """get_response_body on the /json requestId returns the decoded body."""
    await _navigate(f"{http_server}/form")
    await pilot_mirage.mirage_click("#btn-json")

    sent = await _poll_requests(lambda r: _sent_to(r, "/json"))
    request_id = sent["params"]["requestId"]

    body = _loads(await dejavu_mirage.mirage_get_response_body(request_id))
    assert "hello" in body["body"] and "world" in body["body"], body
    assert body["body"] == '{"hello": "world"}', body


@pytest.mark.asyncio
async def test_raw_cdp_get_response_body_is_cdp_shaped(
    mirage_tools: None, http_server: str
) -> None:
    """The generic CDP entry point decodes Juggler's base64body into the
    Chrome-shaped {body, base64Encoded} contract instead of leaking a native
    implementation detail."""
    await _navigate(f"{http_server}/form")
    await pilot_mirage.mirage_click("#btn-json")
    sent = await _poll_requests(lambda r: _sent_to(r, "/json"))
    request_id = sent["params"]["requestId"]

    body = _loads(await pilot.execute_cdp("Network", "getResponseBody", {"requestId": request_id}))
    assert body["body"] == '{"hello": "world"}', body
    assert body["base64Encoded"] is False, body


# ---------------------------------------------------------------------------
# Scenario C: interception -> continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_intercept_continue(mirage_tools: None, http_server: str) -> None:
    """intercept -> click /slow -> shows isIntercepted -> continue -> completes."""
    await _navigate(f"{http_server}/form")  # navigate first (see module docstring)

    assert _loads(await dejavu_mirage.mirage_intercept_requests()) == {}

    await pilot_mirage.mirage_click("#btn-slow")

    # The /slow request is PAUSED: requestWillBeSent carries isIntercepted.
    sent = await _poll_requests(lambda r: _intercepted_sent(r, "/slow"))
    request_id = sent["params"]["requestId"]
    assert request_id, sent

    resumed = await dejavu_mirage.mirage_network_continue(request_id)
    assert _loads(resumed) == {}, resumed

    # Resumed -> server sleeps 1s -> 200; requestFinished follows.
    recv = await _poll_requests(lambda r: _response_for(r, request_id), timeout=20.0)
    assert recv["params"]["status"] == 200, recv

    await _poll_requests(
        lambda r: any(e["event"] == "requestFinished" and e["params"].get("requestId") == request_id for e in r),
        timeout=20.0,
    )

    text = await _poll_text("#slow-out", lambda t: t.startswith("ok:"), timeout=20.0)
    assert text == "ok:200", text

    assert _loads(await dejavu_mirage.mirage_unintercept_requests()) == {}


# ---------------------------------------------------------------------------
# Scenario D: interception -> abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_intercept_abort(mirage_tools: None, http_server: str) -> None:
    """intercept -> abort /slow -> page fetch fails -> error visible."""
    await _navigate(f"{http_server}/form")

    assert _loads(await dejavu_mirage.mirage_intercept_requests()) == {}

    await pilot_mirage.mirage_click("#btn-slow")

    sent = await _poll_requests(lambda r: _intercepted_sent(r, "/slow"))
    request_id = sent["params"]["requestId"]
    assert request_id, sent

    aborted = await dejavu_mirage.mirage_network_abort(request_id, error_code="NS_BINDING_ABORTED")
    assert _loads(aborted) == {}, aborted

    # The page's fetch rejects; the catch handler records the failure.
    text = await _poll_text("#slow-out", lambda t: t.startswith("err:"))
    assert "err:" in text, text

    # requestFailed arrives for the same requestId.
    await _poll_requests(
        lambda r: any(e["event"] == "requestFailed" and e["params"].get("requestId") == request_id for e in r)
    )

    assert _loads(await dejavu_mirage.mirage_unintercept_requests()) == {}
