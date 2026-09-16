"""stealth.py — Self-audit probe package for the anti-detect surface.

Read-only page probes that surface automation leaks and fingerprint
inconsistencies. Every check is plain page JavaScript; nothing writes to
the DOM, listens to events, or persists state — running the audit never
contaminates the audited page.

The probe list mirrors the vectors CreepJS/botd-style detectors use:
webdriver flag, CDP/automation markers, injected binding visibility,
plugin/language sanity, native-prototype integrity, platform/timezone/
screen consistency, and WebGL/audio presence.

The probe is a single expression (no assignments, no event wiring) so the
read-only guarantee holds literally: there is nothing in the script that
could mutate the page.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import orjson

STEALTH_PROBE_JS = r"""
((r) => ({
  checks: [
    r("webdriver", navigator.webdriver === undefined || navigator.webdriver === false, navigator.webdriver),
    ((found) => r("cdp-markers", found.length === 0, found.join(",")))((["cdc_", "__selenium", "__webdriver_evaluate", "__playwright", "__pw_", "__lastWatcher"]).filter((m) => Object.getOwnPropertyNames(window).some((p) => p.includes(m)))),
    r("binding-hidden", typeof window.__kahin_dom_stream_v1 === "undefined" || window.__kahin_dom_stream_v1 === null, typeof window.__kahin_dom_stream_v1),
    r("plugins", (navigator.plugins && navigator.plugins.length) > 0, navigator.plugins && navigator.plugins.length),
    r("languages", Array.isArray(navigator.languages) && navigator.languages.length > 0, navigator.languages && navigator.languages.join(",")),
    r("platform", typeof navigator.platform === "string" && navigator.platform.length > 0, navigator.platform),
    r("oscpu", typeof navigator.oscpu === "string" && navigator.oscpu.length > 0, navigator.oscpu),
    ((tz) => r("timezone-sane", typeof tz === "string" && tz.length > 0 && (tz === "UTC" || tz.includes("/")), tz))((() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ""; } })()),
    r("screen-sane", screen && screen.width > 0 && screen.height > 0 && window.innerWidth > 0 && window.innerHeight > 0, screen.width + "x" + screen.height),
    ((text) => r("prototype-integrity", text.includes("[native code]"), "native toString"))((() => { try { return Element.prototype.getBoundingClientRect.toString(); } catch (e) { return ""; } })()),
    r("permissions-api", typeof navigator.permissions !== "undefined", typeof navigator.permissions),
    // WebGL is an API-level property of the browser build: the interface and
    // the canvas context entry point exist regardless of the runner's GL
    // stack. Context creation itself is environment-dependent (a GL-less CI
    // runner or headless Xvfb can legitimately return null), so requiring a
    // live context would make this check flake on runners without GL. The
    // spoofed vendor/renderer values are verified by fingerprint_report
    // where a context can be created.
    ((ok) => r("webgl", ok, ok ? "webgl api present" : "webgl api missing"))((() => { try { return typeof WebGLRenderingContext !== "undefined" && typeof document.createElement("canvas").getContext === "function"; } catch (e) { return false; } })()),
    r("audio", typeof (window.AudioContext || window.webkitAudioContext) !== "undefined", "AudioContext present"),
    r("hardware-concurrency", typeof navigator.hardwareConcurrency === "number" && navigator.hardwareConcurrency > 0, navigator.hardwareConcurrency),
  ],
}))((check, passed, detail) => ({check, passed: !!passed, detail: String(detail == null ? "" : detail).slice(0, 200)}))
"""


def score_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a list of ``{check, passed, detail}`` results.

    Never skips a check: unknown or malformed entries simply count as not
    passed, and the ratio is exact (no rounding) so callers can gate on a
    threshold without surprises.
    """
    total = len(checks)
    passed = sum(1 for check in checks if bool(check.get("passed")))
    return {
        "passed": passed,
        "total": total,
        "ratio": (passed / total) if total else 0.0,
    }


# Launch policy (crawler/rotation Task 1): the fixed Camoufox launch options
# bound on EVERY Mirage start, default or identity-pinned. This is static,
# payload-free policy — safe to expose in MCP surfaces:
# - ``headless`` follows the engine's headless flag so fingerprint
#   generation (screen/mediaDevices) matches the real headless/visible mode;
# - ``humanize=False`` keeps Camoufox's C++ humanized input layer OFF. With
#   humanize=True the browser expands every mousemove into a multi-point
#   trajectory whose intermediate points each await a renderer ack; when a
#   point's ack is dropped (event coalescing on short moves) the
#   Page.dispatchMouseEvent call never resolves and the whole input channel
#   wedges behind it. Kahin's own humanization (Bezier trajectories,
#   jittered clicks, typing cadence in kahin/humanize.py) dispatches one
#   point per RPC and waits for its response, so it is safe without the
#   broken browser-side trajectory layer;
# - ``enable_cache=True`` keeps Firefox's cache on (bounded memory cost,
#   real crawl continuity);
# - ``block_webgl=False`` keeps WebGL enabled with its sampled fingerprint —
#   blocking it is a leak vector in itself and Camoufox only recommends it
#   for special cases;
# - ``main_world_eval=False`` keeps the Juggler main-world binding OFF, so
#   injected bindings never land in the page's main world (the DOM stream
#   uses browser-level addBinding/setInitScripts, not allowMainWorld).
def launch_policy(headless: bool = True) -> dict[str, Any]:
    """The fixed Camoufox launch policy bound on every Mirage start."""
    import os as _os

    # Experimental CF gate: KAHIN_HUMANIZE=1 enables Camoufox cursor
    # humanization for bot-gated targets. Scoped opt-in only — the global
    # default stays False (browser-side trajectory wedged input, 0.3.10).
    humanize: bool | float = False
    raw = _os.environ.get("KAHIN_HUMANIZE", "").strip().lower()
    if raw in {"1", "true", "yes"}:
        humanize = True
    elif raw:
        try:
            humanize = max(0.5, min(5.0, float(raw)))
        except ValueError:
            humanize = False
    return {
        "headless": bool(headless),
        "humanize": humanize,
        "enable_cache": True,
        "block_webgl": False,
        "main_world_eval": False,
    }


# Identity rotation policy (Faz 3 Task 4): a bounded, validated pin store
# mapping canonical domains to saved Faz 2 identity names. The store is
# plain JSON under the user config dir, exactly like the identity files;
# every read and write is bounded (entry count, name length, file size)
# and validated (canonical DNS keys only), so a tampered file can never
# smuggle in an arbitrary key or an oversized value.
_DOMAIN_RE = re.compile(
    r"^(localhost|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+)$"
)
_PINS_FILE = Path.home() / ".config" / "kahin" / "pins.json"
_PINS_MAX_ENTRIES = 10_000
_PINS_MAX_BYTES = 1024 * 1024
_PINS_KEY_MAX = 253
_PIN_NAME_MAX = 64


def normalize_domain(domain: str) -> str | None:
    """Strip scheme/path/port, lowercase, and validate the hostname shape.

    Accepts ``localhost`` and dotted DNS names (single-char labels allowed,
    no empty labels, no leading/trailing hyphens, at least two labels for
    non-localhost). Anything with spaces, userinfo, underscores, an IPv6
    literal or a missing TLD is rejected, so a stored key can never carry
    injection payloads.
    """
    text = (domain or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0]
    if not text or len(text) > _PINS_KEY_MAX:
        return None
    if not _DOMAIN_RE.match(text):
        return None
    return text


def pins_path() -> Path:
    """The canonical pin store location (~/.config/kahin/pins.json)."""
    return _PINS_FILE


def load_pins() -> dict[str, str]:
    """Load the pinned domain → identity map, validated and bounded.

    A missing, unreadable, oversized or malformed store degrades to an
    empty map; a tampered store cannot smuggle in non-domain keys or
    oversized names.
    """
    try:
        if not _PINS_FILE.is_file():
            return {}
        if _PINS_FILE.stat().st_size > _PINS_MAX_BYTES:
            return {}
        payload = orjson.loads(_PINS_FILE.read_text(encoding="utf-8"))
    except (OSError, orjson.JSONDecodeError):
        return {}
    pins = payload.get("pins") if isinstance(payload, dict) else None
    if not isinstance(pins, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in pins.items():
        if len(result) >= _PINS_MAX_ENTRIES:
            break
        normalized = normalize_domain(str(key))
        if normalized is None:
            continue
        if not isinstance(value, str) or not value or len(value) > _PIN_NAME_MAX:
            continue
        result[normalized] = value
    return result


def save_pins(pins: dict[str, str]) -> None:
    """Persist the pinned map as ``{"version": 1, "pins": {...}}``.

    Only canonical domain keys with bounded string values are written;
    invalid entries are dropped so the file on disk always round-trips
    through ``load_pins`` unchanged. Raises ``OSError`` on write failure.
    """
    if not isinstance(pins, dict):
        raise ValueError("pins must be a dict")
    clean: dict[str, str] = {}
    for key, value in pins.items():
        if len(clean) >= _PINS_MAX_ENTRIES:
            break
        normalized = normalize_domain(str(key))
        if normalized is None:
            continue
        if not isinstance(value, str) or not value or len(value) > _PIN_NAME_MAX:
            continue
        clean[normalized] = value
    _PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PINS_FILE.write_text(
        orjson.dumps({"version": 1, "pins": clean}, option=orjson.OPT_INDENT_2).decode(),
        encoding="utf-8",
    )


def pin_identity(domain: str, name: str) -> dict[str, str] | None:
    """Pin ``name`` to a canonical ``domain``.

    Returns None on success or ``{"error", "code": "invalid_argument"}``
    when the domain cannot be normalized. The caller is responsible for
    proving the identity exists (the tool layer checks the Faz 2 store).
    """
    normalized = normalize_domain(domain)
    if normalized is None:
        return {"error": "invalid domain", "code": "invalid_argument"}
    if not isinstance(name, str) or not name or len(name) > _PIN_NAME_MAX:
        return {"error": "invalid identity name", "code": "invalid_argument"}
    pins = load_pins()
    pins[normalized] = name
    save_pins(pins)
    return None


def unpin_identity(domain: str) -> dict[str, str] | None:
    """Remove any pin for a canonical ``domain`` (idempotent).

    Returns None on success or ``{"error", "code": "invalid_argument"}``
    when the domain cannot be normalized.
    """
    normalized = normalize_domain(domain)
    if normalized is None:
        return {"error": "invalid domain", "code": "invalid_argument"}
    pins = load_pins()
    pins.pop(normalized, None)
    save_pins(pins)
    return None


# Proxy/geo sync (Faz 3 Task 5): the browser layer applies a proxy through
# environment variables (Camoufox passes our env through to the Firefox
# child verbatim), and geo resolution runs THROUGH the proxy so the exit IP
# is what actually gets probed. Credentials live only inside the env dict
# that the browser consumes; every display surface uses ``_redact_proxy``.
_PROXY_SCHEMES = ("http", "https", "socks4", "socks5")
_PROXY_GEO_ENDPOINT = "https://ipapi.co/json/"
_PROXY_GEO_MAX_BYTES = 64 * 1024


def _redact_proxy(proxy_url: str) -> str:
    """Strip userinfo (credentials) from a proxy URL for display/logging."""
    try:
        parsed = urlparse(proxy_url)
        if parsed.scheme and parsed.hostname:
            port = parsed.port
            netloc = f"{parsed.hostname}:{port}" if port else parsed.hostname
            return f"{parsed.scheme}://{netloc}"
    except ValueError:
        pass
    return "<proxy>"


def proxy_env(proxy_url: str) -> dict[str, str]:
    """Map a single proxy URL to the env variables Camoufox/Firefox respect.

    Validates the URL strictly (scheme http/https/socks4/socks5, hostname
    required, numeric port, no whitespace/control characters) and raises
    ``ValueError`` otherwise. The message never embeds credentials.
    ``NO_PROXY`` keeps loopback traffic out of the proxy so local calls
    (and the sidecar itself) are never routed through it.
    """
    raw = (proxy_url or "").strip()
    if not raw:
        raise ValueError("proxy URL must not be empty")
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise ValueError("proxy URL must not contain whitespace or control characters")
    try:
        parsed = urlparse(raw)
        parsed.port  # validates numeric port range
    except ValueError:
        raise ValueError("proxy URL has an invalid port") from None
    if parsed.scheme not in _PROXY_SCHEMES:
        raise ValueError(
            f"unsupported proxy scheme {parsed.scheme!r}; use http, https, socks4 or socks5"
        )
    if not parsed.hostname:
        raise ValueError(f"proxy URL is missing a host: {_redact_proxy(raw)!r}")
    return {
        "HTTPS_PROXY": raw,
        "HTTP_PROXY": raw,
        "ALL_PROXY": raw,
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }


def _bounded_geo_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:maximum]


def resolve_proxy_geo(proxy_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """Resolve the proxy's exit IP geo by fetching ipapi.co THROUGH it.

    Returns ``{ip, timezone, country_code, country_name, city, latitude,
    longitude}`` on success; every failure returns a structured
    ``{"error", "code": "proxy_resolve_failed", "proxy": <redacted>}``
    payload. SOCKS proxies need the optional ``socksio`` package
    (``httpx[socks]``); without it the failure is deterministic and never
    touches the network. Error text never includes proxy credentials.
    """
    redacted = _redact_proxy(proxy_url)
    try:
        proxy_env(proxy_url)
    except ValueError as exc:
        return {"error": str(exc), "code": "proxy_resolve_failed", "proxy": redacted}
    scheme = urlparse(proxy_url).scheme
    if scheme in ("socks4", "socks5") and importlib.util.find_spec("socksio") is None:
        return {
            "error": (
                "SOCKS proxy geo resolution requires the optional socksio package "
                "(pip install 'httpx[socks]'); use an http/https proxy or install socksio"
            ),
            "code": "proxy_resolve_failed",
            "capability": "socks_unsupported",
            "proxy": redacted,
        }
    try:
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            response = client.get(_PROXY_GEO_ENDPOINT)
            response.raise_for_status()
            if len(response.content) > _PROXY_GEO_MAX_BYTES:
                return {
                    "error": "proxy geo resolution returned an oversized payload",
                    "code": "proxy_resolve_failed",
                    "proxy": redacted,
                }
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - structured failure contract
        return {
            "error": f"proxy geo resolution failed ({type(exc).__name__})",
            "code": "proxy_resolve_failed",
            "proxy": redacted,
        }
    if not isinstance(payload, dict):
        return {
            "error": "proxy geo resolution returned a non-object payload",
            "code": "proxy_resolve_failed",
            "proxy": redacted,
        }
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    return {
        "ip": _bounded_geo_text(payload.get("ip"), 64),
        "timezone": _bounded_geo_text(payload.get("timezone"), 128),
        "country_code": _bounded_geo_text(payload.get("country_code"), 8),
        "country_name": _bounded_geo_text(payload.get("country_name"), 128),
        "city": _bounded_geo_text(payload.get("city"), 128),
        "latitude": latitude if isinstance(latitude, (int, float)) else None,
        "longitude": longitude if isinstance(longitude, (int, float)) else None,
    }
