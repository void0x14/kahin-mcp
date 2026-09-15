"""Unit tests for cf_clear_mirage (no browser required).

Covers argument validation and the static JS contracts. Live challenge
solving is covered by test_e2e_cf_clear.py against a real Camoufox.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kahin.tools import cf_clear_mirage as cf


@pytest.mark.asyncio
async def test_cf_clear_rejects_empty_url() -> None:
    resp = json.loads(await cf.cf_clear(url=""))
    assert resp["code"] == "invalid_argument"
    assert resp["tool"] == "kahin_cf_clear"


@pytest.mark.asyncio
async def test_cf_clear_rejects_non_string_url() -> None:
    resp = json.loads(await cf.cf_clear(url=None))  # type: ignore[arg-type]
    assert resp["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_cf_clear_rejects_oversized_url() -> None:
    resp = json.loads(await cf.cf_clear(url="https://x/" + "a" * 5000))
    assert resp["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_cf_clear_rejects_hostless_url() -> None:
    resp = json.loads(await cf.cf_clear(url="notaurl", timeout=5))
    assert resp["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_cf_clear_without_engine_reports_unavailable() -> None:
    resp = json.loads(await cf.cf_clear(url="https://example.com", timeout=5))
    assert resp["code"] == "engine_unavailable"


@pytest.mark.asyncio
async def test_cf_status_without_engine_reports_unavailable() -> None:
    resp = json.loads(await cf.cf_status())
    assert resp["code"] == "engine_unavailable"


def test_block_markers_exclude_ray_id_footers() -> None:
    assert "cloudflare ray id" not in cf._BLOCK_MARKERS
    assert "you have been blocked" in cf._BLOCK_MARKERS
    assert "error 1020" in cf._BLOCK_MARKERS


def test_mount_js_targets_response_input_not_iframe() -> None:
    # Live finding: the Turnstile iframe has an empty URL and a closed
    # shadow root — the mount div holding cf-turnstile-response is the
    # only JS-visible anchor.
    assert "cf-turnstile-response" in cf._FIND_MOUNT_JS
    assert "getBoundingClientRect" in cf._FIND_MOUNT_JS
    assert "iframe" not in cf._FIND_MOUNT_JS.lower()


def test_checkbox_geometry_matches_live_measurement() -> None:
    # Visible widget sits at the mount's left edge; checkbox centre ~19px
    # right of it at two viewports (1424w: 19px, 1920w: 18px observed).
    assert cf._CHECKBOX_LEFT_PX == pytest.approx(19.0, abs=1.0)


def test_bypass_js_gates_on_title_and_blocks() -> None:
    assert "just a moment" in cf._IS_BYPASSED_JS
    assert "please complete the captcha" in cf._IS_BYPASSED_JS
    for marker in cf._BLOCK_MARKERS:
        assert marker in cf._IS_BYPASSED_JS


@pytest.mark.asyncio
async def test_cf_cookies_scoped_to_host() -> None:
    class FakeEngine:
        async def call(self, method: str, params: dict[str, Any], session_id: str | None = None) -> Any:
            assert method == "Browser.getCookies"
            return {"cookies": [
                {"name": "cf_clearance", "value": "other-site", "domain": ".chatgpt.com"},
                {"name": "cf_clearance", "value": "ours", "domain": ".nopecha.com"},
                {"name": "__cf_bm", "value": "x", "domain": "nopecha.com"},
                {"name": "session", "value": "y", "domain": ".nopecha.com"},
            ]}

    real = cf._mirage_engine
    cf._mirage_engine = lambda: FakeEngine()  # type: ignore[assignment]
    try:
        out = await cf._cf_cookies("sid", "nopecha.com")
    finally:
        cf._mirage_engine = real
    assert out == {"cf_clearance": "ours", "__cf_bm": "x"}
