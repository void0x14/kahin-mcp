"""Contract tests for the Baidu ADAS nox cookie minter."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "kahin" / "waf_cookie.py"
_spec = importlib.util.spec_from_file_location("kahin_waf_cookie", _MODULE_PATH)
waf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(waf)

CHALLENGE = """<!DOCTYPE html>
<html><head><script>
    window.__noxExpire=30;window.__noxDomain="";
    window.__noxImd = 1;
</script>
<script src="/sd5abc/static/wb/2.1/nox_20260413.js"></script>
<script src="/sd5abc/static/wb/2.0/gangplank_20251103.js"></script>
</head><body></body></html>"""

REAL_PAGE = "<html><body>repository cards</body></html>"


def _fake_get(status: int, body: bytes):
    def get(url, timeout=0, limit=0):
        return status, body, {"content-type": "text/html"}

    return get


def test_challenge_yields_script_urls_and_ttl():
    urls = waf._find_scripts(CHALLENGE, "https://gitee.com")
    assert urls == [
        "https://gitee.com/sd5abc/static/wb/2.1/nox_20260413.js",
        "https://gitee.com/sd5abc/static/wb/2.0/gangplank_20251103.js",
    ]
    assert waf._declared_ttl(CHALLENGE) == 30


def test_challenge_without_scripts_is_rejected():
    with pytest.raises(waf.NoxCookieError, match="no nox/gangplank"):
        waf._find_scripts("<html><body>nothing</body></html>", "https://gitee.com")


def test_script_outside_origin_is_rejected():
    hostile = '<html><script src="https://evil.example/nox_x.js"></script></html>'
    with pytest.raises(waf.NoxCookieError, match="outside"):
        waf._find_scripts(hostile, "https://gitee.com")


def test_absolute_origin_is_required():
    with pytest.raises(ValueError, match="scheme"):
        waf.NoxCookieStore("gitee.com")


def test_refresh_interval_stays_inside_the_measured_window():
    """The WAF kills a cookie between minute 27 and 30; the interval must be shorter."""
    store = waf.NoxCookieStore("https://gitee.com")
    store.declared_ttl = 30
    interval = store.refresh_interval
    # 30 min TTL * 0.66 = 1188 s = 19.8 min, inside the measured 27 min wall
    assert interval == pytest.approx(1188.0, abs=1.0)
    assert interval < 27 * 60


def test_refresh_interval_falls_back_when_ttl_is_undeclared():
    store = waf.NoxCookieStore("https://gitee.com")
    assert store.refresh_interval == pytest.approx(1800 * waf.REFRESH_FRACTION * 60.0, abs=1.0)


def test_interval_never_drops_below_the_floor():
    """A tiny declared TTL must not turn the interval into a busy loop."""
    store = waf.NoxCookieStore("https://gitee.com")
    store.declared_ttl = 1
    assert store.refresh_interval == waf.MIN_REFRESH_SECONDS
    store.declared_ttl = 10
    assert store.refresh_interval == pytest.approx(396.0, abs=0.1)


def test_undeclared_ttl_uses_a_conservative_fallback():
    """Without __noxExpire the store must not pretend the cookie lasts 30 minutes."""
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    assert store.declared_ttl is None
    assert store.refresh_interval > 27 * 60


def test_held_cookie_reports_fresh_then_stale():
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    store.declared_ttl = 30
    store.cookie = "2.0_abcd_x"
    store.minted_at = waf.time.time()
    assert store.is_fresh()
    # one second past the 1188 s interval is stale
    store.minted_at = waf.time.time() - (store.refresh_interval + 1)
    assert not store.is_fresh()
    assert store.age() > store.refresh_interval


def test_cookie_header_is_empty_without_a_cookie():
    assert waf.NoxCookieStore("https://gitee.com", write_file=False).cookie_header() == ""


def test_cookie_file_name_is_derived_from_origin():
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    assert store.cookie_file().name == "gitee.com.cookie"


def test_refresh_writes_a_bounded_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", probe_path="/explore")

    scripts = [tmp_path / "nox_a.js", tmp_path / "gang_a.js"]
    for script in scripts:
        script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE.encode()))

    calls: list[str] = []

    class FakeHost:
        def __init__(self, *args, **kwargs):
            calls.append("boot")

        def mint(self) -> str:
            calls.append("mint")
            return "2.0_1234_" + "Q" * 200

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(waf, "_NoxHost", FakeHost)

    value = store.refresh()
    assert value.startswith("2.0_1234_")
    assert store.declared_ttl == 30
    assert calls == ["boot", "mint"]

    state = json.loads(store.cookie_file().read_text())
    assert state["origin"] == "https://gitee.com"
    assert state["name"] == "nox_jst_v1"
    assert state["value"] == value
    assert state["declared_ttl"] == 30
    assert state["refresh_interval"] == pytest.approx(1188.0, abs=1.0)
    assert state["stealth_globals"] == ["__KA_USER_AGENT", "__KA_LOCATION"]


def test_ensure_fresh_skips_a_second_mint_while_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", write_file=True)
    scripts = [tmp_path / "nox_a.js"]
    scripts[0].write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE.encode()))

    mints: list[int] = []

    class FakeHost:
        def __init__(self, *args, **kwargs):
            pass

        def mint(self) -> str:
            mints.append(1)
            return f"2.0_{len(mints):04d}_" + "Q" * 200

        def close(self) -> None:
            pass

    monkeypatch.setattr(waf, "_NoxHost", FakeHost)
    first = store.ensure_fresh()
    second = store.ensure_fresh()
    assert first == second
    assert len(mints) == 1
    # the challenge is still re-read to notice a changed script version, but the
    # cookie is not re-minted while it is inside the refresh interval
    assert store.is_fresh()


def test_refresh_fails_when_the_site_stops_challenging(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    monkeypatch.setattr(waf, "_http_get", _fake_get(200, REAL_PAGE.encode()))
    with pytest.raises(waf.NoxCookieError, match="without a WAF challenge"):
        store.refresh()


def test_missing_host_binary_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.setenv("KAHIN_NOX_HOST", str(tmp_path / "nope"))
    with pytest.raises(waf.NoxCookieError, match="missing file"):
        waf._host_binary()


def test_missing_shim_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(waf, "_addon_dir", lambda: tmp_path)
    with pytest.raises(waf.NoxCookieError, match="shim is missing"):
        waf._shim_path()


def test_missing_host_binary_without_override_names_the_build_command(monkeypatch, tmp_path):
    monkeypatch.delenv("KAHIN_NOX_HOST", raising=False)
    monkeypatch.setattr(waf, "_addon_dir", lambda: tmp_path)
    with pytest.raises(waf.NoxCookieError, match="kahin.waf_cookie --build"):
        waf._host_binary()


def test_script_cache_is_reused_without_a_second_fetch(tmp_path, monkeypatch):
    cache = tmp_path / "nox"
    cache.mkdir()
    target = cache / "nox_20260413.js"
    target.write_bytes(b"// cached")

    seen: list[str] = []

    def counting_get(url, timeout=0, limit=0):
        seen.append(url)
        return 200, b"// fresh", {}

    monkeypatch.setattr(waf, "_http_get", counting_get)
    paths = waf._download_scripts(["https://gitee.com/x/nox_20260413.js"], cache)
    assert paths == [target]
    assert seen == []
    assert target.read_bytes() == b"// cached"


def test_symlinked_cache_entry_is_refused(tmp_path, monkeypatch):
    cache = tmp_path / "nox"
    cache.mkdir()
    real = tmp_path / "outside.js"
    real.write_text("// real")
    (cache / "nox_a.js").symlink_to(real)
    with pytest.raises(waf.NoxCookieError, match="symlink"):
        waf._download_scripts(["https://gitee.com/nox_a.js"], cache)


def test_oversized_script_is_refused(tmp_path, monkeypatch):
    cache = tmp_path / "nox"

    def big_get(url, timeout=0, limit=0):
        return 200, b"x" * (waf.MAX_SCRIPT_READ + 10), {}

    monkeypatch.setattr(waf, "_http_get", big_get)
    with pytest.raises(waf.NoxCookieError, match="exceeds"):
        waf._download_scripts(["https://gitee.com/nox_big.js"], cache)


def test_symlinked_cookie_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com")
    store.cookie = "2.0_abcd_value"
    store.minted_at = waf.time.time()
    path = store.cookie_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(waf.NoxCookieError, match="symlink"):
        store._publish()


def test_unusable_mint_value_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE.encode()))

    class FakeHost:
        def __init__(self, *args, **kwargs):
            pass

        def mint(self) -> str:
            return ""

        def close(self) -> None:
            pass

    monkeypatch.setattr(waf, "_NoxHost", FakeHost)
    with pytest.raises(waf.NoxCookieError, match="unusable cookie"):
        store.refresh()


def test_host_serve_mode_is_only_started_once(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE.encode()))

    boots: list[int] = []
    counter = {"n": 0}

    class FakeHost:
        def __init__(self, *args, **kwargs):
            boots.append(1)
            counter["n"] += 1

        def mint(self) -> str:
            return f"2.0_{counter['n']:04d}_" + "Q" * 200

        def close(self) -> None:
            pass

    monkeypatch.setattr(waf, "_NoxHost", FakeHost)
    store.refresh()
    store.refresh(force=True)
    assert len(boots) == 1
    store.stop()


def test_status_reports_the_delivery_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", write_file=True)
    store.cookie = "2.0_abcd_value"
    store.minted_at = waf.time.time()
    store.declared_ttl = 30
    status = store.status()
    assert status["cookie_name"] == "nox_jst_v1"
    assert status["held"] is True
    assert status["fresh"] is True
    assert status["refresh_interval"] == pytest.approx(1188.0, abs=1.0)
    assert status["cookie_file"].endswith("gitee.com.cookie")
    assert status["host_running"] is False


def test_concurrent_mints_do_not_interleave(tmp_path, monkeypatch):
    """Two scrapers hitting the store at once must each get a whole cookie."""
    monkeypatch.setenv("KAHIN_HOME", str(tmp_path))
    store = waf.NoxCookieStore("https://gitee.com", write_file=False)
    script = tmp_path / "nox_a.js"
    script.write_text("// fake")
    monkeypatch.setattr(waf, "_http_get", _fake_get(405, CHALLENGE.encode()))

    counter = {"n": 0}
    in_flight = {"now": 0, "max": 0}

    class FakeHost:
        def __init__(self, *args, **kwargs):
            pass

        def mint(self) -> str:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            time.sleep(0.01)
            counter["n"] += 1
            value = f"2.0_{counter['n']:04d}_" + "Q" * 200
            in_flight["now"] -= 1
            return value

        def close(self) -> None:
            pass

    monkeypatch.setattr(waf, "_NoxHost", FakeHost)

    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(store.refresh(force=True))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 6
    # the store lock serialises refreshes, so no two mints overlap
    assert in_flight["max"] == 1
    assert len(set(results)) == 6