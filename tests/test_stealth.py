"""Stealth probe + identity-pin store tests — pure Python."""

from __future__ import annotations

import importlib.util
import json
from typing import Any

import pytest

from kahin.stealth import (
    STEALTH_PROBE_JS,
    _redact_proxy,
    load_pins,
    normalize_domain,
    pin_identity,
    pins_path,
    proxy_env,
    proxy_juggler_params,
    resolve_proxy_geo,
    save_pins,
    score_checks,
    unpin_identity,
)


def test_probe_covers_expected_checks() -> None:
    for needle in (
        "navigator.webdriver", "cdc_", "__playwright", "__kahin_dom_stream_v1",
        "navigator.plugins", "navigator.languages", "getBoundingClientRect",
        "elementFromPoint" if False else "navigator.permissions",
        "hardwareConcurrency", "AudioContext", "canvas",
    ):
        assert needle in STEALTH_PROBE_JS, f"missing probe: {needle}"


def test_probe_is_read_only() -> None:
    # no assignments, no addEventListener, no dispatchEvent, no localStorage
    assert "=" not in STEALTH_PROBE_JS.replace("===", "").replace("!==", "").replace("=>", "").replace("<=", "").replace(">=", "").replace("==", "")
    assert "addEventListener" not in STEALTH_PROBE_JS
    assert "localStorage" not in STEALTH_PROBE_JS


def test_score_checks() -> None:
    checks = [
        {"check": "a", "passed": True},
        {"check": "b", "passed": False},
        {"check": "c", "passed": True},
    ]
    score = score_checks(checks)
    assert score == {"passed": 2, "total": 3, "ratio": pytest.approx(2 / 3)}


def test_score_no_checks_is_zero() -> None:
    score = score_checks([])
    assert score["passed"] == 0 and score["total"] == 0 and score["ratio"] == 0.0


def test_normalize_domain() -> None:
    assert normalize_domain("HTTPS://Example.COM/Path") == "example.com"
    assert normalize_domain("http://Sub.Example.COM:8080/x/y") == "sub.example.com"
    assert normalize_domain("example.com:8443") == "example.com"
    assert normalize_domain("  EXAMPLE.com  ") == "example.com"
    assert normalize_domain("localhost") == "localhost"


def test_normalize_domain_rejects_malformed() -> None:
    for bad in (
        "bad domain!",
        "exa mple.com",
        "example..com",
        "-example.com",
        "example-.com",
        "exa_mple.com",
        "example.com<script>",
        "user@example.com",
        "http://",
        "",
        "a",
        "example",
        "example.com.",
    ):
        assert normalize_domain(bad) is None, bad


def test_pin_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kahin.stealth._PINS_FILE", tmp_path / "pins.json")
    pin_identity("example.com", "id1")
    assert load_pins() == {"example.com": "id1"}
    unpin_identity("example.com")
    assert load_pins() == {}


def test_pin_rejects_invalid_domain_and_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kahin.stealth._PINS_FILE", tmp_path / "pins.json")
    failed = pin_identity("bad domain!", "id1")
    assert failed is not None and failed["code"] == "invalid_argument"
    failed = pin_identity("example.com", "x" * 65)
    assert failed is not None and failed["code"] == "invalid_argument"
    assert load_pins() == {}


def test_save_pins_roundtrips_through_file(tmp_path, monkeypatch) -> None:
    pins_file = tmp_path / "pins.json"
    monkeypatch.setattr("kahin.stealth._PINS_FILE", pins_file)
    save_pins({"a.example.com": "id1", "b.example.com": "id2"})
    payload = json.loads(pins_file.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "pins": {"a.example.com": "id1", "b.example.com": "id2"},
    }
    assert pins_path() == pins_file


def test_save_pins_filters_invalid_entries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kahin.stealth._PINS_FILE", tmp_path / "pins.json")
    save_pins({"BAD DOMAIN!": "x", "example.com": "id1", "ok.example.org": "y" * 65})
    assert load_pins() == {"example.com": "id1"}


def test_load_pins_ignores_tampered_store(tmp_path, monkeypatch) -> None:
    pins_file = tmp_path / "pins.json"
    monkeypatch.setattr("kahin.stealth._PINS_FILE", pins_file)
    pins_file.write_text(
        json.dumps({
            "version": 1,
            "pins": {
                "not a domain": "x",
                "example.com": "ok",
                "example.net": 123,
                "a.example.org": "y" * 100,
            },
        }),
        encoding="utf-8",
    )
    assert load_pins() == {"example.com": "ok"}


def test_load_pins_corrupt_or_missing_store_is_empty(tmp_path, monkeypatch) -> None:
    pins_file = tmp_path / "pins.json"
    monkeypatch.setattr("kahin.stealth._PINS_FILE", pins_file)
    assert load_pins() == {}
    pins_file.write_text("{not json", encoding="utf-8")
    assert load_pins() == {}


def test_proxy_env_mapping() -> None:
    env = proxy_env("socks5://user:pass@127.0.0.1:1080")
    assert env["HTTPS_PROXY"] == "socks5://user:pass@127.0.0.1:1080"
    assert env["HTTP_PROXY"] == "socks5://user:pass@127.0.0.1:1080"
    assert env["ALL_PROXY"] == "socks5://user:pass@127.0.0.1:1080"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_proxy_env_accepts_http_https_socks4() -> None:
    for url in (
        "http://127.0.0.1:8080",
        "https://proxy.example.com:443",
        "socks4://10.0.0.1:1080",
    ):
        env = proxy_env(url)
        assert env["ALL_PROXY"] == url


def test_proxy_env_rejects_nonsense() -> None:
    for bad in (
        "not a proxy",
        "",
        "ftp://127.0.0.1:21",
        "http://",
        "socks5://",
        "http://host:notaport",
        "http://host:70000",
        "http://host:8080/path with space",
        "http://user:pass@\nhost:8080",
    ):
        with pytest.raises(ValueError):
            proxy_env(bad)


def test_proxy_env_errors_never_embed_credentials() -> None:
    with pytest.raises(ValueError) as excinfo:
        proxy_env("http://user:supersecret@:8080")
    assert "supersecret" not in str(excinfo.value)


def test_proxy_juggler_params_maps_schemes_and_default_ports() -> None:
    socks = proxy_juggler_params("socks5://43.161.246.231:10808")
    assert socks["type"] == "socks"
    assert socks["host"] == "43.161.246.231"
    assert socks["port"] == 10808
    assert socks["bypass"] == ["localhost", "127.0.0.1", "::1"]
    assert "username" not in socks and "password" not in socks

    assert proxy_juggler_params("socks4://10.0.0.1:1080")["type"] == "socks4"
    assert proxy_juggler_params("http://proxy.example.com")["type"] == "http"
    assert proxy_juggler_params("http://proxy.example.com")["port"] == 80
    assert proxy_juggler_params("https://proxy.example.com")["type"] == "https"
    assert proxy_juggler_params("https://proxy.example.com")["port"] == 443


def test_proxy_juggler_params_splits_and_decodes_credentials() -> None:
    params = proxy_juggler_params("socks5://user:pass@127.0.0.1:1080")
    assert params["username"] == "user"
    assert params["password"] == "pass"
    decoded = proxy_juggler_params("http://user%40x:p%3Ass@127.0.0.1:8080")
    assert decoded["username"] == "user@x"
    assert decoded["password"] == "p:ss"


def test_proxy_juggler_params_rejects_missing_or_zero_ports() -> None:
    for bad in ("socks5://host", "socks4://host", "socks5://host:0", "http://host:0"):
        with pytest.raises(ValueError):
            proxy_juggler_params(bad)


def test_proxy_juggler_params_reuses_proxy_env_validation() -> None:
    for bad in ("not a proxy", "", "ftp://127.0.0.1:21", "http://host:70000"):
        with pytest.raises(ValueError):
            proxy_juggler_params(bad)
    with pytest.raises(ValueError) as excinfo:
        proxy_juggler_params("http://user:supersecret@:8080")
    assert "supersecret" not in str(excinfo.value)


def test_redact_proxy_strips_credentials() -> None:
    assert _redact_proxy("socks5://user:pass@127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    redacted = _redact_proxy("http://user:secret@example.com:8080")
    assert "user" not in redacted and "secret" not in redacted
    assert _redact_proxy("garbage") == "<proxy>"


def test_resolve_proxy_geo_http_through_proxy(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeGeoResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.content = b"{}"
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeClient:
        def __init__(self, proxy: str, timeout: float) -> None:
            captured["proxy"] = proxy
            captured["timeout"] = timeout

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def get(self, url: str) -> _FakeGeoResponse:
            captured["url"] = url
            return _FakeGeoResponse({
                "ip": "1.2.3.4",
                "timezone": "Europe/Istanbul",
                "country_code": "TR",
                "country_name": "Turkey",
                "city": "Istanbul",
                "latitude": 41.0,
                "longitude": 28.9,
            })

    monkeypatch.setattr("kahin.stealth.httpx.Client", _FakeClient)
    geo = resolve_proxy_geo("http://user:pass@127.0.0.1:8080", timeout=3.0)
    assert geo["ip"] == "1.2.3.4"
    assert geo["timezone"] == "Europe/Istanbul"
    assert geo["country_code"] == "TR"
    assert geo["latitude"] == 41.0
    assert captured["proxy"] == "http://user:pass@127.0.0.1:8080"
    assert captured["timeout"] == 3.0
    assert "ipapi.co" in captured["url"]


def test_resolve_proxy_geo_failure_is_structured(monkeypatch) -> None:
    class _BoomClient:
        def __init__(self, proxy: str, timeout: float) -> None:
            pass

        def __enter__(self) -> _BoomClient:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def get(self, url: str) -> None:
            raise ConnectionError("boom")

    monkeypatch.setattr("kahin.stealth.httpx.Client", _BoomClient)
    geo = resolve_proxy_geo("http://127.0.0.1:1")
    assert geo.get("code") == "proxy_resolve_failed"
    assert "error" in geo
    assert geo.get("proxy") == "http://127.0.0.1:1"


def test_resolve_proxy_geo_invalid_url_is_structured() -> None:
    geo = resolve_proxy_geo("not a proxy")
    assert geo.get("code") == "proxy_resolve_failed"
    assert "error" in geo


def test_resolve_proxy_geo_socks_capability_error() -> None:
    if importlib.util.find_spec("socksio") is not None:
        pytest.skip("socksio installed; capability error path not applicable")
    geo = resolve_proxy_geo("socks5://user:pass@127.0.0.1:1080")
    assert geo.get("code") == "proxy_resolve_failed"
    assert geo.get("capability") == "socks_unsupported"
    assert "user:pass" not in geo.get("proxy", "")
