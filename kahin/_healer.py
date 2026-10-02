"""_healer.py — Error logging, tracking, and self-healing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_FILE = LOG_DIR / "kahin.log"
# Bounded per-tool performance bookkeeping (Faz 4 Task 3). Tool names come
# from the fixed registry, but the tracker must stay bounded even if an
# unexpected name appears: at most this many distinct tools are tracked and
# ``top_slow`` is capped separately at read time.
_MAX_TRACKED_TOOLS = 256
_TOP_SLOW_LIMIT = 10


class ErrorCode:
    ENGINE_START_FAILED = "ENGINE_START_FAILED"
    ENGINE_TIMEOUT = "ENGINE_TIMEOUT"
    # A command got no reply but the cause is classified (P0-1). Only the
    # ``dead_pipe`` cause is a transport failure; a JS dialog, a pending
    # WebAuthn ceremony or a busy page survive any restart unchanged.
    COMMAND_STALLED = "COMMAND_STALLED"
    ENGINE_ALREADY_RUNNING = "ENGINE_ALREADY_RUNNING"
    CDP_COMMAND_FAILED = "CDP_COMMAND_FAILED"
    CONNECTION_LOST = "CONNECTION_LOST"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_CREATE_FAILED = "SESSION_CREATE_FAILED"
    NAVIGATE_FAILED = "NAVIGATE_FAILED"
    EVALUATE_FAILED = "EVALUATE_FAILED"
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"
    EXTRACT_FAILED = "EXTRACT_FAILED"
    CLICK_FAILED = "CLICK_FAILED"
    INVALID_PARAMS = "INVALID_PARAMS"
    SCHEMA_NOT_LOADED = "SCHEMA_NOT_LOADED"
    SCHEMA_QUERY_FAILED = "SCHEMA_QUERY_FAILED"
    PATTERN_DB_FAILED = "PATTERN_DB_FAILED"
    RESERVED_PORT = "RESERVED_PORT"
    UNKNOWN_ENGINE = "UNKNOWN_ENGINE"
    UNKNOWN = "UNKNOWN"


class RecoveryAction:
    NONE = "NONE"
    RESTART_ENGINE = "RESTART_ENGINE"
    RETRY = "RETRY"
    RECREATE_SESSION = "RECREATE_SESSION"
    CLEAR_STATE = "CLEAR_STATE"
    NOTIFY_USER = "NOTIFY_USER"


RECOVERY_MAP: dict[str, str] = {
    ErrorCode.ENGINE_START_FAILED: RecoveryAction.RETRY,
    ErrorCode.ENGINE_TIMEOUT: RecoveryAction.RESTART_ENGINE,
    ErrorCode.CDP_COMMAND_FAILED: RecoveryAction.RETRY,
    ErrorCode.CONNECTION_LOST: RecoveryAction.RESTART_ENGINE,
    ErrorCode.SESSION_NOT_FOUND: RecoveryAction.RECREATE_SESSION,
    ErrorCode.NAVIGATE_FAILED: RecoveryAction.RETRY,
    ErrorCode.EVALUATE_FAILED: RecoveryAction.RETRY,
    ErrorCode.SCREENSHOT_FAILED: RecoveryAction.RETRY,
    ErrorCode.CLICK_FAILED: RecoveryAction.RETRY,
    ErrorCode.EXTRACT_FAILED: RecoveryAction.RETRY,
}


@dataclass
class ErrorEntry:
    timestamp: str = ""
    level: str = "ERROR"
    tool: str = ""
    error_code: str = ErrorCode.UNKNOWN
    message: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    recovery: str = RecoveryAction.NONE
    traceback_str: str = ""
    duration_ms: float = 0.0


class ErrorTracker:
    def __init__(self) -> None:
        self._total_errors = 0
        self._total_recoveries = 0
        self._success_count = 0
        self._by_code: Counter[str] = Counter()
        self._by_tool: Counter[str] = Counter()
        self._recent: list[ErrorEntry] = []
        # Per-tool aggregate: {"calls", "errors", "total_ms", "max_ms"}.
        # total_ms/max_ms cover successful call durations only, so the
        # existing avg_timing_ms semantics (success average) are preserved
        # exactly; errors are counted separately.
        self._tool_stats: dict[str, dict[str, float | int]] = {}

    def _stat(self, tool: str) -> dict[str, float | int] | None:
        """Fetch or create the aggregate entry for a tool. Returns None when
        the tracker already holds the maximum number of distinct tools."""
        stat = self._tool_stats.get(tool)
        if stat is not None:
            return stat
        if len(self._tool_stats) >= _MAX_TRACKED_TOOLS:
            return None
        stat = {"calls": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0}
        self._tool_stats[tool] = stat
        return stat

    def record_error(self, entry: ErrorEntry) -> None:
        self._total_errors += 1
        self._by_code[entry.error_code] += 1
        self._by_tool[entry.tool] += 1
        stat = self._stat(entry.tool)
        if stat is not None:
            stat["errors"] = int(stat["errors"]) + 1
        self._recent.append(entry)
        if len(self._recent) > 100:
            self._recent.pop(0)

    def record_success(self, tool: str, duration_ms: float) -> None:
        self._success_count += 1
        stat = self._stat(tool)
        if stat is None:
            return
        stat["calls"] = int(stat["calls"]) + 1
        stat["total_ms"] = round(float(stat["total_ms"]) + duration_ms, 3)
        stat["max_ms"] = round(max(float(stat["max_ms"]), duration_ms), 3)

    def record_recovery(self) -> None:
        self._total_recoveries += 1

    def get_stats(self) -> dict[str, Any]:
        avg_timings = {}
        for tool, stat in self._tool_stats.items():
            calls = int(stat["calls"])
            if calls:
                avg_timings[tool] = round(float(stat["total_ms"]) / calls, 1)
        return {
            "total_errors": self._total_errors,
            "total_successes": self._success_count,
            "total_recoveries": self._total_recoveries,
            "errors_by_code": dict(self._by_code.most_common(10)),
            "errors_by_tool": dict(self._by_tool.most_common(10)),
            "avg_timing_ms": avg_timings,
            "recent_errors": [
                {
                    "tool": e.tool,
                    "code": e.error_code,
                    "message": e.message[:100],
                    "time": e.timestamp,
                    "recovery": e.recovery,
                }
                for e in self._recent[-5:]
            ],
        }

    def engine_stats(self) -> dict[str, Any]:
        """Bounded, agent-friendly performance rollup (Faz 4 Task 3).

        Deterministic enough for agents: per-tool averages and maxima are
        rounded, ``top_slow`` is capped, and only the most recent error is
        reported. No unbounded per-tool data leaves the tracker.
        """
        def _avg_key(item: tuple[str, dict[str, float | int]]) -> tuple[float, float]:
            _, stat = item
            calls = int(stat["calls"])
            avg = float(stat["total_ms"]) / calls if calls else 0.0
            return (avg, float(stat["max_ms"]))

        ranked = sorted(self._tool_stats.items(), key=_avg_key, reverse=True)[:_TOP_SLOW_LIMIT]
        top_slow = []
        for tool, stat in ranked:
            calls = int(stat["calls"])
            top_slow.append({
                "tool": tool,
                "count": calls,
                "avg_ms": round(float(stat["total_ms"]) / calls, 1) if calls else 0.0,
                "max_ms": float(stat["max_ms"]),
            })
        last = self._recent[-1] if self._recent else None
        return {
            "tool_calls": self._success_count,
            "tool_errors": self._total_errors,
            "top_slow": top_slow,
            "last_error": (
                {
                    "tool": last.tool,
                    "code": last.error_code,
                    "message": last.message[:200],
                    "time": last.timestamp,
                    "recovery": last.recovery,
                }
                if last is not None
                else None
            ),
        }

_tracker = ErrorTracker()


def get_tracker() -> ErrorTracker:
    return _tracker


class Healer:
    def __init__(self, log_path: str | Path | None = None) -> None:
        self._log_path = Path(log_path) if log_path else LOG_FILE
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine_ref = None
        self._state_ref = None

    def bind_engine(self, engine_ref: Any) -> None:
        self._engine_ref = engine_ref

    def bind_state(self, state_ref: Any) -> None:
        self._state_ref = state_ref

    def _write_log(self, entry: ErrorEntry) -> None:
        data = {
            "timestamp": entry.timestamp or datetime.now(timezone.utc).isoformat(),
            "level": entry.level,
            "tool": entry.tool,
            "error_code": entry.error_code,
            "message": entry.message,
            "context": entry.context,
            "recovery": entry.recovery,
            "duration_ms": round(entry.duration_ms, 1),
        }
        if entry.traceback_str:
            data["traceback"] = entry.traceback_str[:2000]
        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(data) + "\n")
        except OSError:
            pass

    async def _execute_recovery(self, action: str, tool: str, context: dict[str, Any]) -> str | None:
        if action in (RecoveryAction.NONE, RecoveryAction.NOTIFY_USER):
            return None
        _tracker.record_recovery()
        self._write_log(ErrorEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level="INFO",
            tool=tool,
            error_code="RECOVERY",
            message=f"Executing recovery: {action}",
            context=context,
        ))

        if action == RecoveryAction.CLEAR_STATE:
            if self._state_ref is not None:
                from kahin._state import clear_state
                clear_state()
            return "state cleared"

        if action == RecoveryAction.RESTART_ENGINE:
            engine = self._engine_ref
            if engine is None and self._state_ref is not None:
                engine = (
                    self._state_ref.get("_current_engine")
                    if isinstance(self._state_ref, dict)
                    else getattr(self._state_ref, "_current_engine", None)
                )
            if engine is not None:
                try:
                    await engine.stop()
                except Exception as exc:
                    logger.exception("engine cleanup failed during recovery")
                    # Do not clear the state or healer reference when the
                    # child may still be alive. The next explicit stop can
                    # still reap the same object; publishing a replacement
                    # here would create an orphan and target the wrong engine.
                    return f"engine cleanup failed: {exc}"
                self._engine_ref = None
            if self._state_ref is not None:
                if isinstance(self._state_ref, dict):
                    self._state_ref["_current_engine"] = None
                else:
                    setattr(self._state_ref, "_current_engine", None)
            from kahin._state import clear_state
            clear_state()
            return "engine stopped, state cleared"

        if action == RecoveryAction.RETRY:
            return "retry suggested"

        return None

    def _engine_is_alive(self) -> bool:
        engine = self._engine_ref
        if engine is None and self._state_ref is not None:
            engine = (
                self._state_ref.get("_current_engine")
                if isinstance(self._state_ref, dict)
                else getattr(self._state_ref, "_current_engine", None)
            )
        is_alive = getattr(engine, "is_alive", None)
        if not callable(is_alive):
            return False
        try:
            return bool(is_alive())
        except Exception:  # noqa: BLE001 - liveness probe only
            return False

    def _determine_recovery(self, error_code: str) -> str:
        return RECOVERY_MAP.get(error_code, RecoveryAction.NONE)

    @asynccontextmanager
    async def safe(
        self, tool: str, **context: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        t0 = time.monotonic()
        entry = ErrorEntry(tool=tool, context=context)
        try:
            yield {"ok": True}
            duration = (time.monotonic() - t0) * 1000
            _tracker.record_success(tool, duration)
        except asyncio.TimeoutError:
            duration = (time.monotonic() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.ENGINE_TIMEOUT
            entry.message = f"Timeout in {tool}"
            entry.duration_ms = duration
            entry.recovery = self._determine_recovery(entry.error_code)
            if entry.recovery == RecoveryAction.RESTART_ENGINE and self._engine_is_alive():
                entry.recovery = RecoveryAction.NOTIFY_USER
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
            raise RuntimeError(f"{entry.error_code}: {entry.message}" + (f" [{recovery_msg}]" if recovery_msg else ""))
        except ConnectionError as e:
            duration = (time.monotonic() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.CONNECTION_LOST
            entry.message = str(e)
            entry.duration_ms = duration
            entry.recovery = self._determine_recovery(entry.error_code)
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
            raise RuntimeError(f"{entry.error_code}: {entry.message}" + (f" [{recovery_msg}]" if recovery_msg else ""))
        except RuntimeError as e:
            duration = (time.monotonic() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.UNKNOWN
            msg = str(e)
            diagnosis = getattr(e, "diagnosis", None)
            if isinstance(diagnosis, dict):
                cause = str(diagnosis.get("cause") or "page_busy")
                entry.error_code = ErrorCode.COMMAND_STALLED
                entry.message = msg
                entry.context = {**context, "cause": cause}
                entry.duration_ms = duration
                # Restarting is the fix for a dead pipe only. For a dialog,
                # WebAuthn or busy page it destroys the session and the next
                # run walks into the same wall (kahin.log 2026-10-02).
                entry.recovery = (
                    RecoveryAction.RESTART_ENGINE if cause == "dead_pipe" else RecoveryAction.NOTIFY_USER
                )
                entry.traceback_str = traceback.format_exc()
                self._write_log(entry)
                _tracker.record_error(entry)
                if entry.recovery == RecoveryAction.RESTART_ENGINE:
                    recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
                    raise RuntimeError(f"{entry.error_code}: {msg} [auto-recovery: {recovery_msg}]") from e
                raise
            if "not found" in msg.lower() or "no session" in msg.lower():
                entry.error_code = ErrorCode.SESSION_NOT_FOUND
            elif "timeout" in msg.lower():
                entry.error_code = ErrorCode.ENGINE_TIMEOUT
            elif "connection" in msg.lower() or "closed" in msg.lower() or "ws" in msg.lower():
                entry.error_code = ErrorCode.CONNECTION_LOST
            elif "start" in msg.lower() or "failed" in msg.lower():
                entry.error_code = ErrorCode.ENGINE_START_FAILED
            elif "CDP error" in msg:
                entry.error_code = ErrorCode.CDP_COMMAND_FAILED
            entry.message = msg
            entry.duration_ms = duration
            entry.recovery = self._determine_recovery(entry.error_code)
            if entry.recovery == RecoveryAction.RESTART_ENGINE and self._engine_is_alive():
                # An unclassified "timeout" string on a live engine is not a
                # reason to kill the browser and every session in it.
                entry.recovery = RecoveryAction.NOTIFY_USER
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
            if entry.recovery not in (RecoveryAction.NONE, RecoveryAction.NOTIFY_USER):
                raise RuntimeError(f"{entry.error_code}: {entry.message} [auto-recovery: {recovery_msg}]")
            raise
        except Exception as e:
            duration = (time.monotonic() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.UNKNOWN
            entry.message = str(e)
            entry.duration_ms = duration
            entry.recovery = RecoveryAction.NOTIFY_USER
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            raise


_healer: Healer | None = None


def get_healer() -> Healer:
    global _healer
    if _healer is None:
        _healer = Healer()
    return _healer
