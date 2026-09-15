"""Live CF test: cf_clear against a real Cloudflare-protected page.

Target: https://nopecha.com/demo/cloudflare (curl gets 403 + Just a
moment, so the challenge is real). Requires the sidecar + Camoufox
runtime; skipped otherwise — same gate as test_e2e_mirage.py.

Bounded live behaviour is environment-dependent: Cloudflare may grant
clearance, refuse the native attempt, or leave the challenge until the
bounded timeout. The test asserts only the neutral result contract and
never treats refusal as a deterministic server behaviour.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin import _state as state
from kahin.the_twins import mirage as mirage_mod
from kahin.tools import cf_clear_mirage as cf
from kahin.tools import pilot, trainman_mirage


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


def _loads(text: str) -> Any:
    return json.loads(text)


@async_fixture
async def mirage_tools() -> AsyncGenerator[None]:
    resp = _loads(await pilot.browser_start(engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tabs = _loads(await trainman_mirage.mirage_tab_list())
        assert len(tabs) == 1 and tabs[0].get("targetId"), tabs
        await asyncio.sleep(0.5)
        yield
    finally:
        await pilot.browser_stop()


@pytest.mark.asyncio
async def test_cf_clear_live_protected_page(mirage_tools: None) -> None:
    """cf_clear navigates, attempts, and reports honestly on a live page."""
    assert state._current_engine is not None
    resp = _loads(await cf.cf_clear(url="https://nopecha.com/demo/cloudflare", timeout=90))
    assert resp.get("url") == "https://nopecha.com/demo/cloudflare", resp
    assert resp.get("method") in {"none", "auto", "click", "blocked", "refused", "timeout"}, resp
    if resp.get("cleared") is True:
        # Bypass granted: the page itself must be open (title gate), and
        # any claimed clearance must be host-scoped, not the profile jar.
        status = _loads(await cf.cf_status())
        assert status.get("bypassed") is True, status
    else:
        # Refused/timeout: evidence + human-handoff contract, never a
        # silent pass and never a bare failure.
        assert resp.get("method") in {"blocked", "refused", "timeout"}, resp
        assert "action" in resp, resp
        assert resp.get("elapsedMs", 0) > 0, resp
