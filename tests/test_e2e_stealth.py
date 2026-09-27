"""Stealth e2e over real Camoufox (Faz 3, Tasks 1 + 3 + 4 + 5 + 6).

Task 1: the read-only fingerprint audit runs against a live page and must
report every check with a real score. Task 3: humanized input — Bézier
mouse trajectory, humanized click (real DOM click at the element center)
and cadence typing (real keydown/keyup pairs with jittered delays). Every
scenario drives the same async functions the MCP server exposes, so the
JSON-string tool contract is what gets verified. Task 4: the per-domain
identity pin policy — pin/unpin/pins/for_domain through the public tool
functions, with a real saved identity. Task 5: the live fingerprint
report, proxy_resolve validation/capability errors, and the
browser_start(proxy=...) configuration-conflict contract on engine reuse.
Task 6: the CI gate — with KAHIN_REQUIRE_STEALTH=1 the audit ratio is a
hard gate (>= 0.8) and identity rotation must change the page-observed
fingerprint as a whole (full-summary digest + at least two independent
signature dimensions), never a UA-only weakening.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import agent_mirage, pilot, pilot_mirage, stealth_mirage, trainman_mirage


def _real_available() -> bool:
    """True when the sidecar binary and a real Camoufox are both present."""
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(not _real_available(), reason="sidecar binary or Camoufox missing")


def _doc(body: str) -> str:
    """A data: URL carrying an HTML document (no network involved)."""
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


def _summary_signature_diffs(a: dict[str, Any], b: dict[str, Any]) -> int:
    """Count independent fingerprint dimensions that differ between two
    fingerprint_report summaries. userAgent is one dimension among several;
    a UA-only difference scores 1."""
    diffs = 0
    for field in ("userAgent", "platform", "oscpu", "hardwareConcurrency", "deviceMemory"):
        if a.get(field) != b.get(field):
            diffs += 1
    sa, sb = a.get("screen") or {}, b.get("screen") or {}
    if (sa.get("width"), sa.get("height")) != (sb.get("width"), sb.get("height")):
        diffs += 1
    wa, wb = a.get("webgl") or {}, b.get("webgl") or {}
    if (wa.get("vendor"), wa.get("renderer")) != (wb.get("vendor"), wb.get("renderer")):
        diffs += 1
    return diffs


def _summary_digest(summary: dict[str, Any]) -> str:
    """Deterministic digest of the FULL fingerprint report summary."""
    return json.dumps(summary, sort_keys=True, default=str)


def _identity_draw_differs(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when two identity_new generator summaries differ in a field the
    page can observe in every environment. webGl:renderer is deliberately not
    counted: the spoofed renderer is only readable when WebGL context creation
    works, and the CI gate runs without a GL stack (webgl == {}), so a
    webgl-only difference would let the gate claim rotation while the observed
    fingerprint is unchanged."""
    for key in ("navigator.userAgent", "screen.width", "screen.height", "navigator.hardwareConcurrency"):
        if a.get(key) != b.get(key):
            return True
    return False


async def _start_identity_engine(name: str) -> dict[str, Any]:
    """Stop any engine, boot with a saved identity, and return the live
    page-observed fingerprint summary from kahin_fingerprint_report."""
    await pilot.browser_stop()
    started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", identity=name))
    assert started.get("status") == "started", started
    try:
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)
        report = _loads(await stealth_mirage.fingerprint_report())
        assert report.get("engine") == "mirage", report
        summary = report.get("summary")
        assert isinstance(summary, dict) and summary, report
        return summary
    finally:
        await pilot.browser_stop()


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


@pytest.mark.asyncio
async def test_stealth_audit_reports_all_checks(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc("<html><body>probe</body></html>"))
    await asyncio.sleep(0.2)
    result = _loads(await stealth_mirage.stealth_audit())
    assert result.get("audited") is True, result
    assert result.get("engine") == "mirage"
    assert isinstance(result.get("checks"), list) and len(result["checks"]) >= 10, result
    assert result["score"]["total"] == len(result["checks"]), result


@pytest.mark.asyncio
async def test_mouse_trajectory_moves(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc("<html><body>trajectory</body></html>"))
    await asyncio.sleep(0.2)
    result = _loads(
        await stealth_mirage.mirage_mouse_trajectory(320, 240, steps=12, jitter=1.0, seed=7)
    )
    assert not result.get("error"), result
    assert result.get("moved") == 12, result
    assert result.get("to") == [320.0, 240.0], result
    assert isinstance(result.get("from"), list) and len(result["from"]) == 2, result


@pytest.mark.asyncio
async def test_humanized_click_fires_event(mirage_tools: None) -> None:
    await pilot.navigate(
        url=_doc(
            "<html><body><button id='b' style='position:absolute;left:10px;top:10px;width:100px;height:40px'>H</button>"
            "<div id='log'></div>"
            "<script>document.getElementById('b').addEventListener('click', () => { "
            "document.getElementById('log').textContent = 'clicked'; });</script></body></html>"
        )
    )
    await asyncio.sleep(0.2)
    result = _loads(
        await stealth_mirage.mirage_click_humanized(
            "#b",
            steps=20,
            jitter=1.5,
            click_delay_ms=60.0,
            timeout=5.0,
        )
    )
    assert not result.get("error"), result
    assert result.get("moved", 0) >= 10, result
    assert isinstance(result.get("to"), list) and len(result["to"]) == 2, result
    text = await pilot_mirage.mirage_get_text("#log")
    assert "clicked" in text, text


@pytest.mark.asyncio
async def test_cadence_typing_enters_text(mirage_tools: None) -> None:
    await pilot.navigate(
        url=_doc(
            "<html><body><input id='i'><script>"
            "document.getElementById('i').addEventListener('input', () => { "
            "document.getElementById('i').dataset.changed = '1'; });</script></body></html>"
        )
    )
    await asyncio.sleep(0.2)
    focus = await pilot_mirage.mirage_focus("#i")
    assert "focused" in focus, focus
    result = _loads(
        await stealth_mirage.mirage_key_text(
            "hello",
            delay_ms=20.0,
            jitter_ms=10.0,
            seed=5,
        )
    )
    assert not result.get("error"), result
    assert result.get("typed") == "hello", result
    assert len(result.get("cadence", [])) == 5, result
    value = await pilot_mirage.mirage_get_value("#i")
    assert "hello" in value, value


@pytest.mark.asyncio
async def test_identity_pin_roundtrip(
    mirage_tools: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-domain pin policy end-to-end: save an identity, pin a canonical
    domain, look it up, list the map, unpin — all through the public tool
    functions, with the store redirected to a temp file."""
    monkeypatch.setattr("kahin.stealth._PINS_FILE", tmp_path / "pins.json")
    created = _loads(await agent_mirage.identity_new("pin-e2e"))
    assert created.get("saved") is True, created
    try:
        pinned = _loads(
            await stealth_mirage.identity_pin(
                "https://Example.COM:8080/login",
                "pin-e2e",
            )
        )
        assert pinned == {"pinned": True, "domain": "example.com", "name": "pin-e2e"}, pinned

        looked = _loads(await stealth_mirage.identity_for_domain("EXAMPLE.com"))
        assert looked["domain"] == "example.com", looked
        assert looked["name"] == "pin-e2e", looked
        assert "kahin_browser_start(identity='pin-e2e')" in looked["hint"], looked

        listed = _loads(await stealth_mirage.identity_pins())
        assert listed["pins"] == {"example.com": "pin-e2e"}, listed

        bad = _loads(await stealth_mirage.identity_pin("bad domain!", "pin-e2e"))
        assert bad.get("code") == "invalid_argument", bad

        unknown = _loads(await stealth_mirage.identity_pin("example.com", "nope"))
        assert unknown.get("code") == "invalid_argument", unknown
        assert unknown.get("field") == "name", unknown

        unpinned = _loads(await stealth_mirage.identity_unpin("https://example.com/x"))
        assert unpinned == {"unpinned": True, "domain": "example.com"}, unpinned

        after = _loads(await stealth_mirage.identity_for_domain("example.com"))
        assert after["name"] is None, after
        assert "rotate freely" in after["hint"], after
        assert _loads(await stealth_mirage.identity_pins())["pins"] == {}
    finally:
        await agent_mirage.identity_delete("pin-e2e")


@pytest.mark.asyncio
async def test_fingerprint_report_live_page(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc("<html><body>fp</body></html>"))
    await asyncio.sleep(0.2)
    result = _loads(await stealth_mirage.fingerprint_report())
    assert result.get("engine") == "mirage", result
    summary = result.get("summary") or {}
    assert summary.get("userAgent"), summary
    assert summary.get("platform"), summary
    assert summary.get("timezone"), summary
    assert summary.get("locale"), summary
    assert isinstance(summary.get("screen"), dict) and summary["screen"].get("width", 0) > 0
    assert isinstance(summary.get("viewport"), dict) and summary["viewport"].get("width", 0) > 0
    assert isinstance(summary.get("languages"), list) and summary["languages"]
    assert isinstance(summary.get("webgl"), dict)
    assert "hardwareConcurrency" in summary


@pytest.mark.asyncio
async def test_proxy_resolve_validation_and_capabilities(mirage_tools: None) -> None:
    bad = _loads(await stealth_mirage.proxy_resolve("not a proxy"))
    assert bad.get("code") == "invalid_argument", bad
    assert "error" in bad
    unreachable = _loads(await stealth_mirage.proxy_resolve("http://127.0.0.1:1", timeout=2.0))
    assert unreachable.get("code") == "proxy_resolve_failed", unreachable
    assert "error" in unreachable
    assert unreachable.get("proxy") == "http://127.0.0.1:1"
    creds = _loads(
        await stealth_mirage.proxy_resolve(
            "http://user:pass@127.0.0.1:1",
            timeout=2.0,
        )
    )
    assert "user:pass" not in creds.get("proxy", ""), creds


@pytest.mark.asyncio
async def test_browser_start_identity_conflict(mirage_tools: None) -> None:
    created = _loads(await agent_mirage.identity_new("conflict-e2e"))
    assert created.get("saved"), created
    try:
        conflict = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", identity="conflict-e2e"))
        assert conflict.get("code") == "engine_config_conflict", conflict
        assert conflict.get("requested") == {"identity": "conflict-e2e"}, conflict
    finally:
        await agent_mirage.identity_delete("conflict-e2e")


@pytest.mark.asyncio
async def test_browser_start_proxy_conflict_and_reuse(mirage_tools: None) -> None:
    # mirage_tools fixture runs an engine WITHOUT proxy; requesting one
    # must be a structured conflict, never a silent ignore.
    conflict = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", proxy="http://127.0.0.1:8080"))
    assert conflict.get("code") == "engine_config_conflict", conflict
    assert "kahin_browser_stop" in conflict.get("hint", ""), conflict
    assert conflict.get("requested") == {"proxy": "http://127.0.0.1:8080"}, conflict
    assert conflict.get("active") == {"proxy": None}, conflict
    # no proxy requested → plain idempotent reuse
    reuse = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage"))
    assert reuse.get("status") == "reused", reuse


@pytest.mark.asyncio
async def test_browser_start_proxy_records_active_metadata() -> None:
    from kahin import _state

    stopped = _loads(await pilot.browser_stop())
    # No engine is running without the fixture: engine_unavailable is fine.
    assert stopped.get("status") == "stopped" or stopped.get("code") == "engine_unavailable", (
        stopped
    )
    started = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", proxy="http://127.0.0.1:1"))
    assert started.get("status") == "started", started
    try:
        engine = _state._current_engine
        assert engine is not None
        assert getattr(engine, "_proxy_url", None) == "http://127.0.0.1:1"
        # identical request reuses cleanly
        reuse = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", proxy="http://127.0.0.1:1"))
        assert reuse.get("status") == "reused", reuse
        # a different proxy is a conflict
        conflict = _loads(await pilot.browser_start(mode="kes", ephemeral_ack=True, engine="mirage", proxy="http://127.0.0.1:2"))
        assert conflict.get("code") == "engine_config_conflict", conflict
    finally:
        await pilot.browser_stop()


@pytest.mark.asyncio
async def test_stealth_audit_ratio_gate(mirage_tools: None) -> None:
    """Faz 3 Task 6: under KAHIN_REQUIRE_STEALTH=1 the self-audit ratio is a
    hard gate (>= 0.8). The page is a bare document with no DOM stream
    installed, so nothing the agent's own tooling does can sabotage a check;
    a leak on this build fails the gate instead of being skipped."""
    require_stealth = os.environ.get("KAHIN_REQUIRE_STEALTH") == "1"
    await pilot.navigate(url=_doc("<html><body>audit gate</body></html>"))
    await asyncio.sleep(0.2)
    result = _loads(await stealth_mirage.stealth_audit())
    assert result.get("audited") is True, result
    score = result.get("score") or {}
    checks = result.get("checks") or []
    assert isinstance(checks, list) and len(checks) >= 10, result
    assert score.get("total") == len(checks), result
    ratio = score.get("ratio")
    assert isinstance(ratio, (int, float)), result
    if require_stealth:
        failed = [c for c in checks if not c.get("passed")]
        assert ratio >= 0.8, f"stealth gate: audit ratio {ratio} < 0.8; failed checks: {failed}"


@pytest.mark.asyncio
async def test_identity_rotation_changes_fingerprint(mirage_tools: None) -> None:
    """Faz 3 Task 6 rotation gate: rotating browser_start(identity=...) must
    change the page-observed fingerprint as a WHOLE — the full summary
    digest differs AND at least two independent signature dimensions
    (userAgent/platform/screen/WebGL/hardware) differ, never a UA-only
    weakening. The installed fingerprint generator has no seed parameter, so
    distinctness is produced by bounded retry-until-distinct at generation
    time (max 4 draws); under KAHIN_REQUIRE_STEALTH=1 this is a hard gate."""
    require_stealth = os.environ.get("KAHIN_REQUIRE_STEALTH") == "1"
    # Use two explicit OS families so the live browser must expose at least
    # two observable dimensions even on runners where WebGL is unavailable.
    # Random-vs-random generation can legitimately differ only in WebGL, which
    # made this gate depend on the runner's graphics stack.
    base = _loads(await agent_mirage.identity_new("rot-base", os_target="linux"))
    assert base.get("saved"), base
    candidate = _loads(await agent_mirage.identity_new("rot-candidate", os_target="windows"))
    assert candidate.get("saved"), candidate
    draws = 1
    while draws < 4 and not _identity_draw_differs(
        base.get("summary") or {},
        candidate.get("summary") or {},
    ):
        await agent_mirage.identity_delete("rot-candidate")
        candidate = _loads(await agent_mirage.identity_new("rot-candidate"))
        assert candidate.get("saved"), candidate
        draws += 1
    if not _identity_draw_differs(base.get("summary") or {}, candidate.get("summary") or {}):
        pytest.fail("identity generator produced 4 identical draws; retry-until-distinct exhausted")
    try:
        first = await _start_identity_engine("rot-base")
        second = await _start_identity_engine("rot-candidate")
        if require_stealth:
            assert _summary_digest(first) != _summary_digest(second), (
                "rotation did not change the full fingerprint summary:\n"
                f"{_summary_digest(first)}\n{_summary_digest(second)}"
            )
            diffs = _summary_signature_diffs(first, second)
            assert diffs >= 2, (
                f"rotation changed only {diffs} fingerprint dimension(s); "
                f"a UA-only weakening is not a valid rotation:\n{first}\n{second}"
            )
    finally:
        await pilot.browser_stop()
        await agent_mirage.identity_delete("rot-base")
        await agent_mirage.identity_delete("rot-candidate")
