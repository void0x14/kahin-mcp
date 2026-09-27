"""Real-accessibility end-to-end tests for the Mirage AX tool (Gap E).

Proves kahin_mirage_accessibility_tree over REAL Camoufox data — the
tree comes from Accessibility.getFullAXTree (a Camoufox-only Juggler
method; upstream Playwright has no Accessibility domain). The page is
served by a stdlib ``http.server`` thread on 127.0.0.1.

Fixture page (accessible markup):
  <h1>Shop</h1>
  <button aria-label="Close">X</button>
  <input placeholder="Email">

Juggler AX facts (vendored Protocol.js):
- getFullAXTree targets ['page'] — the sidecar must forward it on the
  PAGE session (isPageDomain), otherwise the root session rejects it
  ("Handler for does not implement method Accessibility.getFullAXTree").
- Firefox AX role names differ from CDP: <button> is "pushbutton",
  <input> is "entry" (CDP: button/textbox), text runs are "text leaf".
- Placeholder IS reflected: the <input> node's name is "Email"
  (verified against real Camoufox output).
- The tree is recursive: {role, name, children?, tag?, focusable?,
  editable?, level?, ...}.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import accessibility_mirage, pilot, trainman_mirage

_AX_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>AX Fixture</title></head><body>
<h1>Shop</h1>
<button aria-label="Close">X</button>
<input placeholder="Email">
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    """Minimal local server: /ax serves the accessible fixture page."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/ax":
            body = _AX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
    await asyncio.sleep(0.5)  # AX tree settles after load


def _walk(tree: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Depth-first walk of an AXTree node."""
    yield tree
    for child in tree.get("children") or []:
        yield from _walk(child)


def _find(tree: dict[str, Any], **props: Any) -> list[dict[str, Any]]:
    """All AX nodes matching every given property (role/name/...)."""
    return [n for n in _walk(tree) if all(n.get(k) == v for k, v in props.items())]


def _tree_count(tree: dict[str, Any]) -> int:
    """Node count of a (possibly truncated) returned tree."""
    return sum(1 for _ in _walk(tree))


# ---------------------------------------------------------------------------
# Scenario A: the tree is REAL AX data — button role + aria-label name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ax_tree_button_role_and_name(mirage_tools: None, http_server: str) -> None:
    """The AX tree contains a role=pushbutton node named Close (aria-label)."""
    await _navigate(f"{http_server}/ax")

    raw = await accessibility_mirage.mirage_accessibility_tree()
    data = _loads(raw)
    assert data.get("error") is None, raw
    tree = data["tree"]

    buttons = _find(tree, role="pushbutton", name="Close")
    assert buttons, "no AX node with role=pushbutton name=Close"

    # The node also exposes the standard button state flags + source tag.
    button = buttons[0]
    for key in ("role", "name", "focusable", "tag"):
        assert key in button, f"AX button node missing {key}: {button}"
    assert button["tag"] == "button", button


# ---------------------------------------------------------------------------
# Scenario B: input shows up as a textbox; placeholder reflection verified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ax_tree_input_textbox(mirage_tools: None, http_server: str) -> None:
    """The <input> appears as role=entry named Email: Firefox reflects the
    placeholder into the accessible name (verified against real output)."""
    await _navigate(f"{http_server}/ax")

    raw = await accessibility_mirage.mirage_accessibility_tree()
    data = _loads(raw)
    assert data.get("error") is None, raw
    tree = data["tree"]

    entries = _find(tree, role="entry", name="Email")
    assert entries, f"no AX node with role=entry name=Email: {_loads(raw)['nodeCount']} nodes"

    entry = entries[0]
    for key in ("role", "name", "editable", "tag"):
        assert key in entry, f"AX entry node missing {key}: {entry}"
    assert entry["editable"] is True, entry
    assert entry["tag"] == "input", entry


# ---------------------------------------------------------------------------
# Scenario C: nodeCount + truncation semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ax_node_count_and_truncation(mirage_tools: None, http_server: str) -> None:
    """nodeCount is the FULL tree size; truncated=true cuts the payload."""
    await _navigate(f"{http_server}/ax")

    # Small budget: page has more than 3 AX nodes -> truncated.
    small = _loads(await accessibility_mirage.mirage_accessibility_tree(max_nodes=3))
    assert small.get("error") is None, small
    assert small["truncated"] is True, small
    assert small["nodeCount"] > 3, small
    assert _tree_count(small["tree"]) <= 3, small
    # The full count is preserved even when the payload is cut.
    assert small["nodeCount"] > _tree_count(small["tree"]), small

    # Huge budget: everything returned, nothing cut.
    full = _loads(await accessibility_mirage.mirage_accessibility_tree(max_nodes=100000))
    assert full.get("error") is None, full
    assert full["nodeCount"] > 0, full
    assert full["truncated"] is False, full
    assert _tree_count(full["tree"]) == full["nodeCount"], full

    # Both calls agree on the real tree size (independent of budget).
    assert full["nodeCount"] == small["nodeCount"], (small, full)

    # The button is still reachable through the truncated dump.
    buttons = _find(small["tree"], role="pushbutton", name="Close")
    assert buttons, f"button lost from truncated tree: {small['tree']}"

    # max_nodes <= 0 is rejected, not silently ignored.
    bad = _loads(await accessibility_mirage.mirage_accessibility_tree(max_nodes=0))
    assert "error" in bad, bad


@pytest.mark.asyncio
async def test_ax_tree_heading(mirage_tools: None, http_server: str) -> None:
    """<h1> maps to role=heading with its text as the name."""
    await _navigate(f"{http_server}/ax")

    raw = await accessibility_mirage.mirage_accessibility_tree()
    data = _loads(raw)
    assert data.get("error") is None, raw

    headings = _find(data["tree"], role="heading", name="Shop")
    assert headings, "no AX node with role=heading name=Shop"
