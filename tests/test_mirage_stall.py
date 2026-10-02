"""Stall classification (P0-1).

A fake sidecar reproduces the locks measured on real Camoufox
(152.0.4-beta.30): while a JS dialog is open, content commands on that
session get no reply until Page.handleDialog; Browser.* keeps answering; an
awaited promise that never settles holds only its own evaluate; a busy main
thread holds every command of the session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kahin import _healer
from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.mirage import Mirage, MirageCommandTimeout

FAKE_SIDECAR = """\
#!/usr/bin/env python3
import json, sys
held = []          # request ids parked behind an open dialog
dialog_open = False
busy = False
def out(obj):
    print(json.dumps(obj), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    rid, method = req["id"], req["method"]
    params = req.get("params") or {}
    sid = req.get("sessionId")
    if method == "Browser.health":
        out({"id": rid, "result": {"alive": True, "pid": 1}}); continue
    if method.startswith("Browser."):
        out({"id": rid, "result": {"version": "fake"}}); continue
    if method == "Page.handleDialog":
        dialog_open = False
        out({"id": rid, "result": {}})
        out({"method": "Page.dialogClosed", "params": {"dialogId": "dlg-1"}, "sessionId": sid})
        for hid in held:
            out({"id": hid, "result": {"result": {"type": "number", "value": 1}}})
        held.clear(); continue
    if busy:
        continue  # main thread blocked: nothing on this session answers
    expr = params.get("expression")
    if expr == "OPEN_DIALOG":
        dialog_open = True
        held.append(rid)
        out({"method": "Page.dialogOpened",
             "params": {"dialogId": "dlg-1", "type": "alert", "message": "hello"}, "sessionId": sid})
        continue
    if dialog_open:
        held.append(rid); continue
    if expr == "BUSY":
        busy = True; continue
    if params.get("awaitPromise"):
        continue  # the awaited promise never settles; the page stays responsive
    out({"id": rid, "result": {"result": {"type": "number", "value": 0}}})
"""


@pytest.fixture
def fake_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(FAKE_SIDECAR)
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_STALL_PROBE_TIMEOUT", 0.3)
    return script


@pytest.mark.asyncio
async def test_dialog_stall_names_the_dialog(fake_sidecar: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.6)
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(MirageCommandTimeout) as caught:
            await engine.call("Runtime.evaluate", {"expression": "OPEN_DIALOG"}, session_id="s1")
        assert caught.value.cause == "pending_js_dialog"
        dialogs = caught.value.diagnosis["evidence"]["dialogs"]
        assert dialogs[0]["dialogId"] == "dlg-1" and dialogs[0]["type"] == "alert"
        assert "dlg-1" in caught.value.diagnosis["hint"]
        assert "cause=pending_js_dialog" in str(caught.value)
        assert engine.open_dialogs("s2") == []

        await engine.call("Page.handleDialog", {"dialogId": "dlg-1", "accept": True}, session_id="s1")
        await asyncio.sleep(0.1)
        assert engine.open_dialogs() == []
        reply = await engine.call("Runtime.evaluate", {"expression": "1"}, session_id="s1")
        assert reply["result"]["value"] == 0
        assert not engine._pending  # stalled requests never leak pending slots
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_awaited_promise_is_page_busy_with_responsive_probe(
    fake_sidecar: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.4)
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(MirageCommandTimeout) as caught:
            await engine.call(
                "Runtime.evaluate", {"expression": "p", "awaitPromise": True}, session_id="s1",
            )
        assert caught.value.cause == "page_busy"
        assert caught.value.diagnosis["evidence"]["detail"] == "awaited_promise_pending"
        assert caught.value.diagnosis["evidence"]["pageProbe"] == "responsive"
        assert engine._last_stall is not None and engine._last_stall[1]["cause"] == "page_busy"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_blocked_main_thread_is_page_busy_unresponsive(
    fake_sidecar: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.4)
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(MirageCommandTimeout) as caught:
            await engine.call("Runtime.evaluate", {"expression": "BUSY"}, session_id="s1")
        assert caught.value.cause == "page_busy"
        assert caught.value.diagnosis["evidence"]["detail"] == "main_thread_blocked"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_dead_transport_is_dead_pipe(fake_sidecar: Path) -> None:
    engine = Mirage()
    await engine.start()
    try:
        engine._mark_dead("test_killed")
        diagnosis = await engine.diagnose_stall("Runtime.evaluate", "s1", {}, waited_s=1.0)
        assert diagnosis["cause"] == "dead_pipe"
        assert diagnosis["evidence"]["deathReason"] == "test_killed"
    finally:
        await engine.stop()


def test_dialog_tracker_follows_open_close_and_detach() -> None:
    engine = Mirage()
    engine._track_dialog({
        "method": "Page.dialogOpened", "sessionId": "s1",
        "params": {"dialogId": "a", "type": "confirm", "message": "x" * 500},
    })
    engine._track_dialog({
        "method": "Page.javascriptDialogOpening", "sessionId": "s2",
        "params": {"dialogId": "b", "type": "prompt", "message": "y"},
    })
    assert [d["dialogId"] for d in engine.open_dialogs()] == ["a", "b"]
    assert len(engine.open_dialogs("s1")[0]["message"]) == 200
    engine._track_dialog({"method": "Page.dialogClosed", "sessionId": "s1", "params": {"dialogId": "a"}})
    engine._track_dialog({"method": "Browser.detachedFromTarget", "params": {"sessionId": "s2"}})
    assert engine.open_dialogs() == []


class _StalledEngine:
    def __init__(self, alive: bool) -> None:
        self._alive = alive
        self.stopped = False

    def is_alive(self) -> bool:
        return self._alive

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
@pytest.mark.parametrize("cause,restarts", [
    ("pending_js_dialog", False),
    ("webauthn_pending", False),
    ("page_busy", False),
    ("dead_pipe", True),
])
async def test_healer_restarts_only_on_dead_pipe(tmp_path: Path, cause: str, restarts: bool) -> None:
    """kahin.log 2026-10-02: a stall on a live page restarted the engine."""
    engine = _StalledEngine(alive=cause != "dead_pipe")
    holder = type("S", (), {"_current_engine": engine})()
    healer = _healer.Healer(log_path=tmp_path / "kahin.log")
    healer.bind_state(holder)

    with pytest.raises(RuntimeError) as caught:
        async with healer.safe("kahin_screenshot"):
            raise MirageCommandTimeout(f"Mirage: response timeout [cause={cause}]", {"cause": cause})

    assert engine.stopped is restarts
    entry = json.loads((tmp_path / "kahin.log").read_text().splitlines()[0])
    assert entry["error_code"] == "COMMAND_STALLED"
    assert entry["context"]["cause"] == cause
    if not restarts:
        assert isinstance(caught.value, MirageCommandTimeout)


@pytest.mark.asyncio
async def test_unclassified_timeout_on_live_engine_does_not_restart(tmp_path: Path) -> None:
    engine = _StalledEngine(alive=True)
    holder = type("S", (), {"_current_engine": engine})()
    healer = _healer.Healer(log_path=tmp_path / "kahin.log")
    healer.bind_state(holder)

    with pytest.raises(RuntimeError):
        async with healer.safe("kahin_mirage_wait_selector"):
            raise RuntimeError("selector timeout")

    assert engine.stopped is False
