"""Operator-discipline harness at the MCP dispatch boundary (P1-2).

Each rule is driven through a real FastMCP tool manager, the same path an
MCP client call takes, with tools whose answers are scripted.
"""

from __future__ import annotations

import json
import time

import pytest
from mcp.server.fastmcp import FastMCP

from kahin import _state as state
from kahin import harness as harness_mod


_RO = {"readOnlyHint": True}
_RW = {"readOnlyHint": False}


class Engine:
    _last_stall = None

    def is_alive(self) -> bool:
        return True

    async def health(self) -> dict:
        return {"alive": True}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    harness = harness_mod.OperatorHarness()
    monkeypatch.setattr(harness_mod, "_harness", harness)
    engine = Engine()
    monkeypatch.setattr(state, "_current_engine", engine)

    async def no_inventory(_engine, **_kwargs):
        return {"windows": [], "summary": {}, "marionette": {"status": "skipped"}}

    monkeypatch.setattr("kahin.window_inventory.collect", no_inventory)

    mcp = FastMCP(name="t")
    answers: dict[str, list[str]] = {}
    calls: list[str] = []

    def scripted(name: str, annotations: dict) -> None:
        @mcp.tool(name=name, annotations=annotations)
        async def tool(arg: str = "x") -> str:
            calls.append(name)
            queue = answers.get(name) or ['{"ok": true}']
            return queue.pop(0) if len(queue) > 1 else queue[0]

    for name in ("kahin_navigate", "kahin_mirage_eval", "kahin_vault_login"):
        scripted(name, _RW)
    for name in ("kahin_mirage_click", "kahin_mirage_mouse_click"):
        scripted(name, _RW)
    for name in ("kahin_mirage_snapshot", "kahin_agent_status"):
        scripted(name, _RO)
    for name in ("kahin_browser_start", "kahin_mirage_dialog_dismiss", "kahin_operator_reset"):
        scripted(name, _RW)
    harness_mod.install(mcp)

    async def call(name: str, arg: str = "x") -> dict:
        raw = await mcp._tool_manager.call_tool(name, {"arg": arg})
        return json.loads(raw)

    return call, answers, calls, harness, engine


STALL = json.dumps({"error": "Mirage: response timeout [cause=pending_js_dialog]",
                    "code": "command_stalled", "cause": "pending_js_dialog"})
FAIL = json.dumps({"error": "selector not found", "code": "not_found"})


@pytest.mark.asyncio
async def test_a_identical_call_refused_after_two_failures(server) -> None:
    call, answers, calls, _h, _e = server
    answers["kahin_navigate"] = [FAIL]
    assert (await call("kahin_navigate", "a"))["code"] == "not_found"
    assert (await call("kahin_navigate", "a"))["code"] == "not_found"
    refused = await call("kahin_navigate", "a")
    assert refused["code"] == "harness_repeat_refused" and refused["rule"] == "repeat"
    assert refused["failures"] == 2
    assert calls.count("kahin_navigate") == 2  # the third never reached the tool
    # Different arguments are a different path and still run.
    answers["kahin_navigate"] = ['{"ok": true}']
    assert (await call("kahin_navigate", "b")) == {"ok": True}


@pytest.mark.asyncio
async def test_b_consecutive_stalls_halt_with_state_dump(server) -> None:
    call, answers, calls, harness, _e = server
    answers["kahin_mirage_eval"] = [STALL]
    for arg in ("1", "2", "3"):
        assert (await call("kahin_mirage_eval", arg))["cause"] == "pending_js_dialog"
    halted = await call("kahin_navigate", "z")
    assert halted["code"] == "harness_halted"
    assert halted["dump"]["engine"] == {"alive": True}
    assert len(halted["dump"]["recentFailures"]) == 3
    assert "kahin_navigate" not in calls
    # Read-only observation stays available during a halt.
    assert (await call("kahin_mirage_snapshot")) == {"ok": True}
    # A successful recovery tool lifts the halt.
    assert (await call("kahin_mirage_dialog_dismiss")) == {"ok": True}
    assert (await call("kahin_navigate", "z")) == {"ok": True}
    assert harness.snapshot()["halted"] is False


@pytest.mark.asyncio
async def test_c_page_locked_path_is_blocked_after_restart(server, monkeypatch) -> None:
    call, answers, calls, _h, _e = server
    answers["kahin_mirage_eval"] = [STALL, '{"ok": true}']
    await call("kahin_mirage_eval", "same")
    # Restart: a different engine object takes over.
    monkeypatch.setattr(state, "_current_engine", Engine())
    blocked = await call("kahin_mirage_eval", "same")
    assert blocked["code"] == "harness_same_path_after_restart"
    assert blocked["cause"] == "pending_js_dialog"
    assert calls.count("kahin_mirage_eval") == 1
    # The operator reset lifts the block once the cause is handled.
    assert (await call("kahin_operator_reset", "dialog dismissed manually")) == {"ok": True}


@pytest.mark.asyncio
async def test_c_dead_pipe_path_is_not_blocked_after_restart(server, monkeypatch) -> None:
    call, answers, calls, _h, _e = server
    answers["kahin_mirage_eval"] = [
        json.dumps({"error": "gone", "code": "command_stalled", "cause": "dead_pipe"}), '{"ok": true}',
    ]
    await call("kahin_mirage_eval", "same")
    monkeypatch.setattr(state, "_current_engine", Engine())
    assert (await call("kahin_mirage_eval", "same")) == {"ok": True}


@pytest.mark.asyncio
async def test_d_blind_clicks_require_an_observation(server) -> None:
    call, _answers, calls, harness, _e = server
    for index in range(harness.blind_click_limit):
        tool = "kahin_mirage_click" if index % 2 else "kahin_mirage_mouse_click"
        assert (await call(tool, str(index))) == {"ok": True}
    refused = await call("kahin_mirage_click", "next")
    assert refused["code"] == "harness_observation_required"
    assert (await call("kahin_mirage_snapshot")) == {"ok": True}
    assert (await call("kahin_mirage_click", "next")) == {"ok": True}
    assert calls.count("kahin_mirage_click") + calls.count("kahin_mirage_mouse_click") == harness.blind_click_limit + 1


@pytest.mark.asyncio
async def test_flattened_error_gets_the_engine_stall_diagnosis(server) -> None:
    _call, _answers, _calls, _h, engine = server
    diagnosis = {"cause": "webauthn_pending", "hint": "finish it"}

    mcp = FastMCP(name="flatten")

    @mcp.tool(name="kahin_screenshot", annotations=_RO)
    async def flatten() -> str:
        # The tool layer turned the classified stall into a generic error.
        engine._last_stall = (time.monotonic(), diagnosis)
        return json.dumps({"error": "Screenshot failed: timeout", "code": "tool_failed"})

    harness_mod.install(mcp)
    payload = json.loads(await mcp._tool_manager.call_tool("kahin_screenshot", {}))

    assert payload["cause"] == "webauthn_pending"
    assert payload["diagnosis"] == diagnosis
    assert payload["code"] == "tool_failed"


def test_disabled_harness_never_refuses(monkeypatch) -> None:
    monkeypatch.setenv("KAHIN_HARNESS", "0")
    harness = harness_mod.OperatorHarness()
    harness._failures["kahin_navigate:{}"] = {"count": 99, "last": None}
    assert harness.precheck("kahin_navigate", {}, read_only=False) is None
