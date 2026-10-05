"""Contract tests for the kahin_waf_cookie_* MCP surface."""

from __future__ import annotations

import json

import pytest

from kahin.tools import waf_cookie as wt
from kahin.waf_cookie import NoxCookieError

CHALLENGE = b"""<!DOCTYPE html>
<html><head><script>
    window.__noxExpire=30;window.__noxDomain="";
    window.__noxImd = 1;
</script>
<script src="/sd5abc/static/wb/2.1/nox_20260413.js"></script>
<script src="/sd5abc/static/wb/2.0/gangplank_20251103.js"></script>
</head><body></body></html>"""


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    if wt._STORE is not None:
        wt._STORE.stop()
    wt._STORE = None
    yield
    if wt._STORE is not None:
        wt._STORE.stop()
    wt._STORE = None


def _fake_get(status: int, body: bytes):
    def get(url, timeout=0, limit=0):
        return status, body, {"content-type": "text/html"}

    return get


def _fake_host(monkeypatch, counter: dict | None = None):
    """Patch the host in kahin.waf_cookie: the tools layer only calls it."""
    import kahin.waf_cookie as waf

    counter = counter if counter is not None else {"n": 0}

    class FakeHost:
        def __init__(self, *args, **kwargs):
            pass

        def mint(self) -> str:
            counter["n"] += 1
            return f"2.0_{counter['n']:04d}_" + "Q" * 200

        def close(self) -> None:
            pass

    monkeypatch.setattr(waf, "_NoxHost", FakeHost)
    return counter


@pytest.mark.asyncio
async def test_status_without_a_store_reports_no_store():
    payload = json.loads(await wt.waf_cookie_status())
    assert payload["code"] == "no_store"
    assert "kahin_waf_cookie_mint" in payload["error"]


@pytest.mark.asyncio
async def test_header_is_empty_without_a_store():
    assert await wt.waf_cookie_header() == ""


@pytest.mark.asyncio
async def test_mint_publishes_the_delivery_contract(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    counter = _fake_host(monkeypatch)

    payload = json.loads(await wt.waf_cookie_mint("https://gitee.com", "/explore", False))
    assert payload["held"] is True
    assert payload["declared_ttl"] == 30
    assert payload["refresh_interval"] == pytest.approx(1188.0, abs=1.0)
    assert payload["cookie_file"].endswith("gitee.com.cookie")
    assert payload["host_running"] is True
    assert len(payload["scripts"]) == 2
    assert counter["n"] == 1

    header = await wt.waf_cookie_header()
    assert header.startswith("nox_jst_v1=2.0_0001_")


@pytest.mark.asyncio
async def test_force_re_mints_but_the_default_honours_the_interval(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    counter = _fake_host(monkeypatch)

    await wt.waf_cookie_mint("https://gitee.com", "/explore", False)
    await wt.waf_cookie_mint("https://gitee.com", "/explore", False)
    assert counter["n"] == 1

    await wt.waf_cookie_mint("https://gitee.com", "/explore", True)
    assert counter["n"] == 2


@pytest.mark.asyncio
async def test_mint_failure_is_structured_not_raised(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    monkeypatch.setattr(waf, "_http_get", _fake_get(200, b"<html><body>ok</body></html>"))
    payload = json.loads(await wt.waf_cookie_mint("https://gitee.com", "/explore", False))
    assert payload["code"] == "nox_cookie_unavailable"
    assert payload["origin"] == "https://gitee.com"


@pytest.mark.asyncio
async def test_bad_origin_is_rejected_before_any_request():
    payload = json.loads(await wt.waf_cookie_mint("x", "/explore", False))
    assert payload["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_missing_scheme_is_reported_as_invalid_argument():
    payload = json.loads(await wt.waf_cookie_mint("gitee.com", "/explore", False))
    assert payload["code"] == "invalid_argument"
    assert "scheme" in payload["error"]


@pytest.mark.asyncio
async def test_blank_probe_path_is_rejected():
    payload = json.loads(await wt.waf_cookie_mint("https://gitee.com", "   ", False))
    assert payload["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_probe_path_without_leading_slash_is_normalised(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    _fake_host(monkeypatch)
    payload = json.loads(await wt.waf_cookie_mint("https://gitee.com", "explore", False))
    assert payload["held"] is True
    assert wt._STORE.probe_path == "/explore"


@pytest.mark.asyncio
async def test_changing_origin_replaces_the_store(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    _fake_host(monkeypatch)

    await wt.waf_cookie_mint("https://gitee.com", "/explore", False)
    first = wt._STORE
    await wt.waf_cookie_mint("https://other.example", "/explore", False)
    assert wt._STORE is not first
    assert wt._STORE.origin == "https://other.example"


@pytest.mark.asyncio
async def test_store_survives_a_host_that_dies(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    _fake_host(monkeypatch)
    await wt.waf_cookie_mint("https://gitee.com", "/explore", False)

    class DeadHost:
        def __init__(self, *args, **kwargs):
            self._proc = None

        def mint(self) -> str:
            raise NoxCookieError("nox host exited with 5")

        def close(self) -> None:
            pass

    monkeypatch.setattr(waf, "_NoxHost", DeadHost)
    wt._STORE._host = DeadHost()
    payload = json.loads(await wt.waf_cookie_mint("https://gitee.com", "/explore", True))
    assert payload["code"] == "nox_cookie_unavailable"
    assert "exited" in payload["error"]


@pytest.mark.asyncio
async def test_header_returns_the_exact_cookie_pair(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    _fake_host(monkeypatch)
    await wt.waf_cookie_mint("https://gitee.com", "/explore", False)
    header = await wt.waf_cookie_header()
    name, _, value = header.partition("=")
    assert name == "nox_jst_v1"
    assert value.startswith("2.0_")
    assert ";" not in header


@pytest.mark.asyncio
async def test_cookie_file_is_json_a_scraper_can_read(tmp_path, monkeypatch):
    import kahin.waf_cookie as waf

    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE))
    _fake_host(monkeypatch)
    payload = json.loads(await wt.waf_cookie_mint("https://gitee.com", "/explore", False))
    state = json.loads(open(payload["cookie_file"], encoding="utf-8").read())
    assert state["name"] == "nox_jst_v1"
    assert state["value"].startswith("2.0_")
    assert state["declared_ttl"] == 30
    assert state["origin"] == "https://gitee.com"