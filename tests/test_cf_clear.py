"""Unit tests for cf_clear_mirage (no browser required).

Covers argument validation and the static JS contracts. Live challenge
solving is covered by test_e2e_cf_clear.py against a real Camoufox.
"""

from __future__ import annotations

import json
import pathlib
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


def test_mount_heuristic_removed_from_module_source() -> None:
    # TR trust contract: no mount-div search, no pixel offset — frame-URL
    # filter + shadow walk give frame-anchored coordinates instead.
    source = pathlib.Path(cf.__file__).read_text()
    assert "_FIND_MOUNT_JS" not in source
    assert "_CHECKBOX_LEFT_PX" not in source


def test_checkbox_finder_js_uses_frame_filter_and_shadow_walk() -> None:
    # Mirrors cf_bypasser/core/bypasser.py _FIND_CHECKBOX_JS: walk open +
    # closed shadow roots for input[type=checkbox]; the frame-URL filter
    # marker lives in _CF_FRAME_MARKER used by the frame-tree scan.
    assert "fakeShadowRoot" in cf._FIND_CHECKBOX_JS
    assert "input[type=checkbox]" in cf._FIND_CHECKBOX_JS
    assert "challenges.cloudflare" in cf._CF_FRAME_MARKER


def test_retry_and_settle_constants_match_tr_reference() -> None:
    # TR: DEFAULT_MAX_RETRIES=5, CHALLENGE_SETTLE_SECONDS=5, retry poll 3s.
    assert cf._MAX_ATTEMPTS == 5
    assert cf._INITIAL_SETTLE_SECONDS == pytest.approx(5.0)
    assert cf._RETRY_POLL_SECONDS == pytest.approx(3.0)


def _eval_result(value: Any) -> dict[str, Any]:
    return {"result": {"value": value}}


def _stub_eval_result(value: Any) -> Any:
    async def fake(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _eval_result(value)

    return fake


@pytest.mark.asyncio
async def test_checkbox_gate_skips_missing_narrow_and_checked() -> None:
    # TR click gate (bypasser.py:175): skip when not found, w<=0, checked.
    cases = [
        {"found": False},
        {"found": True, "checked": False, "x": 10.0, "y": 10.0, "w": 0.0},
        {"found": True, "checked": True, "x": 10.0, "y": 10.0, "w": 20.0},
    ]
    real = cf._mirage_eval_result
    for info in cases:
        cf._mirage_eval_result = _stub_eval_result(info)  # type: ignore[assignment]
        try:
            assert await cf._checkbox_in_frame("sid", "frame1") is None
        finally:
            cf._mirage_eval_result = real


@pytest.mark.asyncio
async def test_checkbox_gate_returns_centre_when_clickable() -> None:
    real = cf._mirage_eval_result
    cf._mirage_eval_result = _stub_eval_result(  # type: ignore[assignment]
        {"found": True, "checked": False, "x": 10.0, "y": 20.0, "w": 20.0}
    )
    try:
        assert await cf._checkbox_in_frame("sid", "frame1") == {"x": 10.0, "y": 20.0}
    finally:
        cf._mirage_eval_result = real


@pytest.mark.asyncio
async def test_click_at_reports_dispatch_failure() -> None:
    real = cf._dispatch_mouse

    async def fake_dispatch(kind: str, x: float, y: float, **kwargs: Any) -> str:
        if kind == "mousedown":
            return '{"error": "wedged", "code": "tool_failed"}'
        raise AssertionError("mouseup must not run after a failed mousedown")

    cf._dispatch_mouse = fake_dispatch  # type: ignore[assignment]
    try:
        assert await cf._click_at("sid", 100.0, 100.0) is False
    finally:
        cf._dispatch_mouse = real
        cf._dispatch_mouse = real


@pytest.mark.asyncio
async def test_click_turnstile_reports_dispatch_and_verify_separately() -> None:
    # clicks counts dispatched press/release pairs even when verify fails.
    real_find = cf._find_checkbox
    real_click = cf._click_at
    real_verify = cf._verify_checkbox

    async def fake_find(_session_id: str) -> Any:
        return ("frame1", {"x": 10.0, "y": 20.0})

    async def fake_click(_session_id: str, _x: float, _y: float) -> bool:
        return True

    async def fake_verify(_session_id: str, _frame_id: str) -> bool:
        return False

    cf._find_checkbox = fake_find  # type: ignore[assignment]
    cf._click_at = fake_click  # type: ignore[assignment]
    cf._verify_checkbox = fake_verify  # type: ignore[assignment]
    try:
        assert await cf._click_turnstile("sid") == (True, False)
    finally:
        cf._find_checkbox = real_find
        cf._click_at = real_click
        cf._verify_checkbox = real_verify


@pytest.mark.asyncio
async def test_click_turnstile_reports_no_dispatch_when_lookup_misses() -> None:
    real_find = cf._find_checkbox

    async def fake_find(_session_id: str) -> Any:
        return None

    cf._find_checkbox = fake_find  # type: ignore[assignment]
    try:
        assert await cf._click_turnstile("sid") == (False, False)
    finally:
        cf._find_checkbox = real_find


def test_bypass_js_gates_on_title_and_blocks() -> None:
    assert "just a moment" in cf._IS_BYPASSED_JS
    assert "please complete the captcha" in cf._IS_BYPASSED_JS
    for marker in cf._BLOCK_MARKERS:
        assert marker in cf._IS_BYPASSED_JS


@pytest.mark.asyncio
async def test_fast_path_requires_host_scoped_clearance(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        async def call(self, method: str, params: dict[str, Any], session_id: str | None = None) -> Any:
            assert method == "Page.navigate"
            return {"frameId": "main"}

    async def fake_click(_sid: str) -> tuple[bool, bool]:
        return False, False

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cf, "_capture_page_session", lambda _tool: _session())
    monkeypatch.setattr(cf, "_mirage_engine", lambda: FakeEngine())
    monkeypatch.setattr(cf, "_challenge_probe", lambda _sid: _async_value(_ready()))
    monkeypatch.setattr(cf, "_is_bypassed", lambda _sid: _true())
    monkeypatch.setattr(cf, "_cf_cookies", lambda _sid, _host: _async_value(_cookies_without_clearance()))
    monkeypatch.setattr(cf.asyncio, "sleep", no_sleep)
    result = json.loads(await cf.cf_clear("https://example.com", timeout=5))
    assert result["cleared"] is False
    assert result["method"] == "timeout"


async def _session() -> tuple[str, None]:
    return "sid", None


async def _true() -> bool:
    return True


def _ready() -> dict[str, Any]:
    return {"detected": False}


def _cookies_without_clearance() -> dict[str, str]:
    return {"__cf_bm": "supporting-only"}


@pytest.mark.asyncio
async def test_empty_frame_url_requires_selected_frame_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = {"frame": {"id": "main", "url": "https://example.com"}, "childFrames": [
        {"frame": {"id": "challenge", "parentId": "main", "url": ""}},
    ]}
    monkeypatch.setattr(cf, "_frame_tree", lambda _sid: _async_value(tree))
    monkeypatch.setattr(cf, "_checkbox_in_frame", lambda _sid, _frame: _async_value({"x": 2.0, "y": 3.0}))
    monkeypatch.setattr(cf, "_frame_origin", lambda _sid, _frame, _tree: _async_value(None))
    assert await cf._find_checkbox("sid") is None


async def _async_value(value: Any) -> Any:
    return value


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
