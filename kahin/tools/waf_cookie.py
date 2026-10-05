"""waf_cookie.py — Baidu ADAS `nox_jst_v1` cookie tools (engine-agnostic).

A WAF-protected site answers an HTML challenge instead of the page until the
client carries the site's own `nox_jst_v1` signature. This tool mints that
cookie on a QuickJS runtime and publishes it as a file, so an HTTP scraper reads
a file instead of waiting on a browser. It does not open or drive a browser.

A separate store keeps running in the MCP process; `kahin_waf_cookie_status`
reports its state and the cookie file path a scraper should read.
"""

from __future__ import annotations

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _RW
from kahin.waf_cookie import NoxCookieError, NoxCookieStore

_STORE: NoxCookieStore | None = None

_ORIGIN_MIN = 8
_ORIGIN_MAX = 200
_PATH_MAX = 200


def _bad(code: str, message: str, **extra: object) -> str:
    return orjson.dumps(
        {"error": message, "code": code, **extra}, option=orjson.OPT_INDENT_2
    ).decode()


def _store(origin: str, probe_path: str) -> NoxCookieStore:
    """Return the process-wide store, recreating it when the origin changes."""
    global _STORE
    if _STORE is not None and _STORE.origin == origin.rstrip("/") and _STORE.probe_path == probe_path:
        return _STORE
    if _STORE is not None:
        _STORE.stop()
    _STORE = NoxCookieStore(origin, probe_path=probe_path)
    return _STORE


def _mint(origin: str, probe_path: str, force: bool) -> str:
    if not isinstance(origin, str) or not (_ORIGIN_MIN <= len(origin.strip()) <= _ORIGIN_MAX):
        return _bad("invalid_argument", f"origin must be {_ORIGIN_MIN}-{_ORIGIN_MAX} characters")
    if not isinstance(probe_path, str) or not probe_path.strip() or len(probe_path) > _PATH_MAX:
        return _bad("invalid_argument", f"probe_path must be 1-{_PATH_MAX} characters")
    cleaned = origin.strip().rstrip("/")
    path = probe_path.strip()
    try:
        store = _store(cleaned, path if path.startswith("/") else f"/{path}")
    except ValueError as exc:
        return _bad("invalid_argument", str(exc))
    try:
        store.refresh(force=force)
    except NoxCookieError as exc:
        return _bad("nox_cookie_unavailable", str(exc), origin=store.origin)
    return orjson.dumps(store.status(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_waf_cookie_mint", annotations=_RW)
async def waf_cookie_mint(
    origin: str = "https://gitee.com",
    probe_path: str = "/explore",
    force: bool = False,
) -> str:
    """Mint a fresh Baidu ADAS `nox_jst_v1` cookie for a WAF-protected site.

    The cookie is produced by running the site's own nox JavaScript on a QuickJS
    runtime (about 10 MB RSS, about 9 ms per cookie), then written to a state file
    so an HTTP scraper can read it. No browser is opened. Use ``force=true`` to
    re-mint even when the held cookie is still inside its refresh interval;
    otherwise the interval (two thirds of the TTL the WAF declares) is honoured.
    """
    return _mint(origin, probe_path, force)


@mcp.tool(name="kahin_waf_cookie_status", annotations=_RO)
async def waf_cookie_status() -> str:
    """Report the nox cookie store: held cookie, age, TTL, refresh interval, file path.

    Read-only. Returns `held: false` when no cookie has been minted yet.
    """
    if _STORE is None:
        return _bad(
            "no_store",
            "no nox cookie store is active; mint one with kahin_waf_cookie_mint",
        )
    return orjson.dumps(_STORE.status(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_waf_cookie_header", annotations=_RO)
async def waf_cookie_header() -> str:
    """Return the current `nox_jst_v1` cookie as a `Cookie:` header value.

    Read-only. Returns an empty value when no cookie is held, so a scraper can
    send the result unconditionally.
    """
    if _STORE is None or not _STORE.cookie_header():
        return ""
    return _STORE.cookie_header()