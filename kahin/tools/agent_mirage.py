"""agent_mirage.py — Agent-native observation tools (Faz 2, Tasks 2-4).

``kahin_mirage_snapshot`` wraps the live DOM snapshot (nodeId-carrying tree)
into the compact ref-carrying line format agents act on, with explicit token
accounting. Every emitted ref is a live DOM nodeId valid for
``kahin_mirage_dom_action`` until the document/frame lifecycle ends; after a
navigation or reset the agent must request a fresh snapshot before acting.
``kahin_mirage_fill_form`` types several live fields by ref in one call.
``kahin_mirage_state_save``/``kahin_mirage_state_load`` persist a session
(cookies, localStorage, sessionStorage, current url) to an absolute-path
JSON file and restore it — storage is origin-bound, so load navigates back
to the saved url before restoring anything.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import orjson

from kahin import window_inventory
from kahin._mcp import mcp
from kahin.agent_snapshot import format_snapshot
from kahin.dom_stream import DOM_STREAM_GLOBAL
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_eval_result,
    _mirage_evaluate,
    _network_response_payload,
    _require_engine,
)
from kahin.tools.dom_stream_mirage import (
    _dom_stream_record,
    _dom_stream_status,
    mirage_dom_action,
    mirage_dom_snapshot,
)
from kahin.tools.pilot_mirage import (
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _bounded_float,
    _bounded_int,
    _capture_page_session,
    _json_error,
    _text_arg,
)

_MAX_TOKEN_BUDGET = 100_000
_FILL_FIELDS_MAX = 100
_MAX_STATE_PATH_LENGTH = 4_096
_MAX_STATE_PAYLOAD = 16 * 1024 * 1024
_STATE_STORAGE_KEY_MAX = 1_024
_STATE_STORAGE_VALUE_MAX = 1024 * 1024
_STATE_URL_MAX = 4_096
_MAX_IDENTITY_PAYLOAD = 16 * 1024 * 1024

_CHALLENGE_STATUS_JS = r"""
(() => {
  const body = String(document.body?.innerText || "").slice(0, 30000).toLowerCase();
  const title = String(document.title || "").slice(0, 500).toLowerCase();
  const url = String(location.href || "").slice(0, 4096).toLowerCase();
  const signals = [];
  const has = (name, pattern, source) => {
    if (pattern.test(source)) signals.push(name);
  };
  has("captcha-text", /captcha|recaptcha|hcaptcha|turnstile|verify you are human|cloudflare.*challenge/, body + " " + title);
  has("captcha-url", /captcha|challenge-platform|challenges\.cloudflare|turnstile/, url);
  has("captcha-element", /iframe|textarea|div/, Array.from(document.querySelectorAll(
    "iframe[src*='captcha'], iframe[src*='challenge'], [id*='captcha'], [class*='captcha'], " +
    "[id*='challenge'], [class*='challenge'], textarea[name*='captcha'], " +
    "iframe[src*='turnstile'], input[name*='turnstile'], [name*='cf-turnstile'], " +
    "[id*='cf-chl'], [class*='cf-turnstile']"
  )).map((el) => String(el.outerHTML || "").slice(0, 500)).join(" ").toLowerCase());
  has("rate-limit-text", /too many requests|rate limit|slow down|try again later|temporarily blocked/, body + " " + title);
  has("rate-limit-status", /(?:status(?:\s+code)?|http\s+error|error\s+code|response\s+code)\s*:?\s*(?:429|503)|(?:429|503)\s+(?:too many|service unavailable)/, body + " " + title);
  has("access-denied-text", /access denied|forbidden|request blocked|automated queries/, body + " " + title);
  const captcha = signals.some((item) => item.startsWith("captcha"));
  const rateLimit = signals.some((item) => item.startsWith("rate-limit"));
  const denied = signals.some((item) => item.startsWith("access-denied"));
  const kind = captcha ? "captcha" : rateLimit ? "rate_limit" : denied ? "access_denied" : null;
  return {
    detected: Boolean(kind),
    kind,
    signals: signals.slice(0, 20),
    action: captcha ? "pause_for_human_or_authorized_provider" :
      rateLimit ? "honor_retry_after_and_backoff" :
      denied ? "stop_and_review_authorization" : "continue",
    retryable: rateLimit && !captcha && !denied,
    url: String(location.href || "").slice(0, 4096),
    title: String(document.title || "").slice(0, 500),
  };
})()
"""


def _retry_after_seconds(headers: Any) -> float | None:
    """Parse Retry-After from Juggler's list or CDP's object headers."""
    candidates: list[Any] = []
    if isinstance(headers, dict):
        candidates.extend(
            value for name, value in headers.items()
            if isinstance(name, str) and name.lower() == "retry-after"
        )
    elif isinstance(headers, list):
        candidates.extend(
            entry.get("value")
            for entry in headers
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"].lower() == "retry-after"
        )
    for candidate in candidates:
        try:
            parsed = float(candidate)
        except (TypeError, ValueError):
            try:
                deadline = parsedate_to_datetime(str(candidate))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                parsed = (deadline - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if 0 <= parsed <= 86_400:
            return parsed
    return None

# Identity persistence (Faz 2 Task 5): fingerprints live in the user config
# dir, keyed by a strict name so a saved name can never traverse out of the
# directory. Names are validated identically by every identity tool and by
# ``browser_start(identity=...)`` resolution.
_IDENTITY_DIR = Path.home() / ".config" / "kahin" / "identities"
_IDENTITY_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_IDENTITY_OS_TARGETS = ("windows", "macos", "linux", "random")


def _identity_path(name: str) -> Path | None:
    """Resolve a saved identity name to its JSON file, or None when the
    name is not a safe identifier (no traversal, no weird characters)."""
    if not _IDENTITY_NAME_RE.match(name):
        return None
    return _IDENTITY_DIR / f"{name}.json"


def _identity_summary(config: dict[str, Any]) -> dict[str, Any]:
    """The fingerprint fields agents care about, pulled from a raw config."""
    summary: dict[str, Any] = {}
    for key in (
        "navigator.userAgent",
        "screen.width",
        "screen.height",
        "timezone",
        "locale",
        "webGl:renderer",
    ):
        value = config.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "":
            summary[key] = value
    return summary


def _loads(text: str) -> Any:
    return orjson.loads(text)


def _state_absolute_path(value: str, tool: str, field: str) -> tuple[Path | None, str | None]:
    """Expand ``~`` and require an absolute path (upload_files security rule)."""
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        return None, _json_error(
            tool,
            f"{field} must be an absolute path (or ~-prefixed)",
            "invalid_argument",
            field=field,
        )
    return expanded, None


async def _storage_snapshot(
    tool: str,
    storage_name: str,
    session_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Read one web-storage object pinned to the captured page session.

    ``kahin_mirage_storage_*_get`` returns a list of ``{key, value}``
    entries; here that is normalized into the ``{key: value}`` object the
    state file stores, so restore can write every key back.
    """
    from kahin.tools.storage_mirage import (  # noqa: PLC0415
        _MAX_STORAGE_ENTRIES,
        _storage_expression,
    )

    try:
        raw = _loads(await _mirage_evaluate(
            _storage_expression(storage_name, _MAX_STORAGE_ENTRIES),
            session_id=session_id,
        ))
    except orjson.JSONDecodeError:
        return None, _json_error(tool, "storage read returned invalid JSON", "invalid_engine_response")
    if isinstance(raw, dict) and raw.get("error"):
        return None, orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode()
    if not isinstance(raw, list):
        return None, _json_error(tool, "storage read returned an invalid payload", "invalid_engine_response")
    normalized: dict[str, str] = {}
    for entry in raw:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("key"), str)
            and isinstance(entry.get("value"), str)
        ):
            return None, _json_error(tool, "storage read returned a malformed entry", "invalid_engine_response")
        normalized[entry["key"]] = entry["value"]
    return normalized, None


def _validate_loaded_storage(
    value: Any,
    tool: str,
    field: str,
    path: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Validate one restored web-storage object from a state file."""
    if not isinstance(value, dict):
        return None, _json_error(
            tool,
            f"state file {field} must be an object",
            "invalid_argument",
            path=path,
            field=field,
        )
    checked: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > _STATE_STORAGE_KEY_MAX:
            return None, _json_error(
                tool,
                f"state file {field} has an out-of-bounds key",
                "invalid_argument",
                path=path,
                field=field,
            )
        if not isinstance(item, str) or len(item) > _STATE_STORAGE_VALUE_MAX:
            return None, _json_error(
                tool,
                f"state file {field} has an out-of-bounds value",
                "invalid_argument",
                path=path,
                field=field,
            )
        checked[key] = item
    return checked, None


def _normalize_loaded_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt restored cookies to Browser.setCookies' real behavior.

    The Juggler harness silently drops cookies whose ``expires`` is a
    non-positive number (the representation ``Browser.getCookies`` uses for
    session cookies). Dropping that field restores them as session cookies;
    genuine future expirations are kept intact.
    """
    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        entry = dict(cookie)
        expires = entry.get("expires")
        if (
            isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and expires <= 0
        ):
            entry.pop("expires", None)
        normalized.append(entry)
    return normalized


async def _restore_storage(
    tool: str,
    storage_name: str,
    entries: dict[str, str],
    session_id: str,
) -> str | None:
    """Write every storage entry back, pinned to the loaded page session."""
    for key, value in entries.items():
        k = orjson.dumps(key).decode()
        v = orjson.dumps(value).decode()
        expression = f"(() => {{ {storage_name}.setItem({k}, {v}); return 'set'; }})()"
        outcome = _loads(await _mirage_evaluate(expression, session_id=session_id))
        if isinstance(outcome, dict) and outcome.get("error"):
            return orjson.dumps(outcome, option=orjson.OPT_INDENT_2).decode()
        if outcome != "set":
            return _json_error(tool, f"{storage_name} write returned an invalid payload", "invalid_engine_response")
    return None

@mcp.tool(name="kahin_mirage_fill_form", annotations=_RW)
async def mirage_fill_form(
    fields: list[dict[str, Any]],
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: fill several live fields in one call.

    ``fields`` is a list of ``{ref, text}`` objects; each ``ref`` must be a
    live nodeId from ``kahin_mirage_snapshot``/``kahin_mirage_dom_snapshot``.
    Every field is typed through ``kahin_mirage_dom_action(action="type")`` so
    no stale selector is ever trusted. Returns ``{filled, results[]}`` where
    each result is the corresponding dom_action payload; a field that went
    stale returns ``requiresSnapshot: true`` and never pretends the form
    completed.
    """
    if not isinstance(fields, list) or not fields or len(fields) > _FILL_FIELDS_MAX:
        return _json_error(
            "kahin_mirage_fill_form",
            f"fields must be a list of 1..{_FILL_FIELDS_MAX} items",
            "invalid_argument",
            field="fields",
        )
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe(
        "kahin_mirage_fill_form", count=len(fields), timeout=timeout_value, frame_id=frame_id,
    ):
        results: list[dict[str, Any]] = []
        for field in fields:
            if not isinstance(field, dict):
                return _json_error(
                    "kahin_mirage_fill_form",
                    "each field must be an object {ref, text}",
                    "invalid_argument",
                )
            ref = field.get("ref")
            text = field.get("text")
            if not isinstance(ref, str) or not isinstance(text, str):
                return _json_error(
                    "kahin_mirage_fill_form",
                    "each field needs a string ref and a string text",
                    "invalid_argument",
                )
            outcome = _loads(await mirage_dom_action(node_id=ref, action="type", text=text, frame_id=frame_id))
            results.append(outcome)
            if not isinstance(outcome, dict):
                return _json_error(
                    "kahin_mirage_fill_form",
                    "a field action returned an invalid payload",
                    "invalid_engine_response",
                    filled=len(results) - 1,
                )
            if outcome.get("requiresSnapshot"):
                return orjson.dumps({
                    "error": "a field went stale mid-form",
                    "code": "stale_node",
                    "requiresSnapshot": True,
                    "filled": len(results) - 1,
                    "results": results,
                }, option=orjson.OPT_INDENT_2).decode()
            if outcome.get("error"):
                return _json_error(
                    "kahin_mirage_fill_form",
                    str(outcome["error"]),
                    str(outcome.get("code") or "tool_error"),
                    filled=len(results) - 1,
                )
        return orjson.dumps({"filled": len(results), "results": results}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_snapshot", annotations=_RO)
async def mirage_snapshot(
    selector: str | None = None,
    max_tokens: int = 1500,
    include_hidden: bool = False,
    frame_id: str | None = None,
) -> str:
    """Mirage: agent-ready page observation with live DOM refs.

    Returns compact lines in the form ``- button "Add" [ref=n9] [click]``.
    Every ``ref`` is a live DOM nodeId that ``kahin_mirage_dom_action`` can
    act on without re-resolving a selector. ``max_tokens`` caps the output;
    ``tokens_estimate``/``truncated`` are always reported so truncation is
    never silent. Snapshot metadata (``streamId``/``cursor``/``revision``/
    ``url``/``title``/``readyState``/``focused``) is passed through when the
    live DOM stream provides it.
    """
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= _MAX_TOKEN_BUDGET
    ):
        return _json_error(
            "kahin_mirage_snapshot",
            f"max_tokens must be an integer in 1..{_MAX_TOKEN_BUDGET}",
            "invalid_argument",
            field="max_tokens",
        )
    selector_value: str | None = None
    if selector is not None:
        selector_value, error = _text_arg(
            selector,
            tool="kahin_mirage_snapshot",
            field="selector",
            maximum=_MAX_SELECTOR_LENGTH,
        )
        if error:
            return error
    async with _healer_ref.safe(
        "kahin_mirage_snapshot",
        selector=selector_value[:80] if selector_value else None,
        max_tokens=max_tokens,
        include_hidden=include_hidden,
        frame_id=frame_id,
    ):
        raw_text = await mirage_dom_snapshot(
            selector=selector_value,
            max_nodes=_bounded_int(2000, minimum=1, maximum=5000, default=2000),
            max_depth=_bounded_int(24, minimum=1, maximum=32, default=24),
            include_hidden=include_hidden,
            frame_id=frame_id,
        )
        raw = _loads(raw_text)
        if not isinstance(raw, dict):
            return _json_error(
                "kahin_mirage_snapshot",
                "DOM snapshot returned an invalid payload",
                "invalid_engine_response",
            )
        if raw.get("error"):
            return orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode()
        tree = raw.get("root") or {}
        if not isinstance(tree, dict):
            return _json_error(
                "kahin_mirage_snapshot",
                "DOM snapshot returned no root tree",
                "invalid_engine_response",
            )
        formatted = format_snapshot(tree, max_tokens=max_tokens)
        payload: dict[str, Any] = dict(formatted)
        for key in ("streamId", "cursor", "revision", "url", "title", "readyState", "focused"):
            if key in raw:
                payload[key] = raw[key]
        payload["include_hidden"] = bool(include_hidden)
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_state_save", annotations=_RO)
async def mirage_state_save(path: str) -> str:
    """Mirage: persist the current session to an absolute-path JSON file.

    Captures the current url plus every cookie and every local/session
    storage entry of the active page (values are bounded by the storage
    layer), writes ``{version, url, cookies, localStorage, sessionStorage}``
    and reports per-section counts. Restore with kahin_mirage_state_load.
    """
    tool = "kahin_mirage_state_save"
    path_value, error = _text_arg(
        path, tool=tool, field="path", maximum=_MAX_STATE_PATH_LENGTH,
    )
    if error:
        return error
    assert path_value is not None
    target, error = _state_absolute_path(path_value, tool, "path")
    if error:
        return error
    assert target is not None
    async with _healer_ref.safe(tool, path=str(target)):
        from kahin.tools import storage_mirage  # noqa: PLC0415

        session_id, capture_error = await _capture_page_session(tool)
        if capture_error:
            return capture_error

        cookies_raw = _loads(await storage_mirage.mirage_cookie_get())
        if isinstance(cookies_raw, dict) and cookies_raw.get("error"):
            return orjson.dumps(cookies_raw, option=orjson.OPT_INDENT_2).decode()
        cookies = (
            cookies_raw.get("cookies")
            if isinstance(cookies_raw, dict) and isinstance(cookies_raw.get("cookies"), list)
            else []
        )

        local, storage_error = await _storage_snapshot(tool, "localStorage", session_id)
        if storage_error:
            return storage_error
        session, storage_error = await _storage_snapshot(tool, "sessionStorage", session_id)
        if storage_error:
            return storage_error
        assert local is not None and session is not None
        for storage_name, entries in (("localStorage", local), ("sessionStorage", session)):
            for key in entries:
                if len(key) > _STATE_STORAGE_KEY_MAX:
                    return _json_error(
                        tool,
                        f"{storage_name} key exceeds the persistence bound",
                        "invalid_argument",
                        field=storage_name,
                        maximum=_STATE_STORAGE_KEY_MAX,
                    )

        url = ""
        try:
            url_raw = _loads(await _mirage_evaluate("location.href", session_id=session_id))
        except orjson.JSONDecodeError:
            url_raw = None
        if isinstance(url_raw, str):
            url = url_raw[:_STATE_URL_MAX]

        payload = {
            "version": 1,
            "url": url,
            "cookies": cookies,
            "localStorage": local,
            "sessionStorage": session,
        }
        try:
            serialized = orjson.dumps(payload, option=orjson.OPT_INDENT_2)
        except (TypeError, ValueError) as exc:
            return _json_error(tool, f"cannot serialize session state: {exc}", "tool_failed")
        if len(serialized) > _MAX_STATE_PAYLOAD:
            return _json_error(
                tool,
                "session state exceeds the persistence payload bound",
                "invalid_argument",
                maximum=_MAX_STATE_PAYLOAD,
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(serialized)
        except OSError as exc:
            return _json_error(tool, f"cannot write state file: {exc}", "tool_failed", path=str(target))
        return orjson.dumps({
            "saved": True,
            "path": str(target),
            "cookies": len(cookies),
            "localStorage": len(local),
            "sessionStorage": len(session),
            "url": url,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_state_load", annotations=_RW)
async def mirage_state_load(path: str) -> str:
    """Mirage: restore a session saved by kahin_mirage_state_save.

    Navigates to the saved url first (storage is origin-bound), then
    restores cookies, localStorage and sessionStorage against that origin.
    """
    tool = "kahin_mirage_state_load"
    path_value, error = _text_arg(
        path, tool=tool, field="path", maximum=_MAX_STATE_PATH_LENGTH,
    )
    if error:
        return error
    assert path_value is not None
    target, error = _state_absolute_path(path_value, tool, "path")
    if error:
        return error
    assert target is not None
    async with _healer_ref.safe(tool, path=str(target)):
        try:
            data = target.read_bytes()
        except OSError as exc:
            return _json_error(tool, f"cannot read state file: {exc}", "tool_failed", path=str(target))
        if len(data) > _MAX_STATE_PAYLOAD:
            return _json_error(
                tool,
                "state file exceeds the read payload bound",
                "invalid_argument",
                path=str(target),
                maximum=_MAX_STATE_PAYLOAD,
            )
        try:
            payload = orjson.loads(data)
        except orjson.JSONDecodeError as exc:
            return _json_error(
                tool,
                f"state file is not valid JSON: {exc}",
                "invalid_argument",
                path=str(target),
            )
        if not isinstance(payload, dict):
            return _json_error(
                tool,
                "state file must contain a JSON object",
                "invalid_argument",
                path=str(target),
            )
        version = payload.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            return _json_error(
                tool,
                "unsupported state file version",
                "invalid_argument",
                path=str(target),
                version=version,
            )
        url = payload.get("url", "")
        if not isinstance(url, str) or len(url) > _STATE_URL_MAX:
            return _json_error(
                tool,
                "state file url must be a bounded string",
                "invalid_argument",
                path=str(target),
                field="url",
            )
        cookies = payload.get("cookies", [])
        if not isinstance(cookies, list):
            return _json_error(
                tool,
                "state file cookies must be a list",
                "invalid_argument",
                path=str(target),
                field="cookies",
            )
        local, storage_error = _validate_loaded_storage(
            payload.get("localStorage", {}), tool, "localStorage", str(target),
        )
        if storage_error:
            return storage_error
        session, storage_error = _validate_loaded_storage(
            payload.get("sessionStorage", {}), tool, "sessionStorage", str(target),
        )
        if storage_error:
            return storage_error
        assert local is not None and session is not None

        from kahin.tools import pilot, storage_mirage  # noqa: PLC0415

        if url:
            navigation = _loads(await pilot.navigate(url=url))
            if isinstance(navigation, dict) and navigation.get("error"):
                return orjson.dumps(navigation, option=orjson.OPT_INDENT_2).decode()
        session_id, capture_error = await _capture_page_session(tool)
        if capture_error:
            return capture_error

        if cookies:
            outcome = _loads(await storage_mirage.mirage_cookie_set(
                cookies=_normalize_loaded_cookies(cookies),
            ))
            if isinstance(outcome, dict) and outcome.get("error"):
                return orjson.dumps(outcome, option=orjson.OPT_INDENT_2).decode()
        restore_error = await _restore_storage(tool, "localStorage", local, session_id)
        if restore_error:
            return restore_error
        restore_error = await _restore_storage(tool, "sessionStorage", session, session_id)
        if restore_error:
            return restore_error

        return orjson.dumps({
            "loaded": True,
            "path": str(target),
            "url": url,
            "cookies": len(cookies),
            "localStorage": len(local),
            "sessionStorage": len(session),
        }, option=orjson.OPT_INDENT_2).decode()


# --- Identity persistence (Faz 2 Task 5) -----------------------------------


def _identity_name_arg(tool: str, name: Any) -> tuple[str | None, str | None, Path | None]:
    """Validate an identity name and resolve its file path."""
    name_value, error = _text_arg(name, tool=tool, field="name", maximum=64)
    if error:
        return None, error, None
    assert name_value is not None
    path = _identity_path(name_value)
    if path is None:
        return None, _json_error(
            tool,
            "name must match ^[a-zA-Z0-9_-]{1,64}$",
            "invalid_argument",
            field="name",
        ), None
    return name_value, None, path


def _write_identity(tool: str, path: Path, config: dict[str, Any]) -> str | None:
    """Persist an identity file with bounded payload; returns an error JSON
    string on failure, None on success."""
    try:
        serialized = orjson.dumps(
            {"version": 1, "config": config}, option=orjson.OPT_INDENT_2,
        )
    except (TypeError, ValueError) as exc:
        return _json_error(tool, f"cannot serialize identity: {exc}", "tool_failed")
    if len(serialized) > _MAX_IDENTITY_PAYLOAD:
        return _json_error(
            tool,
            "identity config exceeds the persistence payload bound",
            "invalid_argument",
            maximum=_MAX_IDENTITY_PAYLOAD,
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(serialized)
    except OSError as exc:
        return _json_error(tool, f"cannot write identity: {exc}", "tool_failed", path=str(path))
    return None


@mcp.tool(name="kahin_identity_new", annotations=_RO)
async def identity_new(name: str, os_target: str = "random") -> str:
    """Create and persist a fresh fingerprint identity.

    Uses the installed real Camoufox fingerprint generator (BrowserForge
    synthetic identity). ``os_target``: windows|macos|linux|random. Returns
    ``{saved, name, path, summary}`` where summary carries the pinned
    navigator.userAgent, screen and webgl values for later verification.
    """
    tool = "kahin_identity_new"
    name_value, error, path = _identity_name_arg(tool, name)
    if error:
        return error
    assert name_value is not None and path is not None
    if not isinstance(os_target, str) or os_target not in _IDENTITY_OS_TARGETS:
        return _json_error(
            tool,
            "os_target must be windows|macos|linux|random",
            "invalid_argument",
            field="os_target",
        )
    async with _healer_ref.safe(tool, name=name_value, os_target=os_target):
        try:
            from camoufox.fingerprints import generate_context_fingerprint  # noqa: PLC0415
        except ImportError:
            return _json_error(
                tool,
                "camoufox.fingerprints unavailable",
                "capability_requires_mirage",
            )
        os_filter = None if os_target == "random" else os_target
        try:
            generated = generate_context_fingerprint(os=os_filter)
        except Exception as exc:  # noqa: BLE001 - public tool returns JSON
            return _json_error(tool, f"fingerprint generation failed: {exc}", "tool_failed")
        config = generated.get("config") or {}
        if not isinstance(config, dict) or not config:
            return _json_error(tool, "fingerprint generator returned no config", "tool_failed")
        write_error = _write_identity(tool, path, config)
        if write_error:
            return write_error
        return orjson.dumps({
            "saved": True,
            "name": name_value,
            "path": str(path),
            "summary": _identity_summary(config),
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_save", annotations=_RO)
async def identity_save(name: str, config: dict[str, Any]) -> str:
    """Persist a raw fingerprint config dict under a name.

    The config is a Camoufox ``launch_options(config=...)`` dict (keys like
    ``navigator.userAgent``, ``screen.width``); it is applied verbatim when
    ``kahin_browser_start(identity=<name>)`` launches the engine.
    """
    tool = "kahin_identity_save"
    name_value, error, path = _identity_name_arg(tool, name)
    if error:
        return error
    assert name_value is not None and path is not None
    if not isinstance(config, dict) or not config:
        return _json_error(
            tool,
            "config must be a non-empty object",
            "invalid_argument",
            field="config",
        )
    async with _healer_ref.safe(tool, name=name_value):
        write_error = _write_identity(tool, path, config)
        if write_error:
            return write_error
        return orjson.dumps({
            "saved": True,
            "name": name_value,
            "path": str(path),
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_list", annotations=_RO)
async def identity_list() -> str:
    """List saved identities with fingerprint summaries."""
    async with _healer_ref.safe("kahin_identity_list"):
        identities: list[dict[str, Any]] = []
        if _IDENTITY_DIR.is_dir():
            for file in sorted(_IDENTITY_DIR.glob("*.json")):
                try:
                    payload = orjson.loads(file.read_bytes())
                except (OSError, orjson.JSONDecodeError):
                    continue
                config = payload.get("config") if isinstance(payload, dict) else None
                if not isinstance(config, dict):
                    continue
                identities.append({
                    "name": file.stem,
                    "summary": _identity_summary(config),
                })
        return orjson.dumps({"identities": identities}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_delete", annotations=_DW)
async def identity_delete(name: str) -> str:
    """Delete a saved identity file."""
    tool = "kahin_identity_delete"
    name_value, error, path = _identity_name_arg(tool, name)
    if error:
        return error
    assert name_value is not None and path is not None
    async with _healer_ref.safe(tool, name=name_value):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            return _json_error(
                tool,
                f"cannot delete identity: {exc}",
                "tool_failed",
                path=str(path),
            )
        return orjson.dumps({"deleted": True, "name": name_value}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_report", annotations=_RO)
async def identity_report() -> str:
    """Report the active engine's identity summary.

    With no running engine this returns a structured ``engine_unavailable``
    result. When the engine is active it reports the configured identity
    name/config plus the runtime ``navigator.userAgent`` read from the live
    page (never a set_user_agent acknowledgement). Engines started without an
    identity report ``identity: null`` but still carry the active bounded
    ``identityHash`` (the effective per-launch BrowserForge digest, including
    launches seeded from a saved identity) and the enabled ``stealth`` launch policy — configured or
    not. Fingerprint payloads are never returned, only the hash.
    """
    async with _healer_ref.safe("kahin_identity_report"):
        engine_error = await _require_engine()
        if engine_error:
            return engine_error

        from kahin import _state as state  # noqa: PLC0415

        engine = state._current_engine
        identity_config = getattr(engine, "_identity_config", None)
        identity_name = getattr(engine, "_identity_name", None)

        runtime_ua: str | None = None
        list_pages = getattr(engine, "list_pages", None)
        if list_pages is not None:
            try:
                pages = await list_pages()
                session_id = next(
                    (
                        page.get("sessionId")
                        for page in pages
                        if isinstance(page, dict) and page.get("current")
                    ),
                    pages[0].get("sessionId") if pages and isinstance(pages[0], dict) else None,
                )
                if isinstance(session_id, str):
                    raw = _loads(await _mirage_evaluate(
                        "navigator.userAgent", session_id=session_id,
                    ))
                    if isinstance(raw, str):
                        runtime_ua = raw
            except Exception:  # noqa: BLE001 - report must never fail the engine
                runtime_ua = None

        identity: dict[str, Any] | None = None
        if isinstance(identity_config, dict) and identity_config:
            identity = {
                "name": identity_name if isinstance(identity_name, str) else None,
                "config": _identity_summary(identity_config),
            }
        active_hash = getattr(engine, "_identity_hash", None)
        stealth = getattr(engine, "_launch_policy", None)
        return orjson.dumps({
            "active": True,
            "engine": "mirage" if list_pages is not None else "shadow",
            "identity": identity,
            "identityHash": active_hash if isinstance(active_hash, str) else None,
            "stealth": stealth if isinstance(stealth, dict) and stealth else None,
            "navigator.userAgent": runtime_ua,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_agent_status", annotations=_RO)
async def agent_status() -> str:
    """Agent-loop overview of the running engine and its live page.

    Reports engine liveness, the current page's url/title/readyState (read
    from the live page), tab count and current tab, whether snapshot refs are
    still live plus the latest DOM-stream cursor, pending dialogs, and the
    buffered network/console event counts. Never raises: with no engine this
    returns a structured idle response, and every engine-backed field
    degrades to a neutral value when its source is unavailable.
    ``identityHash`` (active bounded digest) and ``stealth`` (enabled launch
    policy) are reported for any running Mirage engine, configured or not.
    ``refsLive``/``domCursor`` come from real DOM-stream bookkeeping — refs
    are live only after a successful snapshot and are invalidated by
    reset/dropped/stale/stop, never guessed.
    """
    async with _healer_ref.safe("kahin_agent_status"):
        from kahin import _state as state  # noqa: PLC0415
        from kahin.the_twins.mirage import Mirage  # noqa: PLC0415
        from kahin.the_twins.shadow import Obscura  # noqa: PLC0415

        engine = state._current_engine
        if engine is None:
            return orjson.dumps({
                "engine": None,
                "alive": False,
                "url": None,
                "title": None,
                "readyState": None,
                "tabCount": 0,
                "currentTab": None,
                "refsLive": False,
                "domCursor": None,
                "domNextSeq": None,
                "pendingDialogs": 0,
                "networkEvents": 0,
                "consoleMessages": 0,
                "identity": None,
                "identityHash": None,
                "stealth": None,
                "windows": [],
                "windowSummary": None,
                "hint": "use kahin_browser_start",
            }, option=orjson.OPT_INDENT_2).decode()

        payload: dict[str, Any] = {
            "engine": "mirage" if isinstance(engine, Mirage) else (
                "shadow" if isinstance(engine, Obscura) else type(engine).__name__.lower()
            ),
            "alive": False,
            "url": None,
            "title": None,
            "readyState": None,
            "tabCount": 0,
            "currentTab": None,
            "refsLive": False,
            "domCursor": None,
            "domNextSeq": None,
            "pendingDialogs": 0,
            "networkEvents": len(state._network_requests),
            "consoleMessages": len(state._console_messages),
            "identity": None,
            "identityHash": None,
            "stealth": None,
            "windows": [],
            "windowSummary": None,
        }

        identity_config = getattr(engine, "_identity_config", None)
        identity_name = getattr(engine, "_identity_name", None)
        if isinstance(identity_config, dict) and identity_config:
            payload["identity"] = {
                "name": identity_name if isinstance(identity_name, str) else None,
                "summary": _identity_summary(identity_config),
            }
        # Active bounded identity hash and the enabled stealth launch
        # policy, whether or not an identity is configured. Never the raw
        # fingerprint payload or proxy credentials.
        active_hash = getattr(engine, "_identity_hash", None)
        payload["identityHash"] = active_hash if isinstance(active_hash, str) else None
        stealth = getattr(engine, "_launch_policy", None)
        payload["stealth"] = stealth if isinstance(stealth, dict) and stealth else None

        if isinstance(engine, Mirage):
            try:
                health = await asyncio.wait_for(engine.health(), timeout=5.0)
                payload["alive"] = bool(health.get("alive"))
            except Exception:  # noqa: BLE001 - status must never raise
                payload["alive"] = False
            # A dead sidecar may still have Python-side session maps. Never
            # expose those stale maps as live tabs, refs, or page metadata.
            # Agents use this tool as their recovery decision point; a dead
            # engine must be an unambiguous neutral state.
            if not payload["alive"]:
                return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
            try:
                pages = await engine.list_pages()
                payload["tabCount"] = len(pages)
            except Exception:  # noqa: BLE001
                pages = []

            current_target = engine._current_target
            if current_target not in engine._sessions:
                current_target = next(
                    (
                        page.get("targetId")
                        for page in pages
                        if isinstance(page, dict)
                        and isinstance(page.get("targetId"), str)
                    ),
                    None,
                )
            payload["currentTab"] = current_target

            # Status is observational. It must never call ensure_page(),
            # because that silently creates an about:blank tab merely because
            # an agent asked for telemetry.
            session_id = engine._sessions.get(current_target or "")
            # A JS dialog parks the page: an evaluate would only stall. The
            # dialog itself is the page state worth reporting.
            dialog_blocked = isinstance(session_id, str) and bool(engine.open_dialogs(session_id))
            if isinstance(session_id, str) and not dialog_blocked:
                try:
                    raw = _loads(await _mirage_evaluate(
                        "JSON.stringify({url: location.href, title: document.title,"
                        " readyState: document.readyState,"
                        f" stream:(() => {{ const s = window[Symbol.for({DOM_STREAM_GLOBAL!r})];"
                        " return s && s.status ? s.status() : null; })()})",
                        session_id=session_id,
                    ))
                    if isinstance(raw, str):
                        page_state = _loads(raw)
                        if isinstance(page_state, dict):
                            for key in ("url", "title", "readyState"):
                                value = page_state.get(key)
                                if isinstance(value, str):
                                    payload[key] = value
                            stream_state = page_state.get("stream")
                            if isinstance(stream_state, dict):
                                live_stream_id = stream_state.get("streamId")
                                live_next_seq = stream_state.get("nextSeq")
                                _dom_stream_record(
                                    session_id,
                                    stream_id=live_stream_id if isinstance(live_stream_id, str) else None,
                                    next_seq=live_next_seq if isinstance(live_next_seq, int) else None,
                                )
                except Exception:  # noqa: BLE001
                    pass
                stream = _dom_stream_status(session_id)
                if stream is not None:
                    payload["refsLive"] = bool(stream.get("refsLive"))
                    cursor = stream.get("cursor")
                    payload["domCursor"] = cursor if isinstance(cursor, int) else None
                    next_seq = stream.get("nextSeq")
                    payload["domNextSeq"] = next_seq if isinstance(next_seq, int) else None
            # Dialog state comes from the engine's live dialog tracker (the
            # same one that fails dialog-blocked commands fast). Do not call
            # the public dialog tool from a status probe: that hidden nested
            # MCP operation added latency and could race a page session.
            payload["pendingDialogs"] = len(engine.open_dialogs(session_id)) if isinstance(session_id, str) else 0
            # P0-2: every window in one list — Juggler tabs, Marionette
            # windows (Bitwarden popup/FIDO2 popout included) and native
            # dialogs — from the scanner kahin_vault_login itself uses.
            try:
                inventory = await window_inventory.collect(engine, timeout=3.0)
                payload["windows"] = inventory["windows"]
                payload["windowSummary"] = {**inventory["summary"], "marionette": inventory["marionette"]}
            except Exception:  # noqa: BLE001 - status must never raise
                payload["windowSummary"] = {"error": "inventory_unavailable"}
        else:
            payload["alive"] = bool(engine.is_alive()) if hasattr(engine, "is_alive") else True
            if payload["alive"] and isinstance(engine, Obscura):
                # Obscura owns one attached page per browser connection, but
                # unlike Mirage it has no Python-side tab map.  Status must
                # still report the real target and live document instead of
                # pretending that a working Shadow page is an empty engine.
                target_id = getattr(engine, "_target_id", None)
                session_id = getattr(engine, "_session_id", None)
                if isinstance(target_id, str) and isinstance(session_id, str):
                    payload["tabCount"] = 1
                    payload["currentTab"] = target_id
                    try:
                        raw = await asyncio.wait_for(
                            engine.call(
                                "Runtime.evaluate",
                                {
                                    "expression": (
                                        "({url: location.href, title: document.title, "
                                        "readyState: document.readyState})"
                                    ),
                                    "returnByValue": True,
                                },
                                session_id=session_id,
                            ),
                            timeout=5.0,
                        )
                        value = ((raw.get("result") or {}).get("value")) if isinstance(raw, dict) else None
                        if isinstance(value, dict):
                            for key in ("url", "title", "readyState"):
                                if isinstance(value.get(key), str):
                                    payload[key] = value[key]
                    except Exception:  # noqa: BLE001 - status must never raise
                        pass

        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_challenge_status", annotations=_RO)
async def challenge_status() -> str:
    """Detect challenge/rate-limit pages and return a safe next action.

    This is intentionally detection-only. It never solves, bypasses or
    retries a CAPTCHA; agents receive an explicit pause/backoff contract so a
    crawl cannot blindly hammer a blocked origin.
    """
    tool = "kahin_challenge_status"
    async with _healer_ref.safe(tool):
        from kahin import _state as state  # noqa: PLC0415

        err = await _require_engine()
        if err:
            return err
        from kahin.the_twins.mirage import Mirage  # noqa: PLC0415
        from kahin.the_twins.shadow import Obscura  # noqa: PLC0415

        engine = state._current_engine
        session_id: str | None = None
        if isinstance(engine, Mirage):
            session_id, capture_error = await _capture_page_session(tool)
            if capture_error:
                return capture_error
            assert session_id is not None
            result = await _mirage_eval_result(_CHALLENGE_STATUS_JS, session_id=session_id)
        elif isinstance(engine, Obscura):
            # Shadow is the fast crawl engine and already has a live CDP
            # session. Challenge detection is observational, so it must not
            # promote to Mirage or open another browser merely to inspect the
            # current document.
            session_id = getattr(engine, "_session_id", None)
            if not isinstance(session_id, str) or not session_id:
                return _json_error(tool, "selected Shadow page has no live session", "session_unavailable")
            try:
                result = await asyncio.wait_for(
                    engine.call(
                        "Runtime.evaluate",
                        {"expression": _CHALLENGE_STATUS_JS, "returnByValue": True},
                        session_id=session_id,
                    ),
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001 - public tool returns JSON
                return _json_error(tool, f"challenge probe failed: {exc}", "challenge_probe_failed")
        else:
            return _json_error(tool, "active engine has no challenge probe", "capability_unavailable")
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return _json_error(tool, "challenge probe failed", "javascript_error")
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            return _json_error(tool, "challenge probe returned an invalid payload", "invalid_engine_response")
        # A 429/403/503 response can render an otherwise innocuous body. Use
        # the bounded live network buffer as a second signal so a crawler
        # cannot miss a server-side block merely because its HTML is generic.
        network_status: int | None = None
        retry_after_seconds: float | None = None
        current_url = value.get("url") if isinstance(value.get("url"), str) else ""
        requests_by_id: dict[str, dict[str, Any]] = {}
        current_document_started = 0.0
        for event in state._network_requests:
            if event.get("session_id") != session_id:
                continue
            params = event.get("params") or {}
            request_id = params.get("requestId")
            if event.get("event") == "requestWillBeSent" and isinstance(request_id, str):
                requests_by_id[request_id] = params
                if params.get("url") == current_url:
                    current_document_started = max(
                        current_document_started, float(event.get("timestamp") or 0.0)
                    )
        ignored_resource_causes = {
            "TYPE_IMAGE",
            "TYPE_STYLESHEET",
            "TYPE_FONT",
            "TYPE_MEDIA",
            "TYPE_CSS",
        }
        for event in reversed(state._network_requests):
            if event.get("session_id") != session_id or event.get("event") != "responseReceived":
                continue
            if current_document_started and float(event.get("timestamp") or 0.0) < current_document_started:
                continue
            params = event.get("params") or {}
            response = _network_response_payload(event)
            status = response.get("status")
            if not isinstance(status, (int, float)) or isinstance(status, bool):
                continue
            status_int = int(status)
            if status_int not in {403, 429, 503}:
                continue
            request_id = params.get("requestId")
            request_params = requests_by_id.get(request_id, {}) if isinstance(request_id, str) else {}
            if request_params.get("cause") in ignored_resource_causes:
                continue
            response_url = response.get("url") or params.get("url") or request_params.get("url")
            if current_url and isinstance(response_url, str) and response_url != current_url:
                # Keep API/fetch failures visible, but do not let an old
                # document (even on the same origin) or an unrelated asset
                # poison the current challenge decision.
                if request_params.get("cause") not in {"TYPE_XHR", "TYPE_FETCH"}:
                    continue
            network_status = status_int
            retry_after_seconds = _retry_after_seconds(response.get("headers"))
            break
        if network_status is not None:
            signals = value.get("signals")
            if not isinstance(signals, list):
                signals = []
            signals = [*signals, f"http-status-{network_status}"]
            value["signals"] = signals[:20]
            value["detected"] = True
            value["httpStatus"] = network_status
            if value.get("kind") is None:
                if network_status == 403:
                    value["kind"] = "access_denied"
                    value["action"] = "stop_and_review_authorization"
                    value["retryable"] = False
                else:
                    value["kind"] = "rate_limit"
                    value["action"] = "honor_retry_after_and_backoff"
                    value["retryable"] = True
            if retry_after_seconds is not None:
                value["retryAfterSeconds"] = retry_after_seconds
        return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()
