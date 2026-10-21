"""Tests for subagent identity extraction, tab isolation, and browser lock in keş mode."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kahin import _state as state
from kahin.subagent import (
    extract_caller_identity,
    get_active_subagent_context,
    reset_active_subagent_context,
    set_active_subagent_context,
)
from kahin.the_twins.mirage import Mirage


def test_extract_caller_identity_explicit_args() -> None:
    args = {"agent_id": "worker-42", "target_id": "target-99"}
    caller, target = extract_caller_identity(args, None)
    assert caller == "worker-42"
    assert target == "target-99"


def test_extract_caller_identity_subagent_id() -> None:
    args = {"subagent_id": "sub-1", "tab_id": "tab-1"}
    caller, target = extract_caller_identity(args, None)
    assert caller == "sub-1"
    assert target == "tab-1"


def test_extract_caller_identity_context_meta() -> None:
    context = MagicMock()
    req_ctx = MagicMock()
    req_ctx.meta = {"agent_id": "agent-xyz", "claudecode/toolUseId": "tooluse-abc123"}
    context.request_context = req_ctx

    caller, target = extract_caller_identity({}, context)
    assert caller == "agent-xyz"
    assert target is None

    # Per-tool invocation IDs (like claudecode/toolUseId) must NOT be used as caller identity
    req_ctx.meta = {"claudecode/toolUseId": "tooluse-abc123"}
    req_ctx.session = None
    context.session_id = None
    context.client_id = None
    caller_tooluse, _ = extract_caller_identity({}, context)
    assert caller_tooluse is None

    # FastMCP direct context.session_id is detected
    context_sse = MagicMock()
    context_sse.request_context = None
    context_sse.session_id = "sse-client-session-789"
    caller_sse, _ = extract_caller_identity({}, context_sse)
    assert caller_sse == "sse-client-session-789"


def test_extract_caller_identity_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_AGENT", "code-agent-x")
    caller, target = extract_caller_identity({}, None)
    assert caller == "code-agent-x"
    assert target is None


def test_context_var_task_isolation() -> None:
    async def worker(agent_id: str, target_id: str) -> dict[str, str]:
        token = set_active_subagent_context(caller_id=agent_id, target_id=target_id)
        try:
            await asyncio.sleep(0.01)
            ctx = get_active_subagent_context()
            return {"caller": ctx.get("caller_id"), "target": ctx.get("target_id")}
        finally:
            reset_active_subagent_context(token)

    async def main() -> None:
        r1, r2, r3, r4 = await asyncio.gather(
            worker("agent-1", "target-1"),
            worker("agent-2", "target-2"),
            worker("agent-3", "target-3"),
            worker("agent-4", "target-4"),
        )
        assert r1 == {"caller": "agent-1", "target": "target-1"}
        assert r2 == {"caller": "agent-2", "target": "target-2"}
        assert r3 == {"caller": "agent-3", "target": "target-3"}
        assert r4 == {"caller": "agent-4", "target": "target-4"}

    asyncio.run(main())


def test_browser_lock_in_kes_mode() -> None:
    # In keş mode, acquire_browser_lock returns None and does not claim exclusive slot
    res = state.acquire_browser_lock(mode="keş")
    assert res is None


@pytest.mark.asyncio
async def test_mirage_subagent_tab_isolation() -> None:
    """Four concurrent subagents each receive dedicated tabs without colliding."""
    mirage = Mirage()
    mirage._dead = False
    mirage._process = MagicMock()
    mirage._process.stdin = MagicMock()

    # Mock call to Browser.createBrowserContext and create_page
    ctx_counter = 0
    page_counter = 0

    async def mock_call(method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        nonlocal ctx_counter
        if method == "Browser.createBrowserContext":
            ctx_counter += 1
            return {"browserContextId": f"ctx-{ctx_counter}"}
        if method == "Page.close":
            for tid, sid in list(mirage._sessions.items()):
                if sid == session_id:
                    mirage._sessions.pop(tid, None)
            return {}
        if method == "Browser.removeBrowserContext":
            return {}
        if method == "Browser.setUserAgentOverride":
            return {}
        return {}

    async def mock_create_page(url: str = "about:blank", browser_context_id: str | None = None) -> dict:
        nonlocal page_counter
        page_counter += 1
        tid = f"target-{page_counter}"
        sid = f"session-{page_counter}"
        mirage._sessions[tid] = sid
        mirage._target_infos[tid] = {
            "targetId": tid,
            "browserContextId": browser_context_id,
            "url": url,
        }
        return {"targetId": tid, "sessionId": sid}

    mirage.call = mock_call  # type: ignore[assignment]
    mirage.create_page = mock_create_page  # type: ignore[assignment]

    # Subagent 1 calls ensure_page
    page1 = await mirage.ensure_page(caller_id="subagent-1")
    assert page1["targetId"] == "target-1"
    assert page1["sessionId"] == "session-1"

    # Subagent 2 calls ensure_page concurrently
    page2 = await mirage.ensure_page(caller_id="subagent-2")
    assert page2["targetId"] == "target-2"
    assert page2["sessionId"] == "session-2"

    # Subagent 1 calls ensure_page again without target_id: gets tab 1!
    page1_again = await mirage.ensure_page(caller_id="subagent-1")
    assert page1_again["targetId"] == "target-1"

    # Subagent 2 calls ensure_page again without target_id: gets tab 2!
    page2_again = await mirage.ensure_page(caller_id="subagent-2")
    assert page2_again["targetId"] == "target-2"

    # Subagent 3 and 4
    page3 = await mirage.ensure_page(caller_id="subagent-3")
    page4 = await mirage.ensure_page(caller_id="subagent-4")
    assert page3["targetId"] == "target-3"
    assert page4["targetId"] == "target-4"

    # Check session routing
    assert mirage.current_session_id(caller_id="subagent-1") == "session-1"
    assert mirage.current_session_id(caller_id="subagent-2") == "session-2"
    assert mirage.current_session_id(caller_id="subagent-3") == "session-3"
    assert mirage.current_session_id(caller_id="subagent-4") == "session-4"

    # Explicit target_id overrides
    page_explicit = await mirage.ensure_page(caller_id="subagent-1", target_id="target-3")
    assert page_explicit["targetId"] == "target-3"

    # Closing subagent-2's tab disposes its browser context
    closed = await mirage.close_page("target-2")
    assert closed == {"closed": "target-2"}
    assert "subagent-2" not in mirage._agent_targets
    assert "subagent-2" not in mirage._agent_contexts


@pytest.mark.asyncio
async def test_mirage_user_agent_runtime_reflection() -> None:
    """Setting userAgent via Emulation or Network overrides updates tracking and live page."""
    mirage = Mirage()
    mirage._dead = False
    mirage._process = MagicMock()
    mirage._process.stdin = MagicMock()

    calls: list[tuple[str, dict]] = []

    async def mock_call(method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        calls.append((method, params or {}))
        return {}

    mirage.call = mock_call  # type: ignore[assignment]
    mirage._sessions["t1"] = "s1"
    mirage._current_target = "t1"

    res = await mirage.execute_cdp("Network", "setUserAgentOverride", {"userAgent": "CustomUA/1.0"}, session_id="s1")
    assert res == {}

    # Verified Browser.setUserAgentOverride called and Runtime.evaluate evaluated
    called_methods = [c[0] for c in calls]
    assert "Browser.setUserAgentOverride" in called_methods
    assert "Runtime.evaluate" in called_methods
    assert mirage._user_agent_overrides.get("default") == "CustomUA/1.0"

    # Resetting user agent deletes prototype property
    calls.clear()
    res_reset = await mirage.execute_cdp("Network", "setUserAgentOverride", {"userAgent": ""}, session_id="s1")
    assert res_reset == {}
    assert "default" not in mirage._user_agent_overrides
    assert any("delete Navigator.prototype.userAgent" in str(c[1]) for c in calls if c[0] == "Runtime.evaluate")


@pytest.mark.asyncio
async def test_mirage_zombie_context_auto_reap() -> None:
    """True zombies (detached targets or dead processes) are reaped, while in-flight agents are protected."""
    mirage = Mirage()
    mirage._dead = False
    mirage._sessions = {"initial-target": "sess-init"}
    mirage._target_infos = {"initial-target": {"targetId": "initial-target", "type": "page"}}
    mirage._current_target = "initial-target"

    closed_pages: list[str] = []
    removed_contexts: list[str] = []

    async def mock_call(method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        p = params or {}
        if method == "Browser.createBrowserContext":
            return {"browserContextId": f"ctx-{len(removed_contexts) + len(mirage._agent_contexts) + 1}"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1"}}}
        if method in ("Browser.newPage", "Target.createTarget"):
            tid = f"target-{len(mirage._sessions) + 1}"
            sid = f"session-{len(mirage._sessions) + 1}"
            mirage._sessions[tid] = sid
            mirage._target_infos[tid] = {"targetId": tid, "type": "page"}
            return {"targetId": tid}
        if method == "Page.close":
            closed_pages.append(session_id or "")
            return {}
        if method == "Browser.removeBrowserContext":
            removed_contexts.append(p.get("browserContextId", ""))
            return {}
        return {}

    mirage.call = mock_call  # type: ignore[assignment]

    # 1. Unconstrained N-parallelism: spawn 20 callers, NONE are evicted because all are alive
    for i in range(20):
        await mirage.ensure_page(caller_id=f"agent-{i}")
    assert len(mirage._agent_targets) == 20
    assert len(removed_contexts) == 0

    # 2. Detached tab detection: agent-5's target crashes / detaches from self._sessions
    target_5 = mirage._agent_targets["agent-5"]
    mirage._sessions.pop(target_5, None)

    # 3. In-flight protection: mark agent-10 as currently executing a long-running call
    mirage._agent_in_flight["agent-10"] = 1
    # artificially set its last_seen to 2 hours ago
    mirage._agent_last_seen["agent-10"] = 0.0

    # Prune
    await mirage._prune_zombie_contexts()

    # Verified: detached agent-5 is reaped
    assert "agent-5" not in mirage._agent_targets
    assert len(removed_contexts) == 1

    # Verified: in-flight agent-10 is NEVER reaped despite 2 hours old timestamp
    assert "agent-10" in mirage._agent_targets
