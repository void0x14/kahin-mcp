"""Tool-layer end-to-end tests over real Camoufox (Faz 9 Task 4).

Drives the MCP TOOL LAYER — the async functions registered as the
kahin_* / kahin_mirage_* tools in ``kahin/tools/*.py`` — against a real
Camoufox browser through the Zig sidecar. Unlike test_mirage_ipc.py (which
talks to the raw Mirage engine), every scenario here goes through the same
functions the MCP server exposes, so the tool contract (JSON-string
answers, error strings instead of raises, engine liveness eviction) is
what gets verified.

SIDECAR FIX (ipc_main.zig respondFromRaw): no-return Juggler commands
(Browser.setCookies / clearCookies / setUserAgentOverride /
setDefaultViewport / Browser.clearCache / Page.close / Page.insertText /
Page.dispatchMouseEvent / Page.handleDialog) reply with ``{"id":N}`` and
NO ``result`` key. The sidecar now treats a reply that has NEITHER
``error`` NOR ``result`` as success with an empty result (Playwright does
the same), so the scenarios below run their full assertions — no skips.

Other verified build fact (skip reason below):
- localStorage is blocked on data: URLs ("The operation is insecure.") —
  the storage scenario therefore uses a file:// page (still network-free).

Real-HTTP network coverage (requests list, response body, interception
continue/abort) lives in test_e2e_network.py — data: URLs emit no
Network.* events (a data:-URL artifact, not a tool bug), so network
scenarios need a server-backed URL.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin import _state as state
from kahin.the_twins import mirage as mirage_mod
from kahin.tools import dialog_mirage, dom_stream_mirage, emulation_mirage, engine, pilot
from kahin.tools import pilot_mirage, storage_mirage, trainman_mirage


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


def _doc(body: str) -> str:
    """A data: URL carrying an HTML document (no network involved)."""
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    """Start real Camoufox through kahin_browser_start with its one tab, then stop."""
    resp = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tabs = _loads(await trainman_mirage.mirage_tab_list())
        assert len(tabs) == 1 and tabs[0].get("targetId"), tabs
        await asyncio.sleep(0.5)  # session/frame events settle (mirrors test_mirage_ipc)
        yield
    finally:
        await pilot.browser_stop()


async def _navigate(url: str) -> None:
    resp = _loads(await pilot.navigate(url=url))
    assert isinstance(resp, dict) and resp.get("frameId"), resp


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dom_flow_type_click_read(mirage_tools: None) -> None:
    """query -> type -> click -> get_text sees the post-click DOM.

    query/get_text run on the real evaluate surface; type (Page.insertText)
    and click (Page.dispatchMouseEvent) are no-return commands the sidecar
    now answers with an empty result.
    """
    html = """<html><body>
      <button id="go">Go</button>
      <input id="inp">
      <div id="out">initial</div>
      <script>
        document.getElementById('go').addEventListener('click', () => {
          const v = document.getElementById('inp').value;
          document.getElementById('out').textContent = v || 'clicked';
        });
      </script>
    </body></html>"""
    await _navigate(_doc(html))
    await asyncio.sleep(0.5)  # document parsed, listeners attached

    info = _loads(await pilot_mirage.mirage_query("#go"))
    assert info["tag"] == "button", info
    assert info["visible"] is True, info

    typed = await pilot_mirage.mirage_type("#inp", "hello")
    assert _loads(typed)["typed"] == 5, typed

    clicked = await pilot_mirage.mirage_click("#go")
    assert _loads(clicked)["clicked"] == "#go", clicked

    text = _loads(await pilot_mirage.mirage_get_text("#out"))
    assert text == "hello", text


@pytest.mark.asyncio
async def test_multi_tab_lifecycle(mirage_tools: None) -> None:
    """tab_new x2 -> tab_list == 2 -> switch -> close -> tab_list == 1.

    new/list/switch/close are all real Juggler calls.
    """
    first = _loads(await trainman_mirage.mirage_tab_list())
    assert len(first) == 1, first

    tab2 = _loads(await trainman_mirage.mirage_tab_new(url="about:blank"))
    assert tab2.get("targetId"), tab2

    tabs = _loads(await trainman_mirage.mirage_tab_list())
    assert len(tabs) == 2, tabs

    target1 = first[0]["targetId"]
    switched = _loads(await trainman_mirage.mirage_tab_switch(target1))
    assert switched.get("switched") == target1, switched
    tabs = _loads(await trainman_mirage.mirage_tab_list())
    assert next(t for t in tabs if t["targetId"] == target1)["current"] is True

    closed = await trainman_mirage.mirage_tab_close(tab2["targetId"])
    assert _loads(closed).get("closed") == tab2["targetId"], closed
    await asyncio.sleep(0.3)  # detachedFromTarget event lands

    tabs = _loads(await trainman_mirage.mirage_tab_list())
    assert len(tabs) == 1, tabs
    assert tabs[0]["targetId"] == target1, tabs


@pytest.mark.asyncio
async def test_concurrent_tab_session_ownership(mirage_tools: None) -> None:
    """Explicit page sessions stay bound while the current tab switches.

    The two Runtime.evaluate calls deliberately overlap.  The active tab is
    switched while the first call is waiting in the real browser; each call
    carries its owning Juggler session explicitly and must still read its own
    document.  This is a deterministic engine-level regression guard for the
    ownership primitive used by session-aware callers.
    """
    engine = state._current_engine
    assert isinstance(engine, mirage_mod.Mirage), engine

    first = _loads(await trainman_mirage.mirage_tab_list())
    first_target = next(tab for tab in first if tab.get("current"))
    second = _loads(await trainman_mirage.mirage_tab_new(url="about:blank"))
    await asyncio.sleep(0.3)

    for tab, label in ((first_target, "first-owner"), (second, "second-owner")):
        await engine.call(
            "Page.navigate",
            {"url": _doc(f"<html><body>{label}</body></html>")},
            session_id=tab["sessionId"],
        )
    await asyncio.sleep(0.3)

    first_read = asyncio.create_task(engine.call(
        "Runtime.evaluate",
        {
            "expression": "new Promise(resolve => setTimeout(() => resolve(document.body.textContent), 150))",
            "awaitPromise": True,
            "returnByValue": True,
        },
        session_id=first_target["sessionId"],
    ))
    await asyncio.sleep(0.03)
    switched = _loads(await trainman_mirage.mirage_tab_switch(second["targetId"]))
    assert switched.get("switched") == second["targetId"], switched
    second_result = await engine.call(
        "Runtime.evaluate",
        {"expression": "document.body.textContent", "returnByValue": True},
        session_id=second["sessionId"],
    )
    first_result = await first_read

    assert first_result["result"]["value"] == "first-owner", first_result
    assert second_result["result"]["value"] == "second-owner", second_result


@pytest.mark.asyncio
async def test_cookie_round_trip(mirage_tools: None) -> None:
    """set -> get matches -> clear -> get empty.

    set/clear are no-return Juggler commands (empty-result replies); the
    getCookies assertions prove they actually took effect.
    """
    cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    assert cookies == [], cookies

    result = await storage_mirage.mirage_cookie_set(cookies=[
        {"name": "kahin_e2e", "value": "cookie-value", "url": "http://example.com/"},
    ])
    assert _loads(result) == {}, result

    cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    match = [c for c in cookies if c.get("name") == "kahin_e2e"]
    assert len(match) == 1, cookies
    assert match[0]["value"] == "cookie-value", match

    cleared = await storage_mirage.mirage_cookie_clear()
    assert _loads(cleared) == {}, cleared

    cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    assert all(c.get("name") != "kahin_e2e" for c in cookies), cookies


@pytest.mark.asyncio
async def test_browser_context_isolation_and_cleanup(mirage_tools: None) -> None:
    """Context-scoped cookies/UA do not leak to the default context, and
    the public context-close tool removes its tabs instead of leaving a
    browser resource behind."""
    tabs_before = _loads(await trainman_mirage.mirage_tab_list())
    default_target = next(tab["targetId"] for tab in tabs_before if tab.get("current"))

    created = _loads(await trainman_mirage.mirage_context_new())
    context_id = created.get("browserContextId")
    assert isinstance(context_id, str) and context_id, created
    context_tab = _loads(await trainman_mirage.mirage_tab_new(browser_context_id=context_id))
    assert context_tab.get("targetId"), context_tab
    await asyncio.sleep(0.4)

    set_cookie = await storage_mirage.mirage_cookie_set([{
        "name": "kahin_context_cookie",
        "value": "isolated",
        "url": "http://example.com/",
    }])
    assert _loads(set_cookie) == {}, set_cookie
    context_cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    assert any(cookie.get("name") == "kahin_context_cookie" for cookie in context_cookies), context_cookies

    ua = "KahinContext/1.0"
    assert _loads(await emulation_mirage.mirage_set_user_agent(ua)) == {}, "context UA override failed"
    await _navigate(_doc("<html><body>context</body></html>"))
    context_ua = _loads(await pilot.evaluate(expression="navigator.userAgent"))
    assert context_ua["result"]["value"] == ua, context_ua

    switched = _loads(await trainman_mirage.mirage_tab_switch(default_target))
    assert switched.get("switched") == default_target, switched
    default_cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    assert all(cookie.get("name") != "kahin_context_cookie" for cookie in default_cookies), default_cookies

    closed = _loads(await trainman_mirage.mirage_context_close(context_id))
    assert closed == {}, closed
    await asyncio.sleep(0.4)
    tabs_after = _loads(await trainman_mirage.mirage_tab_list())
    assert all(tab.get("targetId") != context_tab["targetId"] for tab in tabs_after), tabs_after


@pytest.mark.asyncio
async def test_dialog_accept(mirage_tools: None) -> None:
    """alert() -> dialog_list shows it -> accept resolves it (no hang).

    dialog_list (event buffer) is real and verified; accept
    (Page.handleDialog) is a no-return command the sidecar answers with an
    empty result.
    """
    html = """<html><body><script>
      setTimeout(() => alert('kahin-dialog'), 200);
    </script><div>page</div></body></html>"""
    await _navigate(_doc(html))
    await asyncio.sleep(1.2)  # dialogOpened event arrives while idle

    dialogs = _loads(await dialog_mirage.mirage_dialog_list())
    assert dialogs, dialogs  # Page.dialogOpened IS emitted by this build
    assert dialogs[0]["message"] == "kahin-dialog", dialogs
    dialog_id = dialogs[0]["dialogId"]

    accepted = await dialog_mirage.mirage_dialog_accept(dialog_id)
    assert _loads(accepted) == {}, accepted
    await asyncio.sleep(0.5)  # dialogClosed event lands

    assert _loads(await dialog_mirage.mirage_dialog_list()) == []


@pytest.mark.asyncio
async def test_open_dialog_fails_fast_with_cause_and_is_inventoried(mirage_tools: None) -> None:
    """P0-1/P2-2/P0-2 on real Camoufox: a JS dialog parks the page.

    Measured on 152.0.4-beta.30: Runtime.evaluate gets no reply while an
    alert is open (it used to wait the full 30 s request timeout, and the
    healer then restarted the engine). Now the command fails within the
    dialog grace window with ``cause=pending_js_dialog`` and the dialogId,
    and kahin_agent_status lists the dialog on its tab without stalling.
    """
    from kahin.tools import agent_mirage

    await _navigate(_doc("<html><body><div>page</div></body></html>"))
    started = asyncio.get_running_loop().time()
    blocked = _loads(await pilot_mirage.mirage_eval("alert('kahin-lock'); 1"))
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 10.0, elapsed
    assert blocked["cause"] == "pending_js_dialog", blocked
    dialog_id = blocked["diagnosis"]["evidence"]["dialogs"][0]["dialogId"]
    assert dialog_id in blocked["hint"], blocked

    started = asyncio.get_running_loop().time()
    status = _loads(await agent_mirage.agent_status())
    assert asyncio.get_running_loop().time() - started < 5.0
    assert status["alive"] is True and status["pendingDialogs"] == 1, status
    current = [w for w in status["windows"] if w.get("current")]
    assert current and current[0]["dialogs"][0]["dialogId"] == dialog_id, status["windows"]
    assert status["windowSummary"]["jsDialogs"] == 1, status["windowSummary"]

    assert _loads(await dialog_mirage.mirage_dialog_dismiss(dialog_id)) == {}
    await asyncio.sleep(0.3)
    assert _loads(await pilot_mirage.mirage_eval("1 + 1")) == 2


@pytest.mark.asyncio
async def test_kill_detection_clean_tool_error(mirage_tools: None) -> None:
    """Firefox death -> clean tool errors, then explicit stop still reaps."""
    eng = state._current_engine
    assert eng is not None and eng.is_alive() is True
    before = _loads(await engine.engine_health())
    browser_pid = before["health"]["pid"]
    os.kill(browser_pid, signal.SIGKILL)
    await asyncio.sleep(0.8)  # reader hits EOF -> on_death retains it for stop

    assert eng.is_alive() is False

    health = _loads(await engine.engine_health())
    assert health["engine"] == "mirage", health
    assert health["alive"] is False, health

    out = await pilot_mirage.mirage_query("#anything")
    assert "Browser engine is dead" in out, out  # clean error string, no crash

    stopped = await pilot.browser_stop()
    assert stopped == '{"status": "stopped"}'
    assert state._current_engine is None


@pytest.mark.asyncio
async def test_local_storage_round_trip(mirage_tools: None, tmp_path: Any) -> None:
    """Page JS writes localStorage -> storage_local_get reads it back.

    localStorage is blocked on data: URLs ("The operation is insecure."),
    so the page is served from a file:// URL — still network-free.
    """
    page = tmp_path / "ls_page.html"
    page.write_text("""<html><body><script>
      localStorage.setItem('kahin_ls', 'ls-value');
    </script></body></html>""")
    await _navigate(page.as_uri())
    await asyncio.sleep(0.5)

    entries = _loads(await storage_mirage.mirage_storage_local_get())
    match = [e for e in entries if e.get("key") == "kahin_ls"]
    assert len(match) == 1, entries
    assert match[0]["value"] == "ls-value", match


@pytest.mark.asyncio
async def test_emulation_ua_round_trip(mirage_tools: None) -> None:
    """set_user_agent -> fresh document -> navigator.userAgent matches.

    Browser.setUserAgentOverride is a no-return command (sidecar fix).
    """
    ua = "KahinE2E/9.9"
    result = await emulation_mirage.mirage_set_user_agent(ua)
    assert _loads(result) == {}, result

    await _navigate(_doc("<html><body>ua</body></html>"))
    await asyncio.sleep(0.5)

    got = _loads(await pilot.evaluate(expression="navigator.userAgent"))
    assert got["result"]["value"] == ua, got


@pytest.mark.asyncio
async def test_engine_health_live(mirage_tools: None) -> None:
    """engine_health reports a live Mirage with the sidecar payload."""
    health = _loads(await engine.engine_health())
    assert health["engine"] == "mirage", health
    assert health["alive"] is True, health
    assert health["health"]["alive"] is True, health


@pytest.mark.asyncio
async def test_dom_stream_snapshot_delta_and_live_action(mirage_tools: None) -> None:
    """MutationObserver deltas and node actions stay tied to live DOM nodes."""
    await _navigate(_doc("""<html><body>
      <input id="name" placeholder="Name">
      <div id="out">initial</div>
      <script>setTimeout(() => {
        document.querySelector('#out').textContent = 'loaded';
        const button = document.createElement('button');
        button.id = 'later'; button.textContent = 'Later';
        button.onclick = () => { window.kahinClicked = true; };
        document.body.append(button);
      }, 120);</script>
    </body></html>"""))
    started = _loads(await dom_stream_mirage.mirage_dom_start(max_events=128))
    snap = _loads(await dom_stream_mirage.mirage_dom_snapshot(max_nodes=100, include_hidden=True))

    def find(node: dict[str, Any], tag: str) -> dict[str, Any] | None:
        if node.get("tag") == tag:
            return node
        for child in node.get("children") or []:
            found = find(child, tag)
            if found:
                return found
        return None

    input_node = find(snap["root"], "input")
    assert input_node and input_node["actions"] == ["focus", "type"], snap
    await asyncio.sleep(0.4)
    events = _loads(await dom_stream_mirage.mirage_dom_events(
        after_seq=snap["cursor"], stream_id=started["stream"]["streamId"], wait_ms=1000, limit=20,
    ))
    assert events["reset"] is False, events
    assert events["events"], events
    assert any(event["type"] == "childList" for event in events["events"]), events

    acted = _loads(await dom_stream_mirage.mirage_dom_action(input_node["nodeId"], "type", text="Ada"))
    assert acted["target"]["nodeId"] == input_node["nodeId"], acted
    value = _loads(await pilot.evaluate(expression="document.querySelector('#name').value"))
    assert value["result"]["value"] == "Ada", value
    input_events = _loads(await dom_stream_mirage.mirage_dom_events(
        after_seq=events["cursor"],
        stream_id=started["stream"]["streamId"],
        wait_ms=1000,
        limit=20,
    ))
    assert any(
        event.get("type") == "event" and event.get("event") == "input"
        for event in input_events["events"]
    ), input_events

    later = _loads(await dom_stream_mirage.mirage_dom_snapshot(selector="#later"))
    clicked = _loads(await dom_stream_mirage.mirage_dom_action(later["root"]["nodeId"], "click"))
    assert clicked["target"]["action"] == "click", clicked
    result = _loads(await pilot.evaluate(expression="window.kahinClicked === true"))
    assert result["result"]["value"] is True, result


@pytest.mark.asyncio
async def test_dom_stream_cursor_resets_after_navigation(mirage_tools: None) -> None:
    """A document navigation invalidates old node/cursor truth explicitly."""
    await _navigate(_doc("<html><body><p>first</p></body></html>"))
    started = _loads(await dom_stream_mirage.mirage_dom_start())
    snap = _loads(await dom_stream_mirage.mirage_dom_snapshot(max_nodes=50, include_hidden=True))
    await _navigate(_doc("<html><body><p>second</p></body></html>"))
    events = _loads(await dom_stream_mirage.mirage_dom_events(
        after_seq=snap["cursor"], stream_id=started["stream"]["streamId"], wait_ms=500,
    ))
    assert events["reset"] is True, events
    assert events["dropped"] is True, events
    assert events["streamId"] != started["stream"]["streamId"], events


@pytest.mark.asyncio
async def test_dom_action_contract_validation_and_select(mirage_tools: None) -> None:
    """text is validated before any page side effect; unsupported_action and select errors surface."""
    await _navigate(_doc("""<html><body>
      <input id="name" placeholder="Name">
      <select id="pick"><option value="a">Alpha</option><option value="b">Beta</option></select>
    </body></html>"""))
    await dom_stream_mirage.mirage_dom_start(max_events=128)
    snap = _loads(await dom_stream_mirage.mirage_dom_snapshot(max_nodes=100, include_hidden=True))

    def find(node: dict[str, Any], tag: str) -> dict[str, Any] | None:
        if node.get("tag") == tag:
            return node
        for child in node.get("children") or []:
            found = find(child, tag)
            if found:
                return found
        return None

    input_node = find(snap["root"], "input")
    select_node = find(snap["root"], "select")
    assert input_node and input_node["actions"] == ["focus", "type"], snap
    assert select_node and select_node["actions"] == ["select"], snap

    rejected = _loads(await dom_stream_mirage.mirage_dom_action(input_node["nodeId"], "type"))
    assert rejected["code"] == "invalid_argument" and rejected["field"] == "text", rejected
    # Validation precedes dispatch: the rejected call must not have focused the input.
    active = _loads(await pilot.evaluate(expression="document.activeElement === document.querySelector('#name')"))
    assert active["result"]["value"] is False, active

    unsupported = _loads(await dom_stream_mirage.mirage_dom_action(input_node["nodeId"], "dblclick"))
    assert unsupported["error"] == "unsupported_action", unsupported
    assert unsupported["allowed"] == ["click", "hover", "focus", "type", "scroll", "select"], unsupported

    not_select = _loads(await dom_stream_mirage.mirage_dom_action(input_node["nodeId"], "select", text="x"))
    assert not_select["error"] == "not_select", not_select

    no_option = _loads(await dom_stream_mirage.mirage_dom_action(select_node["nodeId"], "select", text="nope"))
    assert no_option["error"] == "option_not_found", no_option

    selected = _loads(await dom_stream_mirage.mirage_dom_action(select_node["nodeId"], "select", text="Beta"))
    assert selected["target"]["nodeId"] == select_node["nodeId"], selected
    value = _loads(await pilot.evaluate(expression="document.querySelector('#pick').value"))
    assert value["result"]["value"] == "b", value
