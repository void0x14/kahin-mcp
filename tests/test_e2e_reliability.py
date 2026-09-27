"""Tool-layer reliability e2e over real Camoufox (Faz 1, Tasks 4-5).

Covers the locator-engine adoption on query/query_all/type/wait_selector,
the wait_selector ``state=attached|visible|enabled`` contract, and the
actionability waits on click (animation stability + disabled-becomes-enabled).
Every scenario drives the same async functions the MCP server exposes, so the
JSON-string tool contract is what gets verified.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import (
    agent_mirage,
    dejavu_mirage,
    dom_stream_mirage,
    pilot,
    pilot_mirage,
    reliability_mirage,
    storage_mirage,
    trainman_mirage,
)


def _real_available() -> bool:
    """True when the sidecar binary and a real Camoufox are both present."""
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing"
)


def _doc(body: str) -> str:
    """A data: URL carrying an HTML document (no network involved)."""
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


def _eval_value(text: str) -> Any:
    """Unwrap kahin_evaluate's CDP-shaped {result: {value}} payload."""
    payload = _loads(text)
    if isinstance(payload, dict):
        return (payload.get("result") or {}).get("value")
    return payload


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    """Start real Camoufox through kahin_browser_start with its one tab, then stop."""
    resp = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tabs = _loads(await trainman_mirage.mirage_tab_list())
        assert len(tabs) == 1 and tabs[0].get("targetId"), tabs
        await asyncio.sleep(0.5)  # session/frame events settle
        yield
    finally:
        await pilot.browser_stop()


class _RouteHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.endswith("/form"):
            body = (
                b"<html><body><button id='load'>load</button><script>"
                b"window.loadPage=()=>fetch('/page.html').then(r=>r.text()).then(t=>document.body.dataset.payload=t);"
                b"</script></body></html>"
            )
        elif self.path.endswith("/state"):
            body = (
                b"<html><body>state</body><script>"
                b"localStorage.setItem('k1', 'from-page');"
                b"sessionStorage.setItem('s1', 'from-page');"
                b"document.cookie = 'c1=from-page; path=/';"
                b"</script></html>"
            )
        else:
            body = b"<html><body>origin</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: Any) -> None:
        del args


@async_fixture
async def http_server() -> AsyncGenerator[str, None]:
    server = HTTPServer(("127.0.0.1", 0), _RouteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/page.html"
    finally:
        server.shutdown()
        server.server_close()


async def _navigate(url: str) -> None:
    resp = _loads(await pilot.navigate(url=url))
    assert isinstance(resp, dict) and resp.get("frameId"), resp


@pytest.mark.asyncio
async def test_navigate_wait_until_domcontentloaded(mirage_tools: None) -> None:
    resp = _loads(await pilot.navigate(
        url=_doc("<html><body><p id='x'>hi</p></body></html>"),
        wait_until="domcontentloaded",
    ))
    assert resp.get("frameId"), resp
    assert resp.get("wait_until") == "domcontentloaded", resp


@pytest.mark.asyncio
async def test_navigate_wait_until_timeout(mirage_tools: None) -> None:
    resp = _loads(await pilot.navigate(
        url=_doc("<html><body>x</body></html>"),
        wait_until="networkidle",
        timeout=0.5,
    ))
    assert resp.get("code") == "navigation_timeout", resp


@pytest.mark.asyncio
async def test_snapshot_refs_and_token_budget(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><h1>Todo</h1><input id='t' placeholder='What next?'>"
        "<button id='b'>Add</button></body></html>"
    ))
    result = _loads(await agent_mirage.mirage_snapshot(max_tokens=800))
    assert result.get("lines"), result
    assert result.get("tokens_estimate", 0) > 0, result
    assert result.get("max_tokens") == 800, result
    assert result.get("truncated") is not None, result
    assert result.get("streamId") and result.get("cursor") is not None, result
    joined = "\n".join(result["lines"])
    assert "textbox" in joined and "Add" in joined, joined
    # refs must be action-ready: click the Add button via its live nodeId.
    add_line = next(line for line in result["lines"] if "Add" in line)
    ref = add_line.split("ref=")[1].split("]")[0]
    assert ref.startswith("n"), add_line
    action = _loads(await dom_stream_mirage.mirage_dom_action(node_id=ref, action="click"))
    assert not action.get("error"), action
    assert action.get("action") == "click", action
    assert isinstance(action.get("target"), dict), action


@pytest.mark.asyncio
async def test_fill_form_by_refs(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><input id='a' placeholder='Name'><input id='b' placeholder='Email'>"
        "<button id='s'>Save</button></body></html>"
    ))
    snap = _loads(await agent_mirage.mirage_snapshot(max_tokens=800))
    refs: dict[str, str] = {}
    for line in snap["lines"]:
        if "Name" in line:
            refs["name"] = line.split("ref=")[1].split("]")[0]
        if "Email" in line:
            refs["email"] = line.split("ref=")[1].split("]")[0]
    assert "name" in refs and "email" in refs, snap
    result = _loads(await agent_mirage.mirage_fill_form(fields=[
        {"ref": refs["name"], "text": "Ada"},
        {"ref": refs["email"], "text": "ada@example.com"},
    ]))
    assert result.get("filled") == 2, result
    assert not result.get("error"), result
    v1 = _loads(await pilot_mirage.mirage_get_value("#a"))
    v2 = _loads(await pilot_mirage.mirage_get_value("#b"))
    assert v1 == "Ada" and v2 == "ada@example.com", (v1, v2)


@pytest.mark.asyncio
async def test_click_returns_snapshot_when_requested(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button id='b'>Toggle</button><div id='o'>off</div>"
        "<script>document.getElementById('b').addEventListener('click', () => { "
        "document.getElementById('o').textContent = 'on'; });</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("#b", timeout=5.0, return_snapshot=True))
    assert not result.get("error"), result
    assert result.get("snapshot", {}).get("lines"), result
    joined = "\n".join(result["snapshot"]["lines"])
    assert "on" in joined or "Toggle" in joined, joined


@pytest.mark.asyncio
async def test_wait_selector_text_engine_and_state(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button style='display:none' id='b1'>Hidden</button>"
        "<button id='b2'>Visible</button></body></html>"
    ))
    hidden = _loads(await pilot_mirage.mirage_wait_selector(
        "text=Hidden", timeout=1.0, state="visible",
    ))
    assert hidden.get("found") is False
    visible = _loads(await pilot_mirage.mirage_wait_selector(
        "text=Visible", timeout=3.0, state="visible",
    ))
    assert visible.get("found") is True


@pytest.mark.asyncio
async def test_wait_selector_waits_for_appearance(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><div id='late'></div>"
        "<script>setTimeout(() => { const b = document.createElement('button'); "
        "b.id = 'later'; b.textContent = 'Later'; document.body.appendChild(b); }, 600);</script>"
        "</body></html>"
    ))
    started = time.monotonic()
    result = _loads(await pilot_mirage.mirage_wait_selector("#later", timeout=5.0))
    elapsed = time.monotonic() - started
    assert result.get("found") is True
    assert elapsed >= 0.4, f"did not actually wait: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_click_waits_for_animation_stability(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><head><style>"
        "#btn { position: absolute; left: 0; top: 0; width: 120px; height: 40px; "
        "transition: left 0.8s ease; }"
        "</style></head><body>"
        "<button id='btn' style='left: 0'>Move</button>"
        "<div id='log'></div>"
        "<script>"
        "const btn = document.getElementById('btn');"
        "setTimeout(() => { btn.style.left = '200px'; }, 50);"
        "btn.addEventListener('click', () => { document.getElementById('log').textContent = 'clicked'; });"
        "</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("#btn", timeout=5.0))
    assert not result.get("error"), result
    assert result.get("clicked") == "#btn"
    text = _loads(await pilot_mirage.mirage_get_text("#log"))
    assert "clicked" in text


@pytest.mark.asyncio
async def test_click_waits_for_enabled(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button id='b' disabled>Go</button>"
        "<script>setTimeout(() => { document.getElementById('b').disabled = false; }, 500);"
        "</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("text=Go", timeout=5.0))
    assert not result.get("error"), result


@pytest.mark.asyncio
async def test_expect_retries_text_and_reports_actual(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><div id='s'>Loading</div>"
        "<script>setTimeout(() => { document.getElementById('s').textContent = 'Ready now'; }, 400);</script>"
        "</body></html>"
    ))
    matched = _loads(await reliability_mirage.mirage_expect("#s", text="Ready now", timeout=5.0))
    assert matched.get("matched") is True, matched
    assert matched.get("actual", {}).get("text") == "Ready now", matched
    failed = _loads(await reliability_mirage.mirage_expect("#s", text="Never", timeout=0.4))
    assert failed.get("code") == "expectation_failed", failed
    assert "Ready now" in str(failed.get("actual")), failed


@pytest.mark.asyncio
async def test_check_select_and_dblclick(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><input type='checkbox' id='c'>"
        "<select id='s'><option value='a'>Alpha</option><option value='b'>Bravo</option></select>"
        "<button id='b'>x</button><div id='n'>0</div>"
        "<script>"
        "let n=0; document.getElementById('c').addEventListener('change', () => document.getElementById('c').dataset.changed='1');"
        "document.getElementById('b').addEventListener('click', () => { n += 1; document.getElementById('n').textContent=String(n); });"
        "</script></body></html>"
    ))
    checked = _loads(await reliability_mirage.mirage_check("#c", timeout=5.0))
    assert checked.get("checked") is True and checked.get("changed") is True, checked
    unchecked = _loads(await reliability_mirage.mirage_uncheck("#c", timeout=5.0))
    assert unchecked.get("checked") is False, unchecked
    selected = _loads(await reliability_mirage.mirage_select_option("#s", value="b", timeout=5.0))
    assert selected.get("selected") == "b", selected
    missing = _loads(await reliability_mirage.mirage_select_option("#s", value="zzz", timeout=1.0))
    assert missing.get("code") == "option_not_found", missing
    dbl = _loads(await reliability_mirage.mirage_dblclick("#b", timeout=5.0))
    assert not dbl.get("error"), dbl
    count = _loads(await pilot_mirage.mirage_get_text("#n"))
    assert count == "2", count


@pytest.mark.asyncio
async def test_drag_and_wait_helpers(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><head><style>#a,#b{position:absolute;top:10px;width:100px;height:50px}"
        "#a{left:10px}#b{left:300px}</style></head><body>"
        "<div id='a'>Source</div><div id='b'>Target</div><div id='s'></div>"
        "<script>setTimeout(() => document.getElementById('s').textContent='ready-now', 250);</script>"
        "</body></html>"
    ))
    dragged = _loads(await reliability_mirage.mirage_drag("#a", "#b", timeout=5.0, steps=5))
    assert not dragged.get("error") and dragged.get("steps") == 5, dragged
    found = _loads(await reliability_mirage.mirage_wait_for_text("ready-now", timeout=3.0))
    assert found.get("found") is True, found
    started = time.monotonic()
    waited = _loads(await reliability_mirage.mirage_wait_for_timeout(ms=150))
    assert waited.get("waited_ms") == 150 and time.monotonic() - started >= 0.12, waited


@pytest.mark.asyncio
async def test_route_abort_matches_pattern(mirage_tools: None, http_server: str) -> None:
    await pilot.navigate(url=http_server.replace("/page.html", "/form"))
    intercepted = _loads(await dejavu_mirage.mirage_intercept_requests())
    assert not intercepted.get("error"), intercepted
    route_task = asyncio.create_task(reliability_mirage.mirage_route("*/page.html", action="abort", wait_ms=8_000))
    await pilot.evaluate(expression="window.loadPage(); true")
    route = _loads(await asyncio.wait_for(route_task, timeout=8.0))
    assert route.get("matched") is True, route
    assert route.get("action") == "abort", route
    assert "/page.html" in route.get("url", ""), route


@pytest.mark.asyncio
async def test_route_fulfill_inline_body(mirage_tools: None, http_server: str) -> None:
    await pilot.navigate(url=http_server.replace("/page.html", "/form"))
    intercepted = _loads(await dejavu_mirage.mirage_intercept_requests())
    assert not intercepted.get("error"), intercepted
    route_task = asyncio.create_task(reliability_mirage.mirage_route(
        "*/page.html",
        action="fulfill",
        body="MOCKED",
        status=200,
        headers={"X-Kahin": "route"},
        wait_ms=8_000,
    ))
    await pilot.evaluate(expression="window.loadPage(); true")
    route = _loads(await asyncio.wait_for(route_task, timeout=8.0))
    assert route.get("matched") is True, route
    await reliability_mirage.mirage_wait_for_timeout(ms=300)
    content = _loads(await pilot_mirage.mirage_page_content())
    assert "MOCKED" in str(content.get("html", "")), content


@pytest.mark.asyncio
async def test_state_save_load_roundtrip(
    mirage_tools: None, http_server: str, tmp_path: Path,
) -> None:
    """Real-origin roundtrip: save, wipe, load, and prove every section
    (url, cookies, localStorage, sessionStorage) was genuinely restored."""
    state_url = http_server.replace("/page.html", "/state")
    await _navigate(state_url)
    await reliability_mirage.mirage_wait_for_timeout(ms=250)
    # Overwrite the page-provided markers with the values we persist, so a
    # post-load assertion can distinguish restored data from page scripts.
    overwritten = _eval_value(await pilot.evaluate(
        expression=(
            "localStorage.setItem('k1', 'v1');"
            "sessionStorage.setItem('s1', 'sv');"
            "document.cookie = 'c1=vv; path=/'; true"
        ),
    ))
    assert overwritten is True, overwritten

    save_path = str(tmp_path / "state.json")
    saved = _loads(await agent_mirage.mirage_state_save(save_path))
    assert saved.get("saved") is True, saved
    assert saved.get("path") == save_path, saved
    assert saved.get("cookies") == 1, saved
    assert saved.get("localStorage") == 1, saved
    assert saved.get("sessionStorage") == 1, saved
    assert saved.get("url") == state_url, saved

    payload = _loads(Path(save_path).read_text(encoding="utf-8"))
    assert payload.get("version") == 1, payload
    assert payload["localStorage"]["k1"] == "v1", payload
    assert payload["sessionStorage"]["s1"] == "sv", payload
    assert any(c.get("name") == "c1" and c.get("value") == "vv" for c in payload["cookies"]), payload
    assert payload["url"] == state_url, payload

    # Wipe the live session on the same origin, then prove it is empty.
    await _navigate(http_server)
    await pilot.evaluate(expression="localStorage.clear(); sessionStorage.clear(); true")
    await storage_mirage.mirage_cookie_clear()
    await reliability_mirage.mirage_wait_for_timeout(ms=200)
    assert _loads(await storage_mirage.mirage_storage_local_get()) == [], "wipe failed"
    assert _loads(await storage_mirage.mirage_storage_session_get()) == [], "wipe failed"
    wiped_cookies = _loads(await storage_mirage.mirage_cookie_get())
    assert wiped_cookies.get("cookies") == [], wiped_cookies

    loaded = _loads(await agent_mirage.mirage_state_load(save_path))
    assert loaded.get("loaded") is True, loaded
    assert loaded.get("path") == save_path, loaded
    assert loaded.get("url") == state_url, loaded
    assert loaded.get("cookies") == 1, loaded
    assert loaded.get("localStorage") == 1, loaded
    assert loaded.get("sessionStorage") == 1, loaded

    # url restored (navigate back to the saved origin/page)
    href = _eval_value(await pilot.evaluate(expression="location.href"))
    assert href == state_url, href
    # cookies restored
    cookies = _loads(await storage_mirage.mirage_cookie_get())
    names = {c.get("name"): c.get("value") for c in cookies.get("cookies", [])}
    assert names.get("c1") == "vv", cookies
    # localStorage and sessionStorage restored (not the page-set markers)
    local = _loads(await storage_mirage.mirage_storage_local_get())
    assert isinstance(local, list) and {"key": "k1", "value": "v1"} in local, local
    session = _loads(await storage_mirage.mirage_storage_session_get())
    assert isinstance(session, list) and {"key": "s1", "value": "sv"} in session, session


@pytest.mark.asyncio
async def test_state_paths_reject_relative(mirage_tools: None) -> None:
    saved = _loads(await agent_mirage.mirage_state_save("relative/state.json"))
    assert saved.get("code") == "invalid_argument", saved
    assert "absolute" in str(saved.get("error", "")), saved
    loaded = _loads(await agent_mirage.mirage_state_load("relative/state.json"))
    assert loaded.get("code") == "invalid_argument", loaded


@pytest.mark.asyncio
async def test_identity_save_list_report_and_delete() -> None:
    """Identity surface without an engine: name/config validation, real
    fingerprint generation, list, delete, and the engine_unavailable report."""
    await pilot.browser_stop()  # deterministic slate
    try:
        report = _loads(await agent_mirage.identity_report())
        assert report.get("code") == "engine_unavailable", report

        traversal = _loads(await agent_mirage.identity_save(
            "../evil", {"navigator.userAgent": "x"},
        ))
        assert traversal.get("code") == "invalid_argument", traversal
        empty = _loads(await agent_mirage.identity_save("emptycfg", {}))
        assert empty.get("code") == "invalid_argument", empty

        created = _loads(await agent_mirage.identity_new("freshid"))
        assert created.get("saved") is True, created
        assert created.get("name") == "freshid", created
        assert created.get("summary", {}).get("navigator.userAgent"), created

        fixed = {
            "navigator.userAgent": "Mozilla/5.0 (KAHIN-TEST) Gecko/20100101 Firefox/143.0",
            "screen.width": 1280,
            "screen.height": 720,
        }
        saved = _loads(await agent_mirage.identity_save("pinned", fixed))
        assert saved.get("saved") is True, saved

        listed = _loads(await agent_mirage.identity_list())
        by_name = {i.get("name"): i for i in listed.get("identities", [])}
        assert "freshid" in by_name and "pinned" in by_name, listed
        assert by_name["pinned"]["summary"].get("navigator.userAgent") == fixed["navigator.userAgent"], listed
    finally:
        await agent_mirage.identity_delete("freshid")
        await agent_mirage.identity_delete("pinned")


@pytest.mark.asyncio
async def test_identity_start_pins_user_agent() -> None:
    """browser_start(identity=...) applies the pinned fingerprint at launch.

    Both the inline-config and the saved-name paths must pin
    navigator.userAgent, proven page-side with Runtime.evaluate (the Juggler
    set-user-agent acknowledgement is empty and proves nothing). The report
    reflects the active identity and the runtime UA.
    """
    fixed = {
        "navigator.userAgent": "Mozilla/5.0 (KAHIN-TEST) Gecko/20100101 Firefox/143.0",
        "screen.width": 1280,
        "screen.height": 720,
    }
    saved = _loads(await agent_mirage.identity_save("startpin", fixed))
    assert saved.get("saved") is True, saved
    await pilot.browser_stop()  # deterministic slate
    try:
        # 1) inline config dict
        started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", identity=dict(fixed)))
        assert started.get("status") == "started", started
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)
        ua = _eval_value(await pilot.evaluate(expression="navigator.userAgent"))
        assert ua == fixed["navigator.userAgent"], ua
        report = _loads(await agent_mirage.identity_report())
        assert report.get("active") is True, report
        assert report.get("identity", {}).get("name") is None, report
        assert report.get("navigator.userAgent") == fixed["navigator.userAgent"], report
        await pilot.browser_stop()

        # 2) saved identity name through browser_start
        started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", identity="startpin"))
        assert started.get("status") == "started", started
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)
        ua = _eval_value(await pilot.evaluate(expression="navigator.userAgent"))
        assert ua == fixed["navigator.userAgent"], ua
        report = _loads(await agent_mirage.identity_report())
        assert report.get("identity", {}).get("name") == "startpin", report
        assert report.get("navigator.userAgent") == fixed["navigator.userAgent"], report
        listed = _loads(await agent_mirage.identity_list())
        assert "startpin" in [i.get("name") for i in listed.get("identities", [])], listed
    finally:
        await pilot.browser_stop()
        await agent_mirage.identity_delete("startpin")


@pytest.mark.asyncio
async def test_agent_status_summary(mirage_tools: None) -> None:
    """agent_status reports real engine/page/DOM-stream state and never
    guesses refs: live after a snapshot, invalidated after dom_stop, and a
    structured idle response with no engine."""
    await _navigate(_doc(
        "<html><head><title>T</title></head><body><h1>Status</h1>"
        "<input id='a' placeholder='Name'></body></html>"
    ))

    idle = _loads(await agent_mirage.agent_status())
    assert idle.get("engine") == "mirage", idle
    assert idle.get("alive") is True, idle
    assert idle.get("url", "").startswith("data:text/html"), idle
    assert idle.get("title") == "T", idle
    assert idle.get("readyState") in ("complete", "interactive", "loading"), idle
    assert idle.get("tabCount", 0) >= 1, idle
    assert idle.get("currentTab"), idle
    # No snapshot has been taken: refs must never be reported as live.
    assert idle.get("refsLive") is False, idle
    assert idle.get("domCursor") is None, idle
    assert isinstance(idle.get("pendingDialogs"), int), idle
    assert isinstance(idle.get("networkEvents"), int), idle
    assert isinstance(idle.get("consoleMessages"), int), idle
    assert idle.get("identity") is None, idle

    snap = _loads(await agent_mirage.mirage_snapshot(max_tokens=800))
    assert snap.get("streamId") and snap.get("cursor") is not None, snap
    live = _loads(await agent_mirage.agent_status())
    assert live.get("refsLive") is True, live
    assert isinstance(live.get("domCursor"), int), live

    stopped = _loads(await dom_stream_mirage.mirage_dom_stop())
    assert not stopped.get("error"), stopped
    invalidated = _loads(await agent_mirage.agent_status())
    assert invalidated.get("refsLive") is False, invalidated

    # agent_status must never raise even with the engine torn down.
    await pilot.browser_stop()
    no_engine = _loads(await agent_mirage.agent_status())
    assert no_engine.get("alive") is False, no_engine
    assert no_engine.get("engine") is None, no_engine
    assert no_engine.get("tabCount") == 0, no_engine
    assert no_engine.get("refsLive") is False, no_engine
    assert no_engine.get("hint") == "use kahin_browser_start", no_engine
