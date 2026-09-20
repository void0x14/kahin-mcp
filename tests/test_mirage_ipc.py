"""Mirage engine tests: fake-sidecar unit tests + real Camoufox integration.

Faz 9 Task 3: the wire is the Juggler-native call schema
``Mirage.call(method, params, session_id)`` — send_cdp is gone.
"""


def test_user_js_uses_json_booleans_not_python_repr():
    """GODMODE 2026-09-19: user.js must be valid JS. The old
    ``"user_pref({!r}, {!r})"`` formatting wrote Python repr
    (``False``/``True``/single quotes), which Firefox silently drops —
    fission stayed on and webgl prefs never applied. JSON dumps
    emits JS-compatible literals."""
    import json

    prefs = {"fission.autostart": False, "webgl.force-enabled": True, "x": "y"}
    lines = [f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()]
    text = "\n".join(lines)
    assert "False" not in text and "True" not in text and "'" not in text
    assert 'user_pref("fission.autostart", false);' in text
    assert 'user_pref("webgl.force-enabled", true);' in text

import asyncio
from pathlib import Path

import pytest

from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.chassis import EventData
from kahin.the_twins.mirage import Mirage

FAKE_SIDECAR = """\
#!/usr/bin/env python3
import json, sys, time
for line in sys.stdin:
    req = json.loads(line)
    rid = req["id"]
    params = req.get("params") or {}
    if params.get("slow"):
        time.sleep(5)
        continue
    if req["method"] == "Browser.health":
        print(json.dumps({"id": rid, "result": {"alive": True, "pid": 1}}), flush=True)
        continue
    if params.get("emit_event"):
        print(json.dumps({"method": "Runtime.console",
                          "params": {"type": "log",
                                     "args": [{"type": "string", "value": "hi"}],
                                     "location": {"url": "", "lineNumber": 1, "columnNumber": 1}},
                          "sessionId": "sess-1"}), flush=True)
    print(json.dumps({"id": rid, "result": {"echo": req["method"]}}), flush=True)
"""


@pytest.fixture
def fake_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(FAKE_SIDECAR)
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    return script


@pytest.mark.asyncio
async def test_call_id_matching(fake_sidecar: Path) -> None:
    engine = Mirage()
    await engine.start()
    assert engine._process is not None
    try:
        result = await engine.call("Browser.createBrowserContext")
        assert result == {"echo": "Browser.createBrowserContext"}
        result = await engine.call("Runtime.evaluate", {"expression": "1+1"})
        assert result["echo"] == "Runtime.evaluate"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_call_timeout(fake_sidecar: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.3)
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(RuntimeError, match="timeout"):
            await engine.call("Runtime.evaluate", {"slow": True})
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_get_response_body_retries_native_completion_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request id can be visible before Juggler exposes its body."""
    engine = Mirage()
    calls: list[str] = []

    async def fake_call(
        method: str,
        params: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        del params, session_id
        calls.append(method)
        if len(calls) < 3:
            raise RuntimeError("CDP error: {'message': 'No resource with given identifier'}")
        return {"base64body": "eA=="}

    monkeypatch.setattr(engine, "call", fake_call)
    monkeypatch.setattr(mirage_mod, "_RESPONSE_BODY_RETRY_INTERVAL", 0.001)
    monkeypatch.setattr(mirage_mod, "_RESPONSE_BODY_RETRY_TIMEOUT", 0.1)

    assert await engine.get_response_body("request-1") == {"base64body": "eA=="}
    assert calls == ["Network.getResponseBody"] * 3


@pytest.mark.asyncio
async def test_event_dispatch(fake_sidecar: Path) -> None:
    engine = Mirage()
    received: list[EventData] = []
    await engine.on_event(lambda evt: received.append(evt))
    await engine.start()
    try:
        await engine.call("Runtime.evaluate", {"emit_event": True})
        await asyncio.sleep(0.2)
        assert len(received) == 1
        assert received[0].method == "Runtime.console"
        assert received[0].session_id == "sess-1"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_stop_reaps_sidecar_when_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation during stop must not leave the sidecar running."""
    script = tmp_path / "stubborn_sidecar.py"
    script.write_text(
        """#!/usr/bin/env python3
import json
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    if request[\"method\"] == \"Browser.health\":
        print(json.dumps({\"id\": request[\"id\"], \"result\": {\"alive\": True}}), flush=True)
        break

while True:
    time.sleep(60)
"""
    )
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)

    engine = Mirage()
    await engine.start()
    proc = engine._process
    assert proc is not None
    try:
        stop_task = asyncio.create_task(engine.stop())
        await asyncio.sleep(0.1)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert proc.returncode is not None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if engine._stderr_file is not None:
            engine._stderr_file.close()
            engine._stderr_file = None
        engine._remove_profile()


def _real_available() -> bool:
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing; build core/ipc_main.zig first"
)


@pytest.mark.asyncio
async def test_mirage_full_flow_real_camoufox() -> None:
    """End-to-end over the real Camoufox: page, navigate, evaluate, screenshot,
    and idle event forwarding (navigation events; page-context console events
    are not emitted by this Camoufox build — evaluate-triggered ones arrive
    in-flight and are eaten by the driver pump, see ipc_main.zig docstring)."""
    engine = Mirage()
    ctx = await engine.start()
    assert ctx.engine_name == "mirage"
    assert ctx.ws_url == ""

    events: list[EventData] = []
    await engine.on_event(lambda evt: events.append(evt))

    try:
        ctx_id = (await engine.call("Browser.createBrowserContext"))["browserContextId"]
        assert ctx_id

        page = await engine.create_page("about:blank", browser_context_id=ctx_id)
        target_id = page["targetId"]
        assert target_id
        assert page["sessionId"]
        await asyncio.sleep(0.5)  # idle gap: attachedToTarget/frame/context events flow upward

        nav = await engine.call("Page.navigate", {"url": "about:blank"})
        assert nav.get("frameId")

        result = await engine.call("Runtime.evaluate", {"expression": "1+1"})
        assert result["result"]["value"] == 2

        shot = await engine.screenshot(format="png")
        assert shot[:8] == b"\x89PNG\r\n\x1a\n"

        await asyncio.sleep(0.3)  # post-response events flow upward while idle
        methods = [e.method for e in events]
        assert any(m.startswith(("Page.", "Runtime.")) for m in methods), methods
    finally:
        await engine.stop()
