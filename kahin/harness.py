"""Operator discipline at the MCP boundary (P1-2).

The browser chain was healthy; the agent driving it was not: it re-ran a
call that had just failed twice, kept sending commands into a page that had
stopped answering, re-ran the same path after a restart that could not have
fixed it, and clicked blind. These four rules stop each loop where it starts,
for every tool, without trusting the agent to notice:

(a) ``repeat``   — the same tool with the same arguments failed
                   ``repeat_fail_limit`` times: the next identical call is
                   refused. Change the arguments, observe, or fix the cause.
(b) ``halt``     — ``timeout_halt`` consecutive stalls/timeouts: Kahin takes
                   a state dump (engine health + window inventory + recent
                   failures) and refuses every state-changing call until a
                   recovery tool succeeds or ``kahin_operator_reset``.
(c) ``restart``  — a call that stalled on a page-side lock (JS dialog,
                   WebAuthn, busy page) is refused after an engine restart:
                   a restart does not clear those causes. ``dead_pipe`` stalls
                   are exempt because a restart is their fix.
(d) ``blind``    — ``blind_click_limit`` clicks in a row without a single
                   observation: the next click is refused until the agent
                   looks at the page.

Every refusal is a structured JSON answer (``code: harness_*``) carrying the
evidence and the way out. ``KAHIN_HARNESS=0`` disables the layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Any


_CLICK_TOOLS = frozenset({
    "kahin_click",
    "kahin_mirage_click",
    "kahin_mirage_click_humanized",
    "kahin_mirage_dblclick",
    "kahin_mirage_dom_action",
    "kahin_mirage_mouse_click",
})
# Tools whose answer shows the agent the live page or browser state.
_OBSERVATION_TOOLS = frozenset({
    "kahin_agent_status",
    "kahin_challenge_status",
    "kahin_cf_status",
    "kahin_extract",
    "kahin_iframe_tree",
    "kahin_mirage_accessibility_tree",
    "kahin_mirage_dialog_list",
    "kahin_mirage_dom_events",
    "kahin_mirage_dom_snapshot",
    "kahin_mirage_expect",
    "kahin_mirage_frame_tree",
    "kahin_mirage_get_attribute",
    "kahin_mirage_get_html",
    "kahin_mirage_get_text",
    "kahin_mirage_get_value",
    "kahin_mirage_page_content",
    "kahin_mirage_query",
    "kahin_mirage_query_all",
    "kahin_mirage_screencast_frame",
    "kahin_mirage_snapshot",
    "kahin_mirage_wait_for_text",
    "kahin_mirage_wait_selector",
    "kahin_ocr",
    "kahin_passkey_setup_status",
    "kahin_screenshot",
})
# Tools that change the lock state itself; never refused, and a success
# clears the halt and the failure counters (the world changed).
_RECOVERY_TOOLS = frozenset({
    "kahin_browser_start",
    "kahin_browser_stop",
    "kahin_mirage_dialog_accept",
    "kahin_mirage_dialog_dismiss",
    "kahin_operator_reset",
    "kahin_passkey_setup_close",
})
# Pure diagnostics: never refused by (a), always allowed during a halt.
_DIAGNOSTIC_TOOLS = frozenset({
    "kahin_agent_status",
    "kahin_engine_health",
    "kahin_engine_stats",
    "kahin_healer_stats",
    "kahin_mirage_dialog_list",
})
_PAGE_LOCK_CAUSES = frozenset({"pending_js_dialog", "webauthn_pending", "page_busy"})
_STALL_CODES = frozenset({"command_stalled", "engine_health_timeout"})
_ERROR_TEXT_LIMIT = 300


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _signature(name: str, arguments: dict[str, Any] | None) -> str:
    try:
        args = json.dumps(arguments or {}, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        args = repr(sorted((arguments or {}).items()))
    return f"{name}:{args}"


def _failure_of(raw: Any) -> dict[str, Any] | None:
    """The failure carried by a tool answer, or None for a success."""
    if not isinstance(raw, str):
        return None
    text = raw.lstrip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "error" not in payload or not payload.get("error"):
        return None
    return {
        "error": str(payload.get("error"))[:_ERROR_TEXT_LIMIT],
        "code": payload.get("code"),
        "cause": payload.get("cause"),
    }


def _exception_cause(error: BaseException) -> str | None:
    """The stall cause of an exception or of anything in its cause chain."""
    seen = 0
    current: BaseException | None = error
    while current is not None and seen < 8:
        diagnosis = getattr(current, "diagnosis", None)
        if isinstance(diagnosis, dict) and diagnosis.get("cause"):
            return str(diagnosis["cause"])
        current = current.__cause__ or current.__context__
        seen += 1
    return None


def _is_stall(failure: dict[str, Any]) -> bool:
    if failure.get("cause") in _PAGE_LOCK_CAUSES or failure.get("cause") == "dead_pipe":
        return True
    if failure.get("code") in _STALL_CODES:
        return True
    return "timeout" in str(failure.get("error") or "").lower()


class OperatorHarness:
    """Stateful guard in front of every MCP tool call."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("KAHIN_HARNESS", "1").strip() not in ("0", "false", "off")
        self.repeat_fail_limit = _env_int("KAHIN_HARNESS_REPEAT_FAILS", 2, 1)
        self.timeout_halt = _env_int("KAHIN_HARNESS_TIMEOUT_HALT", 3, 1)
        self.blind_click_limit = _env_int("KAHIN_HARNESS_BLIND_CLICKS", 4, 1)
        self.reset_state()

    def reset_state(self) -> None:
        self._failures: dict[str, dict[str, Any]] = {}
        self._consecutive_stalls = 0
        self._halt: dict[str, Any] | None = None
        self._stalled_paths: dict[str, dict[str, Any]] = {}
        self._blocked_after_restart: dict[str, dict[str, Any]] = {}
        self._blind_clicks = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=8)
        self._engine_ref: Any = None
        self._generation = 0

    # --- bookkeeping --------------------------------------------------------

    def _observe_engine(self) -> None:
        """A different engine object means a restart happened (any path)."""
        from kahin import _state as state  # noqa: PLC0415

        engine = state._current_engine
        if engine is None or engine is self._engine_ref:
            return
        if self._engine_ref is not None:
            self._generation += 1
            # (c) page-side stalls survive a restart; their exact path is blocked.
            for sig, info in self._stalled_paths.items():
                self._blocked_after_restart[sig] = {**info, "blockedAtGeneration": self._generation}
            self._stalled_paths.clear()
            self._failures.clear()
            self._consecutive_stalls = 0
            self._blind_clicks = 0
        self._engine_ref = engine

    def snapshot(self) -> dict[str, Any]:
        """Agent-readable harness state (exposed through kahin_agent_status)."""
        return {
            "enabled": self.enabled,
            "limits": {
                "repeatFails": self.repeat_fail_limit,
                "timeoutHalt": self.timeout_halt,
                "blindClicks": self.blind_click_limit,
            },
            "halted": self._halt is not None,
            "haltReason": (self._halt or {}).get("reason"),
            "consecutiveStalls": self._consecutive_stalls,
            "blindClicks": self._blind_clicks,
            "refusedRepeats": sorted(
                sig for sig, info in self._failures.items() if info["count"] >= self.repeat_fail_limit
            )[:16],
            "blockedAfterRestart": sorted(self._blocked_after_restart)[:16],
            "recentFailures": list(self._recent),
        }

    # --- rules ------------------------------------------------------------------

    def precheck(self, name: str, arguments: dict[str, Any] | None, read_only: bool) -> dict[str, Any] | None:
        """A refusal payload, or None when the call may run."""
        if not self.enabled or name in _RECOVERY_TOOLS:
            return None
        self._observe_engine()
        sig = _signature(name, arguments)

        if self._halt is not None and not read_only and name not in _DIAGNOSTIC_TOOLS:
            return {
                "error": (
                    f"Kahin halted after {self.timeout_halt} consecutive stalls; "
                    f"{name} is refused until the cause is handled."
                ),
                "code": "harness_halted",
                "rule": "halt",
                "dump": self._halt.get("dump"),
                "hint": (
                    "Read the dump. Handle the cause with a recovery tool "
                    "(kahin_mirage_dialog_accept/dismiss, kahin_browser_stop/start) or call "
                    "kahin_operator_reset with the reason the next step is different. "
                    "Read-only observation tools stay available."
                ),
            }

        blocked = self._blocked_after_restart.get(sig)
        if blocked is not None:
            return {
                "error": (
                    f"{name} with these arguments stalled on '{blocked.get('cause')}' before the "
                    "engine restart; a restart does not clear that cause, so the same path is blocked."
                ),
                "code": "harness_same_path_after_restart",
                "rule": "restart",
                "cause": blocked.get("cause"),
                "previous": blocked,
                "hint": (
                    "Take a different path: handle the dialog, finish the WebAuthn ceremony through "
                    "kahin_vault_login, or observe with kahin_agent_status. kahin_operator_reset "
                    "lifts the block once the cause is gone."
                ),
            }

        failed = self._failures.get(sig)
        if failed is not None and failed["count"] >= self.repeat_fail_limit and name not in _DIAGNOSTIC_TOOLS:
            return {
                "error": (
                    f"{name} failed {failed['count']} times with identical arguments; "
                    "the identical call is refused."
                ),
                "code": "harness_repeat_refused",
                "rule": "repeat",
                "failures": failed["count"],
                "lastError": failed.get("last"),
                "hint": (
                    "Change the arguments, observe the page first (kahin_agent_status / "
                    "kahin_mirage_snapshot), or fix the reported cause."
                ),
            }

        if name in _CLICK_TOOLS and self._blind_clicks >= self.blind_click_limit:
            return {
                "error": (
                    f"{self._blind_clicks} clicks in a row without observing the page; "
                    f"{name} is refused until the page is observed."
                ),
                "code": "harness_observation_required",
                "rule": "blind",
                "blindClicks": self._blind_clicks,
                "hint": (
                    "Observe first: kahin_mirage_snapshot, kahin_screenshot, kahin_agent_status "
                    "or kahin_mirage_dom_events. Any observation re-enables clicking."
                ),
            }
        return None

    async def record(self, name: str, arguments: dict[str, Any] | None, raw: Any, error: BaseException | None) -> None:
        """Account one finished call (success or failure)."""
        if not self.enabled:
            return
        self._observe_engine()
        sig = _signature(name, arguments)
        if error is not None:
            failure: dict[str, Any] | None = {
                "error": f"{type(error).__name__}: {str(error)[:_ERROR_TEXT_LIMIT]}",
                "code": "tool_exception",
                "cause": _exception_cause(error),
            }
        else:
            failure = _failure_of(raw)

        if name in _CLICK_TOOLS:
            self._blind_clicks += 1
        elif name in _OBSERVATION_TOOLS and failure is None:
            self._blind_clicks = 0

        if failure is None:
            self._failures.pop(sig, None)
            self._consecutive_stalls = 0
            if name in _RECOVERY_TOOLS:
                self._halt = None
                self._failures.clear()
            return

        entry = {"tool": name, "at": round(time.time(), 3), **failure}
        self._recent.append(entry)
        slot = self._failures.setdefault(sig, {"count": 0, "last": None})
        slot["count"] += 1
        slot["last"] = failure
        if _is_stall(failure):
            self._consecutive_stalls += 1
            cause = failure.get("cause")
            if cause in _PAGE_LOCK_CAUSES:
                self._stalled_paths[sig] = {"tool": name, "cause": cause, "error": failure.get("error")}
            if self._consecutive_stalls >= self.timeout_halt and self._halt is None:
                self._halt = {
                    "since": round(time.time(), 3),
                    "reason": f"{self._consecutive_stalls} consecutive stalls (last: {name})",
                    "dump": await self._state_dump(),
                }
        else:
            self._consecutive_stalls = 0

    async def _state_dump(self) -> dict[str, Any]:
        """Engine health + window inventory + recent failures, all bounded."""
        from kahin import _state as state  # noqa: PLC0415

        dump: dict[str, Any] = {"recentFailures": list(self._recent), "engine": None, "inventory": None}
        engine = state._current_engine
        if engine is None:
            return dump
        health = getattr(engine, "health", None)
        if callable(health):
            try:
                dump["engine"] = await asyncio.wait_for(health(), timeout=3.0)
            except Exception as exc:  # noqa: BLE001 - a dump never raises
                dump["engine"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        last_stall = getattr(engine, "_last_stall", None)
        if isinstance(last_stall, tuple) and len(last_stall) == 2:
            dump["lastStall"] = last_stall[1]
        try:
            from kahin import window_inventory  # noqa: PLC0415

            dump["inventory"] = await window_inventory.collect(engine, timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            dump["inventory"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return dump

    def operator_reset(self, reason: str) -> dict[str, Any]:
        before = self.snapshot()
        engine_ref = self._engine_ref
        generation = self._generation
        self.reset_state()
        self._engine_ref = engine_ref
        self._generation = generation
        return {"status": "reset", "reason": reason, "before": before}


def _attach_stall(raw: Any, started: float) -> Any:
    """Give a flattened error answer the stall diagnosis it lost on the way up."""
    if not isinstance(raw, str) or _failure_of(raw) is None:
        return raw
    from kahin import _state as state  # noqa: PLC0415

    last = getattr(state._current_engine, "_last_stall", None)
    if not (isinstance(last, tuple) and len(last) == 2 and last[0] >= started):
        return raw
    payload = json.loads(raw)
    if payload.get("cause"):
        return raw
    diagnosis = last[1] if isinstance(last[1], dict) else {}
    payload["cause"] = diagnosis.get("cause")
    payload["diagnosis"] = diagnosis
    payload.setdefault("hint", diagnosis.get("hint"))
    return json.dumps(payload, indent=2, default=str)


_harness = OperatorHarness()


def get_harness() -> OperatorHarness:
    return _harness


def install(mcp: Any) -> None:
    """Wrap FastMCP's tool dispatch once; every client call passes the harness."""
    manager = mcp._tool_manager
    if getattr(manager, "_kahin_harness_installed", False):
        return
    original = manager.call_tool

    async def call_tool(
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        tool = manager.get_tool(name)
        if tool is None:
            return await original(name, arguments, context=context, convert_result=convert_result)
        annotations = getattr(tool, "annotations", None)
        read_only = bool(getattr(annotations, "readOnlyHint", False))
        refusal = _harness.precheck(name, arguments, read_only)
        if refusal is not None:
            raw: Any = json.dumps({**refusal, "tool": name}, indent=2, default=str)
            return tool.fn_metadata.convert_result(raw) if convert_result else raw
        started = time.monotonic()
        try:
            raw = await original(name, arguments, context=context, convert_result=False)
        except BaseException as exc:
            if isinstance(exc, Exception):
                await _harness.record(name, arguments, None, exc)
            raise
        raw = _attach_stall(raw, started)
        await _harness.record(name, arguments, raw, None)
        return tool.fn_metadata.convert_result(raw) if convert_result else raw

    manager.call_tool = call_tool
    manager._kahin_harness_installed = True
