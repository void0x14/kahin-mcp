"""Real-browser checks for the shared engine capability contract.

These tests deliberately start Shadow first.  A visual capability must move
the live page into Camoufox inside Kahin; an agent must never be instructed to
leave the MCP surface for a second automation library.
"""

from __future__ import annotations

import base64
import json
import struct
from urllib.parse import quote

import pytest

from kahin import _state as state
from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.mirage import Mirage
from kahin.tools import emulation_mirage, engine, pilot


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


def _loads(text: str):
    return json.loads(text)


def _page() -> str:
    html = """<!doctype html><html><body>
      <main style="width: 480px; height: 240px; background: #1464a0; color: white">
        <h1 id="title">Kahin visual contract</h1>
      </main>
    </body></html>"""
    return "data:text/html," + quote(html)


@pytest.mark.asyncio
async def test_browser_start_defaults_to_camoufox() -> None:
    started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, ))
    assert started["engine"] == "mirage", started
    assert started["capabilities"]["visual"] is True, started
    try:
        health = _loads(await engine.engine_health())
        assert health["engine"] == "mirage", health
        assert health["alive"] is True, health
    finally:
        await pilot.browser_stop()


@pytest.mark.asyncio
async def test_shadow_cdp_screenshot_promotes_to_camoufox() -> None:
    started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="shadow"))
    assert started["engine"] == "shadow", started
    assert started["capabilities"]["screenshot"] is False, started
    try:
        navigated = _loads(await pilot.navigate(_page()))
        assert navigated.get("frameId"), navigated

        # This is the path an agent takes when it uses the generic CDP tool.
        raw = _loads(await pilot.execute_cdp("Page", "captureScreenshot", {}))
        assert raw.get("data"), raw
        assert state._current_engine is not None
        assert isinstance(state._current_engine, Mirage)

        health = _loads(await engine.engine_health())
        assert health["engine"] == "mirage", health
        assert health["capabilities"]["screenshot"] is True, health
    finally:
        await pilot.browser_stop()


@pytest.mark.asyncio
async def test_cdp_layout_metrics_are_real_on_camoufox() -> None:
    started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, ))
    assert started["engine"] == "mirage", started
    try:
        await pilot.navigate(
            "data:text/html," + quote("<html><body style='height:2400px'>metrics</body></html>")
        )
        metrics = _loads(await pilot.execute_cdp("Page", "getLayoutMetrics", {}))
        assert metrics["contentSize"]["height"] >= metrics["layoutViewport"]["clientHeight"]
        assert metrics["layoutViewport"]["clientWidth"] > 0
        assert metrics["visualViewport"]["scale"] == 1
    finally:
        await pilot.browser_stop()


@pytest.mark.asyncio
async def test_shared_screenshot_preserves_shadow_page_without_external_fallback() -> None:
    started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="shadow"))
    assert started["engine"] == "shadow", started
    try:
        await pilot.navigate(_page())
        viewport = _loads(await emulation_mirage.mirage_set_viewport(390, 844))
        assert viewport == {}, viewport
        assert isinstance(state._current_engine, Mirage)

        result = _loads(await pilot.screenshot())
        assert result["format"] == "png", result
        assert result["screenshot"].startswith("iVBOR"), result
        png = base64.b64decode(result["screenshot"])
        assert struct.unpack(">II", png[16:24]) == (390, 844), "mobile screenshot size drifted"

        body = await pilot.extract("#title")
        assert _loads(body)["result"]["value"] == "Kahin visual contract", body
    finally:
        await pilot.browser_stop()
