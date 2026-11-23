"""dialog_mirage.py — Mirage (Camoufox/Juggler) dialog/download/worker/ws tools.

Faz 9 Task 3: Juggler has NO list methods for dialogs, downloads, workers or
web sockets — they are event-driven (verified against Protocol.js):

- dialogs   -> Page.dialogOpened / dialogClosed events; act with
               Page.handleDialog {dialogId, accept, promptText?}
- downloads -> Browser.downloadCreated {uuid, pageTargetId, url,
               suggestedFileName} / downloadFinished {uuid, canceled?, error?};
               behavior via Browser.setDownloadOptions
               {downloadOptions: {behavior: saveToDisk|cancel, downloadsDir?}}
- workers   -> Page.workerCreated {workerId, frameId, url} /
               workerDestroyed {workerId}
- websocket -> Page.webSocketCreated {wsid, requestURL} / webSocketOpened /
               webSocketClosed {error} / webSocketFrameSent|Received

The list tools therefore read the forwarded event buffer
(``state._current_event_log``) and merge lifecycle events into live objects.
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.the_twins.mirage import describe_dialog
from kahin.tools._common import _RO, _RW, _healer_ref, _mirage_call, _mirage_engine, _require_mirage


def _dialog_meta(dialog_id: str) -> dict[str, Any] | None:
    """Look up live dialog metadata directly from Mirage's open-dialog table."""
    engine = state._current_engine
    open_dialogs = getattr(engine, "open_dialogs", None)
    if callable(open_dialogs):
        for item in open_dialogs():
            if item.get("dialogId") == dialog_id:
                return item
    return None


def _dialog_owner_session(dialog_id: str) -> str | None:
    """Find the session that opened a dialog; prefer live Mirage state."""
    meta = _dialog_meta(dialog_id)
    if isinstance(meta, dict):
        sid = meta.get("sessionId")
        if isinstance(sid, str) and sid:
            return sid
    for event in reversed(state._current_event_log):
        ev_name = event.get("event")
        params = event.get("params") or {}
        if params.get("dialogId") != dialog_id:
            continue
        if ev_name in {"Page.dialogClosed", "Page.javascriptDialogClosed"}:
            return None
        if ev_name in {"Page.dialogOpened", "Page.javascriptDialogOpening"}:
            session_id = event.get("session_id")
            return session_id if isinstance(session_id, str) and session_id else None
    return None


@mcp.tool(name="kahin_mirage_dialog_list", annotations=_RO)
async def mirage_dialog_list() -> str:
    """Mirage: interactive dialogs currently open on the active tab."""
    async with _healer_ref.safe("kahin_mirage_dialog_list"):
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        current_session = engine.current_session_id()
        dialogs = engine.open_dialogs(current_session) if current_session else []
        return orjson.dumps(
            [describe_dialog(item) for item in dialogs],
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_mirage_dialog_accept", annotations=_RW)
async def mirage_dialog_accept(
    dialog_id: str,
    prompt_text: str | None = None,
    humanize: bool = False,
) -> str:
    """Mirage: accept a dialog (Page.handleDialog accept=true). prompt_text is
    used for prompt() dialogs; in headed mode it is visibly typed first, and
    humanize=true adds human-like timing even in headless mode."""
    if not isinstance(dialog_id, str) or not dialog_id:
        return orjson.dumps({"error": "dialog_id must be a non-empty string", "code": "invalid_argument"}).decode()
    if prompt_text is not None and not isinstance(prompt_text, str):
        return orjson.dumps({"error": "prompt_text must be a string when provided", "code": "invalid_argument"}).decode()
    if not isinstance(humanize, bool):
        return orjson.dumps({"error": "humanize must be a boolean", "code": "invalid_argument"}).decode()
    async with _healer_ref.safe("kahin_mirage_dialog_accept", dialog_id=dialog_id[:80]):
        params: dict[str, Any] = {"dialogId": dialog_id, "accept": True}
        if prompt_text is not None:
            params["promptText"] = prompt_text
        meta = _dialog_meta(dialog_id)
        owner_session = _dialog_owner_session(dialog_id)
        if owner_session is None:
            return orjson.dumps({
                "error": "dialog_id is not present in the live dialog buffer; refusing to route it to the current tab",
                "code": "stale_dialog",
            }).decode()
        is_prompt = (meta or {}).get("type") == "prompt"
        engine = state._current_engine
        nav_seq_map = getattr(engine, "_nav_seq", None)
        nav_seq_before = int(nav_seq_map.get(owner_session, 0)) if isinstance(nav_seq_map, dict) else 0
        visible_typed = False
        if prompt_text is not None and is_prompt:
            type_fn = getattr(engine, "type_into_visible_dialog", None)
            if callable(type_fn) and not getattr(engine, "_headless", True):
                visible_typed = await type_fn(
                    prompt_text,
                    char_delay_ms=55 if humanize else 35,
                    settle_ms=300 if humanize else 250,
                    allow_empty_clear=bool((meta or {}).get("defaultValue")),
                    humanize=humanize,
                )
            elif humanize:
                await asyncio.sleep(min(1.5, 0.12 + 0.04 * len(prompt_text)))
        raw = await _mirage_call("Page.handleDialog", params, session_id=owner_session)
        try:
            parsed = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return raw
        if isinstance(parsed, dict) and parsed.get("error"):
            return raw
        settle_fn = getattr(engine, "settle_after_dialog", None)
        if callable(settle_fn):
            await settle_fn(
                owner_session,
                dialog_id,
                dialog_type=(meta or {}).get("type"),
                accepted=True,
                nav_seq_before=nav_seq_before,
            )
        reply: dict[str, Any] = {
            "handled": "accepted",
            "dialogId": dialog_id,
            "type": (meta or {}).get("type") or "dialog",
        }
        if prompt_text is not None:
            reply["promptText"] = prompt_text
            reply["visibleTyped"] = visible_typed
        peek_fn = getattr(engine, "peek_unreported_auto_dialogs", None)
        mark_fn = getattr(engine, "mark_auto_dialogs_delivered", None)
        if callable(peek_fn):
            unreported_auto, max_seq = peek_fn(owner_session)
            if unreported_auto:
                reply["dialogs"] = unreported_auto
                if callable(mark_fn):
                    mark_fn(owner_session, max_seq)
        open_fn = getattr(engine, "open_dialogs", None)
        if callable(open_fn):
            next_open = open_fn(owner_session)
            if next_open:
                described_next = [describe_dialog(item) for item in next_open]
                reply["nextDialog"] = described_next[0]
                if len(described_next) > 1:
                    reply["openDialogs"] = described_next
        return orjson.dumps(reply, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_dialog_dismiss", annotations=_RW)
async def mirage_dialog_dismiss(dialog_id: str) -> str:
    """Mirage: dismiss (cancel) a dialog (Page.handleDialog accept=false)."""
    if not isinstance(dialog_id, str) or not dialog_id:
        return orjson.dumps({"error": "dialog_id must be a non-empty string", "code": "invalid_argument"}).decode()
    async with _healer_ref.safe("kahin_mirage_dialog_dismiss", dialog_id=dialog_id[:80]):
        meta = _dialog_meta(dialog_id)
        owner_session = _dialog_owner_session(dialog_id)
        if owner_session is None:
            return orjson.dumps({
                "error": "dialog_id is not present in the live dialog buffer; refusing to route it to the current tab",
                "code": "stale_dialog",
            }).decode()
        engine = state._current_engine
        nav_seq_map = getattr(engine, "_nav_seq", None)
        nav_seq_before = int(nav_seq_map.get(owner_session, 0)) if isinstance(nav_seq_map, dict) else 0
        raw = await _mirage_call(
            "Page.handleDialog", {"dialogId": dialog_id, "accept": False}, session_id=owner_session,
        )
        try:
            parsed = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return raw
        if isinstance(parsed, dict) and parsed.get("error"):
            return raw
        settle_fn = getattr(engine, "settle_after_dialog", None)
        if callable(settle_fn):
            await settle_fn(
                owner_session,
                dialog_id,
                dialog_type=(meta or {}).get("type"),
                accepted=False,
                nav_seq_before=nav_seq_before,
            )
        reply: dict[str, Any] = {
            "handled": "dismissed",
            "dialogId": dialog_id,
            "type": (meta or {}).get("type") or "dialog",
        }
        peek_fn = getattr(engine, "peek_unreported_auto_dialogs", None)
        mark_fn = getattr(engine, "mark_auto_dialogs_delivered", None)
        if callable(peek_fn):
            unreported_auto, max_seq = peek_fn(owner_session)
            if unreported_auto:
                reply["dialogs"] = unreported_auto
                if callable(mark_fn):
                    mark_fn(owner_session, max_seq)
        open_fn = getattr(engine, "open_dialogs", None)
        if callable(open_fn):
            next_open = open_fn(owner_session)
            if next_open:
                described_next = [describe_dialog(item) for item in next_open]
                reply["nextDialog"] = described_next[0]
                if len(described_next) > 1:
                    reply["openDialogs"] = described_next
        return orjson.dumps(reply, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_download_list", annotations=_RO)
async def mirage_download_list() -> str:
    """Mirage: downloads seen so far (Browser.downloadCreated/downloadFinished
    events), with finished/canceled/error status merged per uuid."""
    async with _healer_ref.safe("kahin_mirage_download_list"):
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        current_target = engine._current_target
        downloads: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            params = e.get("params") or {}
            if e["event"] == "Browser.downloadCreated":
                if params.get("pageTargetId") and params.get("pageTargetId") != current_target:
                    continue
                downloads[params.get("uuid", "?")] = {
                    "uuid": params.get("uuid"),
                    "pageTargetId": params.get("pageTargetId"),
                    "frameId": params.get("frameId"),
                    "url": params.get("url"),
                    "suggestedFileName": params.get("suggestedFileName"),
                    "status": "in-progress",
                }
            elif e["event"] == "Browser.downloadFinished":
                d = downloads.get(params.get("uuid", ""))
                if d:
                    d["status"] = "canceled" if params.get("canceled") else (
                        f"failed: {params.get('error')}" if params.get("error") else "finished"
                    )
        return orjson.dumps(list(downloads.values()), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_download_save", annotations=_RW)
async def mirage_download_save(downloads_dir: str | None = None) -> str:
    """Mirage: save downloads to disk instead of canceling them
    (Browser.setDownloadOptions behavior=saveToDisk, optional downloadsDir)."""
    if downloads_dir is not None and (not isinstance(downloads_dir, str) or len(downloads_dir) > 4096):
        return orjson.dumps({"error": "downloads_dir must be a string of at most 4096 characters", "code": "invalid_argument"}).decode()
    async with _healer_ref.safe("kahin_mirage_download_save", downloads_dir=downloads_dir or ""):
        options: dict[str, Any] = {"behavior": "saveToDisk"}
        if downloads_dir:
            options["downloadsDir"] = downloads_dir
        params: dict[str, Any] = {"downloadOptions": options}
        try:
            context_id = _mirage_engine().current_browser_context_id()
        except Exception:  # liveness is reported by _mirage_call
            context_id = None
        if context_id:
            params["browserContextId"] = context_id
        return await _mirage_call("Browser.setDownloadOptions", params)


@mcp.tool(name="kahin_mirage_worker_list", annotations=_RO)
async def mirage_worker_list() -> str:
    """Mirage: web workers alive on the page (Page.workerCreated minus
    workerDestroyed), read from the forwarded event buffer."""
    async with _healer_ref.safe("kahin_mirage_worker_list"):
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        current_session = engine._sessions.get(engine._current_target or "")
        workers: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            if e.get("session_id") != current_session:
                continue
            params = e.get("params") or {}
            if e["event"] == "Page.workerCreated":
                workers[params.get("workerId", "?")] = {
                    "workerId": params.get("workerId"),
                    "frameId": params.get("frameId"),
                    "url": params.get("url"),
                }
            elif e["event"] == "Page.workerDestroyed":
                workers.pop((e.get("params") or {}).get("workerId", "?"), None)
        return orjson.dumps(list(workers.values()), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_websocket_list", annotations=_RO)
async def mirage_websocket_list() -> str:
    """Mirage: web sockets seen on the page (Page.webSocketCreated/Opened/
    Closed events), merged per wsid with opcode/data of the last frame."""
    async with _healer_ref.safe("kahin_mirage_websocket_list"):
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        current_session = engine._sessions.get(engine._current_target or "")
        sockets: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            if e.get("session_id") != current_session:
                continue
            params = e.get("params") or {}
            name = e["event"]
            if name == "Page.webSocketCreated":
                sockets[params.get("wsid", "?")] = {
                    "wsid": params.get("wsid"),
                    "frameId": params.get("frameId"),
                    "requestURL": params.get("requestURL"),
                    "effectiveURL": None,
                    "status": "created",
                    "error": None,
                    "lastOpcode": None,
                    "lastDataSize": None,
                }
            elif name == "Page.webSocketOpened":
                s = sockets.get(params.get("wsid", ""))
                if s:
                    s["effectiveURL"] = params.get("effectiveURL")
                    s["status"] = "open"
            elif name == "Page.webSocketClosed":
                s = sockets.get(params.get("wsid", ""))
                if s:
                    s["status"] = "closed"
                    s["error"] = params.get("error")
            elif name in ("Page.webSocketFrameSent", "Page.webSocketFrameReceived"):
                s = sockets.get(params.get("wsid", ""))
                if s:
                    s["lastOpcode"] = params.get("opcode")
                    s["lastDataSize"] = len(params.get("data", ""))
        return orjson.dumps(list(sockets.values()), option=orjson.OPT_INDENT_2).decode()
