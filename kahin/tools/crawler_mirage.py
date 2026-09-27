"""crawler_mirage.py — single-engine background crawl job manager (Mirage).

Task 2 of ``docs/superpowers/plans/2026-08-09-kahin-crawler-rotation.md``.
Design contract: ``docs/superpowers/specs/2026-08-09-kahin-crawler-rotation-design.md``.

The manager runs a production-shaped background crawl job on the existing
Camoufox/Mirage engine and its current tab — no second browser or tab is
ever opened. It owns:

- a bounded URL queue (10,000 max) with canonical-URL dedupe;
- a bounded result ledger with opaque, monotonic result cursors;
- an explicit job state machine (queued/running/backing_off/paused/rotating/
  completed/failed/cancelled) guarded by a single per-job lock;
- page/depth/duration/delay limits and bounded result payloads;
- Retry-After honoring plus capped exponential backoff for rate limits;
- CAPTCHA/access-denied pause with explicit resume (never bypassed, and
  rotation is never used to evade a rate limit);
- rotation that stops/starts the engine after a bounded page/time budget,
  preserving queue/result/job state;
- exactly one crash-recovery restart per job.

Every page uses the existing navigation contract (``kahin_navigate``), the
existing challenge probe (``kahin_challenge_status``) and a bounded in-page
DOM/title/link extraction. Cross-tool imports are lazy so this module can be
imported by ``kahin/tools/__init__.py`` (Task 3) without tool-registration
cycles; only shared plumbing (``kahin.tools._common``) is imported eagerly.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.the_twins.mirage import STATE_MODE_AGIRBAS, STATE_MODE_KES
from kahin.tools._common import _DW, _RO, _RW, _healer_ref

logger = logging.getLogger(__name__)

# --- hard bounds (design contract) ----------------------------------------

_MAX_QUEUE = 10_000
_MAX_RESULTS_LEDGER = 10_000
_MAX_EVENTS_PER_JOB = 10_000
_MAX_RESULTS_PER_RESPONSE = 100
_MAX_RESULT_LINKS = 100
_MAX_TITLE_LENGTH = 500
_MAX_TEXT_LENGTH = 2_000
_MAX_URL_LENGTH = 4_096
_MAX_SEEDS = 1_000
_MAX_IDENTITY_NAME_LENGTH = 64
_MAX_RECOVERY_IDENTITY_BYTES = 1 * 1024 * 1024
_MAX_PROXY_LENGTH = 4_096
_MAX_RETAINED_JOBS = 20
_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_EVENT_RESPONSE_MAX_BYTES = 2 * 1024 * 1024

_NAVIGATE_TIMEOUT = 30.0
_PAGE_OPERATION_TIMEOUT = 45.0
_CHALLENGE_SETTLE_SECONDS = 0.1
_CHALLENGE_RECHECK_SECONDS = 0.25
_ENGINE_PROBE_TIMEOUT = 5.0
_PAGE_SETTLE_TIMEOUT = 5.0
_ROTATION_GATE_TIMEOUT = 15.0

_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 60.0
_BACKOFF_MAX_RETRIES = 3
_CHALLENGE_MAX_RETRIES = 3
_EVENT_WAIT_MAX_MS = 30_000

_TERMINAL = frozenset({"completed", "failed", "cancelled"})

_ENGINE_FAILURE_CODES = frozenset({
    "engine_dead", "engine_degraded", "engine_health_timeout", "connection_lost",
    "cdp_command_failed", "challenge_probe_failed", "extraction_failed",
})

# Allowed job state transitions, guarded by the single per-job lock.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused", "cancelled"}),
    "running": frozenset({"backing_off", "paused", "rotating", "completed", "failed", "cancelled"}),
    "backing_off": frozenset({"running", "paused", "rotating", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "rotating": frozenset({"running", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

# Bounded in-page extraction: title/text/link summary only. Each field is
# sliced in JS so a hostile page can never grow the payload unboundedly.
_EXTRACT_JS = r"""
(() => {
  const MAX_URL = 4096, MAX_TITLE = 500, MAX_TEXT = 2000, MAX_LINKS = 100;
  const out = { url: "", title: "", text: "", links: [] };
  try { out.url = String(location.href || "").slice(0, MAX_URL); } catch (e) {}
  try { out.title = String(document.title || "").slice(0, MAX_TITLE); } catch (e) {}
  try {
    const raw = String(document.body && document.body.innerText || "")
      .replace(/\s+/g, " ").trim();
    out.text = raw.slice(0, MAX_TEXT);
  } catch (e) {}
  try {
    const anchors = document.querySelectorAll("a[href]");
    const seen = new Set();
    for (const a of anchors) {
      if (out.links.length >= MAX_LINKS) break;
      let href = "";
      try { href = a.href || ""; } catch (e) { continue; }
      if (!href) continue;
      let abs = "";
      try { abs = new URL(href, location.href).href; } catch (e) { continue; }
      try {
        const u = new URL(abs);
        if (u.protocol !== "http:" && u.protocol !== "https:") continue;
      } catch (e) { continue; }
      abs = abs.slice(0, MAX_URL);
      if (seen.has(abs)) continue;
      seen.add(abs);
      out.links.push(abs);
    }
  } catch (e) {}
  return out;
})()
"""


def _json_error(tool: str, message: str, code: str = "tool_error", **details: Any) -> str:
    payload: dict[str, Any] = {"error": message, "code": code, "tool": tool}
    payload.update(details)
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


def _loads(text: Any) -> dict[str, Any] | None:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None
    try:
        parsed = orjson.loads(text)
    except orjson.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _canonical_url(value: str) -> str | None:
    """Canonicalize an absolute http/https URL for queue dedupe.

    Strips the fragment, lowercases scheme/host and drops default ports.
    Query strings are preserved verbatim (dedupe is exact). Returns None for
    anything that is not a parseable absolute http/https URL.
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if not host:
        return None
    host = host.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _origin_of(value: str) -> str:
    try:
        parts = urlsplit(value)
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    except ValueError:
        return ""


def _active_identity_hash() -> str | None:
    engine = state._current_engine
    if engine is None:
        return None
    value = getattr(engine, "_identity_hash", None)
    return value if isinstance(value, str) else None


def _snapshot_identity_config(value: Any) -> dict[str, Any] | None:
    """Make a bounded, detached copy of an inline identity config.

    The crawl job retains this only for a later recovery/rotation launch;
    raw identity material is never included in a status or result payload.
    """
    if not isinstance(value, dict) or not value:
        return None
    try:
        serialized = orjson.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(serialized) > _MAX_RECOVERY_IDENTITY_BYTES:
        return None
    try:
        copied = orjson.loads(serialized)
    except orjson.JSONDecodeError:
        return None
    return copied if isinstance(copied, dict) and copied else None


def _bounded_int(
    value: Any, *, tool: str, field: str, minimum: int, maximum: int, default: int,
) -> tuple[int | None, str | None]:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        return None, _json_error(
            tool, f"{field} must be an integer", "invalid_argument", field=field,
        )
    if not minimum <= value <= maximum:
        return None, _json_error(
            tool,
            f"{field} must be between {minimum} and {maximum}",
            "invalid_argument",
            field=field,
            minimum=minimum,
            maximum=maximum,
            received=value,
        )
    return value, None


def _bounded_bool(value: Any, *, tool: str, field: str, default: bool) -> tuple[bool | None, str | None]:
    if value is None:
        return default, None
    if not isinstance(value, bool):
        return None, _json_error(
            tool, f"{field} must be a boolean", "invalid_argument", field=field,
        )
    return value, None


_EVENT_STRING_MAX = 500


def _event_scalar(value: Any) -> Any:
    """Keep event payloads JSON-safe and bounded."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:_EVENT_STRING_MAX]
    if isinstance(value, (list, tuple)):
        return [_event_scalar(item) for item in value[:20]]
    return str(value)[:_EVENT_STRING_MAX]


def _event_payload(job: "_CrawlJob", *, cursor: int, limit: int) -> str:
    """Serialize one bounded event window using monotonic event cursors."""
    ledger = list(job.events)
    oldest_cursor = (
        int(ledger[0].get("index"))
        if ledger and isinstance(ledger[0].get("index"), int)
        else job._event_seq
    )
    start = max(cursor, oldest_cursor)
    cursor_reset = cursor < oldest_cursor
    selected = [
        event
        for event in ledger
        if isinstance(event.get("index"), int) and int(event["index"]) >= start
    ][:limit]
    next_cursor = (
        int(selected[-1]["index"]) + 1
        if selected and isinstance(selected[-1].get("index"), int)
        else start
    )
    has_more = any(
        isinstance(event.get("index"), int) and int(event["index"]) >= next_cursor
        for event in ledger
    )
    payload: dict[str, Any] = {
        "jobId": job.job_id,
        "cursor": start,
        "oldestCursor": oldest_cursor,
        "cursorReset": cursor_reset,
        "limit": limit,
        "count": len(selected),
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "events": selected,
    }
    raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
    if len(raw) <= _EVENT_RESPONSE_MAX_BYTES:
        return raw
    payload["events"] = selected[: max(1, limit // 2)]
    payload["count"] = len(payload["events"])
    if payload["events"]:
        payload["nextCursor"] = int(payload["events"][-1]["index"]) + 1
        payload["hasMore"] = True
    payload["truncated"] = True
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


async def _wait_for_events(
    job: "_CrawlJob",
    *,
    cursor: int,
    limit: int,
    wait_ms: int,
) -> str:
    """Return events now or wait once for a newer event."""
    if orjson.loads(_event_payload(job, cursor=cursor, limit=limit)).get("events") or wait_ms <= 0:
        return _event_payload(job, cursor=cursor, limit=limit)
    job.event_signal.clear()
    if orjson.loads(_event_payload(job, cursor=cursor, limit=limit)).get("events"):
        return _event_payload(job, cursor=cursor, limit=limit)
    try:
        await asyncio.wait_for(job.event_signal.wait(), timeout=wait_ms / 1000)
    except asyncio.TimeoutError:
        pass
    return _event_payload(job, cursor=cursor, limit=limit)


class _CrawlJob:
    """One crawl job. Every state transition is guarded by ``self.lock``."""

    def __init__(self, job_id: str, config: dict[str, Any], seeds: list[str]) -> None:
        self.job_id = job_id
        self.config = config
        self.lock = asyncio.Lock()
        # Serializes the stop/start boundary with explicit job cancellation.
        # The cancellation flag is set before this gate is acquired, so a
        # stop request arriving after the old engine is stopped cannot allow
        # the rotation path to start a replacement engine.
        self.rotation_gate = asyncio.Lock()
        self.resume_event = asyncio.Event()

        self.state = "queued"
        self.state_reason: str | None = None
        self.state_seq = 0
        self.created_at = time.time()
        self.started_at: float | None = None
        self.updated_at = self.created_at
        self.finished_at: float | None = None

        # bounded queue state
        self._queue: deque[tuple[str, int]] = deque()
        self._queued: set[str] = set()
        self._visited: set[str] = set()
        for seed in seeds:
            self._enqueue(seed, 0)

        # bounded result ledger
        self.results: deque[dict[str, Any]] = deque(maxlen=_MAX_RESULTS_LEDGER)
        self._result_seq = 0
        self._page_budget_urls: set[str] = set()

        # Live progress ledger. Result cursors remain focused on page data;
        # this separate bounded stream lets callers observe queue/state
        # changes without repeatedly downloading the result window.
        self.events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS_PER_JOB)
        self._event_seq = 0
        self.event_signal = asyncio.Event()

        # counters
        self.pages_fetched = 0
        self.attempts = 0
        self.succeeded = 0
        self.failed = 0
        self.dropped = 0
        self.links_discovered = 0
        self.pages_since_rotation = 0
        self.rotations = 0
        self.recovery_attempts = 0
        self.last_rotation_at: float | None = None
        self.rotation_reason: str | None = None
        self.rotation_started_at: float | None = None

        # per-URL retry budgets (bounded by queue size)
        self._retries: dict[str, dict[str, int]] = {}

        # live page + backoff fields
        self.current_url: str | None = None
        self.current_depth: int | None = None
        self.in_progress = False
        self.cancel_requested = False
        self.backoff_until: float | None = None
        self.backoff_retry_after: float | None = None
        self.backoff_attempt = 0

        # challenge/error evidence (bounded)
        self.last_challenge: dict[str, Any] | None = None
        self.last_error: dict[str, Any] | None = None

        self.tab_id: str | None = None
        self.identity: str | None = None
        self.launch_identity: str | dict[str, Any] | None = None
        self.launch_headless = True
        self.launch_proxy: str | None = None
        # Durum modu işe göre seçilir ve motorun gerçek modundan devralınır:
        # anonim kazıma keş, ağırbaş işler ağırbaş. Sabit değildir.
        self.launch_mode: str = STATE_MODE_AGIRBAS
        self.task: asyncio.Task[Any] | None = None

    def _record_event(self, kind: str, **payload: Any) -> None:
        """Append one bounded progress event and wake long-poll readers."""
        event: dict[str, Any] = {
            "index": self._event_seq,
            "kind": str(kind)[:_EVENT_STRING_MAX],
            "at": time.time(),
        }
        for key, value in payload.items():
            if key == "cookies" or key == "proxy":
                continue
            event[str(key)[:80]] = _event_scalar(value)
        self.events.append(event)
        self._event_seq += 1
        self.event_signal.set()

    # --- bounded queue primitives -----------------------------------------

    def _enqueue(self, url: str, depth: int) -> bool:
        if url in self._visited or url in self._queued:
            return False
        if len(self._queue) >= _MAX_QUEUE:
            self.dropped += 1
            return False
        self._queue.append((url, depth))
        self._queued.add(url)
        return True

    def _pop(self) -> tuple[str, int] | None:
        if not self._queue:
            return None
        url, depth = self._queue.popleft()
        self._queued.discard(url)
        self._visited.add(url)
        self.current_url = url
        self.current_depth = depth
        self.in_progress = True
        self._record_event(
            "page_started",
            state=self.state,
            currentUrl=url,
            currentDepth=depth,
            queued=len(self._queue),
        )
        return url, depth

    def _requeue_front(self, url: str, depth: int) -> None:
        if url in self._queued:
            return
        if len(self._queue) >= _MAX_QUEUE:
            # Keep the URL marked visited when no bounded retry slot exists;
            # the loss is explicit in ``dropped`` rather than silently making
            # the same URL eligible for a later duplicate fetch.
            self.dropped += 1
            return
        self._visited.discard(url)
        self._queue.appendleft((url, depth))
        self._queued.add(url)

    def _retry_bump(self, url: str, kind: str) -> int:
        entry = self._retries.setdefault(url, {"backoff": 0, "challenge": 0})
        entry[kind] = entry.get(kind, 0) + 1
        return entry[kind]

    def _retry_count(self, url: str, kind: str) -> int:
        return self._retries.get(url, {}).get(kind, 0)

    def _record_result(
        self,
        *,
        url: str,
        depth: int,
        status: str,
        title: str = "",
        text: str = "",
        links: list[str] | None = None,
        identity_hash: str | None = None,
        error: dict[str, str] | None = None,
        paused: bool = False,
        count_as_failure: bool = True,
    ) -> None:
        entry: dict[str, Any] = {
            "index": self._result_seq,
            "url": url[:_MAX_URL_LENGTH],
            "status": status,
            "depth": depth,
            "title": title[:_MAX_TITLE_LENGTH],
            "text": text[:_MAX_TEXT_LENGTH],
            "links": [link[:_MAX_URL_LENGTH] for link in (links or [])][:_MAX_RESULT_LINKS],
            "identityHash": identity_hash,
            "paused": paused,
            "fetchedAt": time.time(),
        }
        if error:
            entry["error"] = {
                "code": str(error.get("code") or "error")[:200],
                "message": str(error.get("message") or "")[:500],
            }
        self.results.append(entry)
        self._result_seq += 1
        if status == "success":
            self.succeeded += 1
        elif count_as_failure:
            self.failed += 1
        self._record_event(
            "page_finished",
            state=self.state,
            url=url,
            status=status,
            depth=depth,
            pagesFetched=self.pages_fetched,
            succeeded=self.succeeded,
            failed=self.failed,
            queued=len(self._queue),
        )

    def _record_failed(
        self, url: str, depth: int, code: str, message: str, *, paused: bool = False,
    ) -> None:
        self._record_result(
            url=url,
            depth=depth,
            status="failed",
            identity_hash=_active_identity_hash(),
            error={"code": code, "message": message},
            paused=paused,
        )


async def _transition(job: _CrawlJob, new_state: str, reason: str, **meta: Any) -> str | None:
    """Apply one guarded job state transition. Returns a JSON error string
    when the transition is invalid, else None. Never awaited while the caller
    already holds ``job.lock``."""
    async with job.lock:
        if new_state not in _ALLOWED_TRANSITIONS.get(job.state, frozenset()):
            return _json_error(
                "kahin_crawl",
                f"invalid state transition {job.state} -> {new_state}",
                "invalid_state_transition",
                jobId=job.job_id,
                current=job.state,
                requested=new_state,
            )
        job.state = new_state
        job.state_reason = reason
        job.state_seq += 1
        job.updated_at = time.time()
        if new_state in _TERMINAL:
            job.finished_at = job.updated_at
            job.in_progress = False
            job.current_url = None
            job.current_depth = None
        for key, value in meta.items():
            setattr(job, key, value)
        job._record_event(
            "state",
            state=new_state,
            reason=reason,
            stateSeq=job.state_seq,
            queued=len(job._queue),
            pagesFetched=job.pages_fetched,
            succeeded=job.succeeded,
            failed=job.failed,
            currentUrl=job.current_url,
        )
        return None


def _policy_payload(job: _CrawlJob) -> dict[str, Any]:
    config = job.config
    return {
        "seeds": len(config["seeds"]),
        "maxPages": config["maxPages"],
        "maxDepth": config["maxDepth"],
        "rotationEveryPages": config["rotationEveryPages"],
        "rotationEverySeconds": config["rotationEverySeconds"],
        "delayMs": config["delayMs"],
        "maxDurationSeconds": config["maxDurationSeconds"],
        "sameOrigin": config["sameOrigin"],
        "extract": config["extract"],
        "identity": job.identity,
        "maxQueue": _MAX_QUEUE,
        "backoff": {
            "baseSeconds": _BACKOFF_BASE_SECONDS,
            "capSeconds": _BACKOFF_CAP_SECONDS,
            "maxRetries": _BACKOFF_MAX_RETRIES,
        },
        "challengeMaxRetries": _CHALLENGE_MAX_RETRIES,
    }


async def _enter_backoff(job: _CrawlJob, *, retry_after: float | None, attempt: int) -> None:
    capped = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    bounded_retry_after = min(_BACKOFF_CAP_SECONDS, max(0.0, float(retry_after or 0.0)))
    wait = max(bounded_retry_after, capped)
    job.backoff_retry_after = float(retry_after) if retry_after is not None else None
    job.backoff_attempt = attempt
    job.backoff_until = time.time() + wait
    await _transition(job, "backing_off", "rate_limit_backoff")


async def _wait_runnable(job: _CrawlJob) -> bool:
    """Wait until the job may fetch the next URL. Returns False when the job
    reached a terminal state or a stop was requested."""
    while True:
        async with job.lock:
            current = job.state
        if current in _TERMINAL or job.cancel_requested:
            return False
        if current == "paused":
            # Explicit resume only; challenge pauses never self-resolve.
            await job.resume_event.wait()
            continue
        if current == "backing_off":
            remaining = (job.backoff_until or 0.0) - time.time()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 1.0))
                continue
            await _transition(job, "running", "backoff_expired")
            continue
        return True


async def _inter_iteration_delay(job: _CrawlJob) -> None:
    delay = float(job.config.get("delayMs", 0)) / 1000.0
    if delay <= 0:
        return
    deadline = time.time() + delay
    while True:
        if job.cancel_requested:
            return
        async with job.lock:
            paused = job.state == "paused"
        if paused:
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.5))


async def _pin_crawl_tab(job: _CrawlJob) -> str | None:
    """Keep every crawl operation on the tab captured at job start."""
    from kahin.the_twins.mirage import Mirage  # noqa: PLC0415

    engine = state._current_engine
    if not isinstance(engine, Mirage):
        return None
    target_id = job.tab_id
    if target_id is None:
        try:
            page = await asyncio.wait_for(engine.ensure_page(), timeout=_PAGE_SETTLE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - caller returns structured state
            return _json_error("kahin_crawl", f"crawl tab is unavailable: {exc}", "crawl_tab_unavailable")
        target_id = page.get("targetId") if isinstance(page, dict) else None
        if isinstance(target_id, str):
            job.tab_id = target_id
            return None
    if not isinstance(target_id, str) or target_id not in engine._sessions:
        return _json_error(
            "kahin_crawl",
            "the crawl tab was closed or detached",
            "crawl_tab_lost",
            tabId=target_id,
        )
    if engine._current_target != target_id:
        try:
            await asyncio.wait_for(engine.switch_page(target_id), timeout=_PAGE_SETTLE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - do not silently crawl another tab
            return _json_error("kahin_crawl", f"could not reselect the crawl tab: {exc}", "crawl_tab_unavailable")
    return None


async def _engine_gate(job: _CrawlJob, url: str, depth: int) -> bool:
    """Health gate before a page fetch. Returns True when the worker may
    proceed; False when the URL was already accounted for (requeued, recorded
    failed, or the job reached a terminal state)."""
    from kahin.tools._common import _require_engine  # noqa: PLC0415

    err = await _require_engine()
    if err is None:
        tab_error = await _pin_crawl_tab(job)
        if tab_error:
            payload = _loads(tab_error) or {}
            code = str(payload.get("code") or "crawl_tab_unavailable")
            message = str(payload.get("error") or tab_error)[:500]
            job.last_error = {"code": code, "message": message}
            job._record_failed(url, depth, code, message)
            await _transition(job, "failed", code)
            return False
        return True
    payload = _loads(err) or {}
    code = str(payload.get("code") or "engine_unavailable")
    message = str(payload.get("error") or err)[:500]
    if code in {"engine_dead", "engine_degraded", "engine_health_timeout"}:
        if not await _recover_engine(job, url=url, depth=depth):
            return False
        job._requeue_front(url, depth)
        return False
    if code == "engine_unavailable":
        job.last_error = {"code": code, "message": message}
        job._record_failed(url, depth, code, "engine was stopped while the crawl was running")
        await _transition(job, "failed", code)
        return False
    # Other transient health errors use bounded backoff, then retry the same
    # URL without opening another browser.
    attempt = job._retry_bump(url, "backoff")
    if attempt > _BACKOFF_MAX_RETRIES:
        job._record_failed(url, depth, code, message)
        return False
    await _enter_backoff(job, retry_after=None, attempt=attempt)
    job._requeue_front(url, depth)
    return False


async def _handle_challenge(job: _CrawlJob, url: str, depth: int, challenge: dict[str, Any]) -> str:
    """Apply the challenge contract. Returns ``paused`` (job paused, explicit
    resume required), ``requeued`` (rate-limit backoff, URL retried later) or
    ``failed_page`` (retry budget exhausted, URL recorded failed)."""
    kind = str(challenge.get("kind") or "unknown")
    retry_after = challenge.get("retryAfterSeconds")
    if retry_after is not None and not isinstance(retry_after, (int, float)):
        retry_after = None
    job.last_challenge = {
        "kind": kind,
        "action": str(challenge.get("action") or "continue")[:200],
        "retryAfterSeconds": retry_after,
        "detectedAt": time.time(),
        "url": url[:_MAX_URL_LENGTH],
    }
    if kind in ("captcha", "access_denied"):
        attempt = job._retry_bump(url, "challenge")
        if attempt > _CHALLENGE_MAX_RETRIES:
            job._record_failed(
                url,
                depth,
                f"challenge_{kind}_unresolved",
                f"{kind} persisted after {attempt} explicit resumes",
            )
            return "failed_page"
        job._record_result(
            url=url,
            depth=depth,
            status="failed",
            identity_hash=_active_identity_hash(),
            error={"code": f"challenge_{kind}", "message": "paused until explicit resume"},
            paused=True,
            count_as_failure=False,
        )
        job._requeue_front(url, depth)
        job.resume_event.clear()
        await _transition(job, "paused", f"challenge:{kind}")
        return "paused"
    # rate_limit (and unknown detections): Retry-After honored, capped
    # exponential backoff applied, never rotated past as an evasion.
    attempt = job._retry_bump(url, "backoff")
    if attempt > _BACKOFF_MAX_RETRIES:
        job._record_failed(
            url,
            depth,
            "rate_limited",
            f"rate limit persisted after {attempt} backoff retries",
        )
        return "failed_page"
    await _enter_backoff(job, retry_after=float(retry_after) if retry_after is not None else None, attempt=attempt)
    job._requeue_front(url, depth)
    return "requeued"


async def _extract_page(job: _CrawlJob, url: str, depth: int) -> dict[str, Any] | None:
    """Bounded DOM/title/link extraction on the current tab.

    Failure evidence is left on the job for ``_process_url`` to classify as a
    transport recovery or an ordinary page failure; this prevents recording a
    failed result before a crashed engine gets one chance to retry the URL.
    """
    from kahin.tools._common import _mirage_eval_result  # noqa: PLC0415

    raw = await _mirage_eval_result(_EXTRACT_JS)
    if isinstance(raw, str):
        payload = _loads(raw) or {}
        code = str(payload.get("code") or "extraction_failed")[:200]
        message = str(payload.get("error") or raw)[:500]
        job.last_error = {"code": code, "message": message}
        return None
    if not isinstance(raw, dict) or raw.get("exceptionDetails"):
        job.last_error = {"code": "extraction_javascript_error", "message": "page extraction raised"}
        return None
    result = raw.get("result") or {}
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        job.last_error = {"code": "extraction_invalid_payload", "message": "extraction returned an invalid payload"}
        return None
    return {
        "title": str(value.get("title") or "")[:_MAX_TITLE_LENGTH],
        "text": str(value.get("text") or "")[:_MAX_TEXT_LENGTH],
        "links": [
            str(link)[:_MAX_URL_LENGTH]
            for link in (value.get("links") or [])
            if isinstance(link, str)
        ][:_MAX_RESULT_LINKS],
        "url": str(value.get("url") or url)[:_MAX_URL_LENGTH],
    }


def _rotation_due(job: _CrawlJob) -> bool:
    config = job.config
    if job.pages_since_rotation >= int(config["rotationEveryPages"]):
        return True
    base = job.last_rotation_at or job.started_at or job.created_at
    return time.time() - base >= float(config["rotationEverySeconds"])


async def _stop_for_rotation(job: _CrawlJob) -> bool:
    """Stop the engine for rotation/recovery. A dead engine reference is
    tolerated (``browser_start`` reaps it); a live engine that refuses to
    stop fails the rotation."""
    from kahin.tools.pilot import browser_stop as pilot_browser_stop  # noqa: PLC0415

    raw = await pilot_browser_stop()
    payload = _loads(raw) or {}
    if not payload.get("error"):
        return True
    code = str(payload.get("code") or "engine_stop_failed")
    if code == "engine_unavailable":
        return True
    if code == "engine_stop_failed":
        engine = state._current_engine
        if engine is not None:
            try:
                health = await asyncio.wait_for(engine.health(), timeout=_ENGINE_PROBE_TIMEOUT)
                if bool(health.get("alive")) or health.get("state") not in ("dead", "degraded"):
                    job.last_error = {
                        "code": code,
                        "message": f"engine refused to stop during rotation: {payload.get('error')}",
                    }
                    return False
            except Exception:  # noqa: BLE001 - treat as dead; browser_start reaps it
                pass
        return True
    job.last_error = {"code": code, "message": str(payload.get("error"))[:500]}
    return False


async def _start_for_rotation(
    job: _CrawlJob,
    *,
    identity: str | dict[str, Any] | None,
    previous_hash: str | None,
    require_different: bool,
) -> bool:
    from kahin.tools.pilot import browser_start as pilot_browser_start  # noqa: PLC0415

    kwargs: dict[str, Any] = {
        "engine": "mirage",
        "headless": bool(job.launch_headless),
        # Durum modu kahin_browser_start'ta zorunludur (spec §5). Crawler modu
        # işe göre seçer: motor hangi modda başlatıldıysa rotasyon da o modda
        # sürer. Anonim kazıma keş (proxy'lerle), hesaplı kazıma ağırbaş.
        "mode": job.launch_mode,
    }
    if job.launch_mode == STATE_MODE_KES:
        kwargs["ephemeral_ack"] = True
    if isinstance(job.launch_proxy, str) and job.launch_proxy:
        kwargs["proxy"] = job.launch_proxy
    if identity is not None:
        kwargs["identity"] = identity
    raw = await pilot_browser_start(**kwargs)
    payload = _loads(raw) or {}
    if payload.get("error"):
        job.last_error = {
            "code": str(payload.get("code") or "engine_start_failed"),
            "message": str(payload.get("error") or raw)[:500],
        }
        return False
    engine = state._current_engine
    new_hash = getattr(engine, "_identity_hash", None) if engine is not None else None
    if require_different and (not previous_hash or not new_hash or new_hash == previous_hash):
        job.last_error = {
            "code": "rotation_identity_unchanged",
            "message": "restarted engine reported the same identity hash; refusing to continue",
        }
        return False
    if engine is not None:
        try:
            page = await asyncio.wait_for(engine.ensure_page(), timeout=_PAGE_SETTLE_TIMEOUT)
            if isinstance(page, dict) and isinstance(page.get("targetId"), str):
                job.tab_id = page["targetId"]
        except Exception:  # noqa: BLE001 - tab pinning is best effort
            pass
    return True


async def _rotate_engine(job: _CrawlJob, *, reason: str) -> bool:
    """Stop/restart while preserving queue/result state.

    The captured identity is the configuration input, but every restart must
    report a different effective fingerprint digest.
    """
    async with job.rotation_gate:
        async with job.lock:
            if job.state != "running" or job.cancel_requested:
                return not job.cancel_requested
        transition_err = await _transition(job, "rotating", reason)
        if transition_err:
            return False
        job.rotation_reason = reason
        job.rotation_started_at = time.time()
        previous_hash = _active_identity_hash()
        identity = job.identity if reason == "rotation" else job.launch_identity
        if not await _stop_for_rotation(job):
            await _transition(job, "failed", "rotation_stop_failed")
            return False
        # A stop request may arrive while the old engine is being reaped. Do
        # not start a replacement browser after the caller has cancelled the
        # job; the stop tool will publish the terminal state after this gate.
        if job.cancel_requested:
            return False
        if not await _start_for_rotation(
            job, identity=identity, previous_hash=previous_hash, require_different=True,
        ):
            await _transition(job, "failed", "rotation_start_failed")
            return False
        if job.cancel_requested:
            await _stop_for_rotation(job)
            return False
        job.rotations += 1
        job.pages_since_rotation = 0
        job.last_rotation_at = time.time()
        transition_err = await _transition(job, "running", f"{reason}_done")
        return transition_err is None


async def _recover_engine(job: _CrawlJob, *, url: str, depth: int) -> bool:
    """Single crash-recovery restart. A second death fails the job."""
    if job.recovery_attempts >= 1:
        job.last_error = {
            "code": "engine_crashed",
            "message": "engine died twice; the single recovery restart was already used",
        }
        job._record_failed(url, depth, "engine_crashed", "engine died twice; single recovery restart already used")
        await _transition(job, "failed", "engine_crashed")
        return False
    job.recovery_attempts += 1
    return await _rotate_engine(job, reason="recovery")


def _looks_like_engine_failure(code: str, message: str) -> bool:
    lowered = f"{code} {message}".lower()
    return (
        code in _ENGINE_FAILURE_CODES
        or any(marker in lowered for marker in (
            "sidecar exited", "mirage is dead", "transport closed",
            "stdin write timeout", "connection lost", "health probe",
        ))
    )


async def _recover_failed_page(
    job: _CrawlJob,
    *,
    url: str,
    depth: int,
    code: str,
    message: str,
    force_health_probe: bool = False,
) -> str:
    """Give a suspect page one bounded engine recovery before failing it."""
    if not force_health_probe and not _looks_like_engine_failure(code, message):
        return "not_needed"
    health = await _engine_health_payload()
    if health is not None and health.get("alive"):
        return "not_needed"
    if job.cancel_requested:
        return "terminal"
    if await _recover_engine(job, url=url, depth=depth):
        job._requeue_front(url, depth)
        return "requeued"
    return "terminal"


async def _process_url(job: _CrawlJob, url: str, depth: int) -> None:
    if not await _engine_gate(job, url, depth):
        return
    from kahin.tools.pilot import navigate as pilot_navigate  # noqa: PLC0415

    raw = await pilot_navigate(url=url, wait_until="load", timeout=_NAVIGATE_TIMEOUT)
    nav = _loads(raw) or {}
    if nav.get("error"):
        code = str(nav.get("code") or "navigation_failed")[:200]
        message = str(nav.get("error"))[:500]
        outcome = await _recover_failed_page(job, url=url, depth=depth, code=code, message=message)
        if outcome != "not_needed":
            return
        job.last_error = {"code": code, "message": message}
        job._record_failed(url, depth, code, message)
        return

    # Juggler reports the load lifecycle just before the page's final DOM
    # turn is observable on some fast local/HTTP responses. Give the document
    # one bounded scheduling window before classifying its body as a
    # challenge; otherwise a previous 429 page can be mistaken for the fresh
    # 200 retry and consume the retry budget.
    await asyncio.sleep(_CHALLENGE_SETTLE_SECONDS)
    from kahin.tools.agent_mirage import challenge_status  # noqa: PLC0415

    challenge_raw = await challenge_status()
    challenge = _loads(challenge_raw) or {}
    if challenge.get("error"):
        code = str(challenge.get("code") or "challenge_probe_failed")[:200]
        message = str(challenge.get("error"))[:500]
        outcome = await _recover_failed_page(job, url=url, depth=depth, code=code, message=message)
        if outcome != "not_needed":
            return
        job.last_error = {"code": code, "message": message}
        job._record_failed(url, depth, code, message)
        return
    if challenge.get("detected") and challenge.get("kind") == "rate_limit":
        # A navigation can expose the previous document's 429 body for one
        # event-loop turn while its fresh response is being committed. A
        # single bounded recheck avoids consuming another retry slot for that
        # stale observation, while a real rate-limit page remains detected.
        await asyncio.sleep(_CHALLENGE_RECHECK_SECONDS)
        rechecked = _loads(await challenge_status()) or {}
        if not rechecked.get("error") and not rechecked.get("detected"):
            challenge = rechecked
    if challenge.get("detected"):
        decision = await _handle_challenge(job, url, depth, challenge)
        if decision in ("paused", "requeued", "failed_page"):
            return

    if not job.config.get("extract", True):
        payload: dict[str, Any] = {"url": url, "title": "", "text": "", "links": []}
    else:
        payload = await _extract_page(job, url, depth)
        if payload is None:
            error = job.last_error or {
                "code": "extraction_failed",
                "message": "page extraction failed",
            }
            outcome = await _recover_failed_page(
                job,
                url=url,
                depth=depth,
                code=str(error.get("code") or "extraction_failed"),
                message=str(error.get("message") or "page extraction failed"),
            )
            if outcome != "not_needed":
                return
            job._record_failed(
                url,
                depth,
                str(error.get("code") or "extraction_failed"),
                str(error.get("message") or "page extraction failed"),
            )
            return

    links = [link for link in payload.get("links", []) if isinstance(link, str)]
    if job.config.get("sameOrigin", True):
        origin = _origin_of(url)
        links = [link for link in links if _origin_of(link) == origin]
    for link in links[:_MAX_RESULT_LINKS]:
        canon = _canonical_url(link)
        if canon is None or depth + 1 > int(job.config["maxDepth"]):
            continue
        if job._enqueue(canon, depth + 1):
            job.links_discovered += 1

    job._record_result(
        url=url,
        depth=depth,
        status="success",
        title=payload.get("title", ""),
        text=payload.get("text", ""),
        links=links,
        identity_hash=_active_identity_hash(),
    )
    job.pages_since_rotation += 1

    # Rotation happens only after the page result is written to the ledger,
    # and only when there is still queued work worth rotating for.
    if job._queue and _rotation_due(job):
        await _rotate_engine(job, reason="rotation")


async def _run_job(job: _CrawlJob) -> None:
    """Background crawl loop. All state transitions go through the single
    per-job lock; queue/result/job state survives engine restarts."""
    try:
        while True:
            if job.cancel_requested:
                await _transition(job, "cancelled", "user_stop")
                return
            elapsed = time.time() - (job.started_at or job.created_at)
            if elapsed >= float(job.config["maxDurationSeconds"]):
                job.dropped += len(job._queue)
                job._queue.clear()
                job._queued.clear()
                await _transition(job, "completed", "duration_limit")
                return
            if not await _wait_runnable(job):
                return
            if (
                job.pages_fetched >= int(job.config["maxPages"])
                and job._queue
                and job._queue[0][0] not in job._page_budget_urls
            ):
                job.dropped += len(job._queue)
                job._queue.clear()
                job._queued.clear()
                await _transition(job, "completed", "page_limit")
                return
            item = job._pop()
            if item is None:
                await _transition(job, "completed", "queue_drained")
                return
            url, depth = item
            job.attempts += 1
            if url not in job._page_budget_urls:
                job._page_budget_urls.add(url)
                job.pages_fetched += 1
            try:
                await asyncio.wait_for(
                    _process_url(job, url, depth),
                    timeout=_PAGE_OPERATION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                message = f"page operation exceeded {_PAGE_OPERATION_TIMEOUT:.0f}s"
                job.last_error = {"code": "page_timeout", "message": message}
                outcome = await _recover_failed_page(
                    job,
                    url=url,
                    depth=depth,
                    code="page_timeout",
                    message=message,
                    force_health_probe=True,
                )
                if outcome == "not_needed":
                    job._record_failed(url, depth, "page_timeout", message)
            finally:
                job.in_progress = False
                job.current_url = None
                job.current_depth = None
            await _inter_iteration_delay(job)
    except asyncio.CancelledError:
        await _transition(job, "cancelled", "user_stop")
        raise
    except Exception as exc:  # noqa: BLE001 - a background task must never die silently
        logger.exception("crawl job %s failed", job.job_id)
        job.last_error = {"code": "internal_error", "message": str(exc)[:500]}
        await _transition(job, "failed", "internal_error")
    finally:
        async with state._crawl_jobs_lock:
            if state._active_crawl_id == job.job_id and job.state in _TERMINAL:
                state._active_crawl_id = None


def _resolve_job(tool: str, job_id: str | None) -> tuple[_CrawlJob | None, str | None]:
    """Resolve an explicit or active job. Returns (job, None) or
    (None, structured JSON error)."""
    if job_id is not None:
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            return None, _json_error(
                tool, "jobId must be a non-empty string of at most 128 characters",
                "invalid_argument", field="jobId",
            )
        job = state._crawl_jobs.get(job_id)
        if job is None:
            return None, _json_error(tool, f"unknown crawl job {job_id}", "job_not_found", jobId=job_id)
        return job, None
    active_id = state._active_crawl_id
    if active_id is None:
        return None, _json_error(
            tool, "no active crawl job; pass jobId", "job_not_found",
            hint="start one with kahin_crawl_start",
        )
    job = state._crawl_jobs.get(active_id)
    if job is None:
        return None, _json_error(tool, "active crawl job registry entry is missing", "job_not_found")
    return job, None


async def _engine_health_payload() -> dict[str, Any] | None:
    engine = state._current_engine
    if engine is None:
        return None
    try:
        health = await asyncio.wait_for(engine.health(), timeout=_ENGINE_PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - status degrades, never raises
        return {"alive": False, "state": "degraded", "error": str(exc)[:300]}
    if not isinstance(health, dict):
        return None
    return {
        "alive": bool(health.get("alive")),
        "state": str(health.get("state") or ("alive" if health.get("alive") else "unknown")),
        "pid": health.get("pid"),
    }


def _status_payload(job: _CrawlJob, engine_health: dict[str, Any] | None) -> dict[str, Any]:
    now = time.time()
    queued = len(job._queue)
    in_progress = job.in_progress and job.state not in _TERMINAL
    base = job.last_rotation_at or job.started_at or job.created_at
    rotation_seconds_left = max(0, int(float(job.config["rotationEverySeconds"]) - (now - base)))
    return {
        "jobId": job.job_id,
        "state": job.state,
        "stateReason": job.state_reason,
        "engine": "mirage",
        "tabId": job.tab_id,
        "startedAt": job.started_at,
        "updatedAt": job.updated_at,
        "finishedAt": job.finished_at,
        "currentUrl": job.current_url if in_progress else None,
        "currentDepth": job.current_depth if in_progress else None,
        "currentIdentityHash": _active_identity_hash(),
        "counters": {
            "queued": queued,
            "visited": len(job._visited),
            "pagesFetched": job.pages_fetched,
            "attempts": job.attempts,
            "succeeded": job.succeeded,
            "failed": job.failed,
            "inProgress": in_progress,
            "dropped": job.dropped,
            "linksDiscovered": job.links_discovered,
            "pagesSinceRotation": job.pages_since_rotation,
            "rotations": job.rotations,
            "recoveryAttempts": job.recovery_attempts,
        },
        "queueSize": queued,
        "backoff": {
            "active": job.state == "backing_off",
            "retryAfterSeconds": job.backoff_retry_after,
            "until": job.backoff_until,
            "attempt": job.backoff_attempt,
        },
        "challenge": job.last_challenge,
        "lastError": job.last_error,
        "rotation": {
            "lastAt": job.last_rotation_at,
            "reason": job.rotation_reason,
            "nextInPages": max(0, int(job.config["rotationEveryPages"]) - job.pages_since_rotation),
            "nextInSeconds": rotation_seconds_left,
        },
        "engineHealth": engine_health,
        "hint": "pause/resume/stop via kahin_crawl_pause/kahin_crawl_resume/kahin_crawl_stop",
    }


def _bounded_results_response(job: _CrawlJob, *, cursor: int, limit: int) -> str:
    ledger = list(job.results)
    oldest_cursor = (
        int(ledger[0].get("index"))
        if ledger and isinstance(ledger[0].get("index"), int)
        else job._result_seq
    )
    # The deque is intentionally bounded. Once old entries are evicted, a
    # position-based cursor would point at a different result (or appear
    # exhausted) even though newer entries exist. Cursors are therefore the
    # monotonic result indexes emitted by _record_result.
    start = max(cursor, oldest_cursor)
    cursor_reset = cursor < oldest_cursor
    selected = [
        entry for entry in ledger
        if isinstance(entry.get("index"), int) and int(entry["index"]) >= start
    ][:limit]
    next_cursor = (
        int(selected[-1]["index"]) + 1
        if selected and isinstance(selected[-1].get("index"), int)
        else start
    )
    has_more = any(
        isinstance(entry.get("index"), int) and int(entry["index"]) >= next_cursor
        for entry in ledger
    )
    payload: dict[str, Any] = {
        "jobId": job.job_id,
        "state": job.state,
        "cursor": start,
        "oldestCursor": oldest_cursor,
        "cursorReset": cursor_reset,
        "limit": limit,
        "count": len(selected),
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "truncated": False,
        "results": selected,
    }
    raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
    if len(raw) <= _RESPONSE_MAX_BYTES:
        return raw
    # Deterministic shrink: halve per-entry link lists first, then drop the
    # tail entries until the response fits the hard byte budget.
    trimmed: list[dict[str, Any]] = []
    for entry in selected:
        copy = dict(entry)
        links = copy.get("links")
        if isinstance(links, list):
            copy["links"] = links[: max(1, len(links) // 2)]
        trimmed.append(copy)
    while trimmed:
        payload["results"] = trimmed
        payload["count"] = len(trimmed)
        last_index = trimmed[-1].get("index") if trimmed else None
        trimmed_next = int(last_index) + 1 if isinstance(last_index, int) else start
        payload["nextCursor"] = trimmed_next
        payload["hasMore"] = any(
            isinstance(entry.get("index"), int) and int(entry["index"]) >= trimmed_next
            for entry in ledger
        )
        raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
        if len(raw) <= _RESPONSE_MAX_BYTES:
            payload["truncated"] = True
            return raw
        trimmed = trimmed[:-1]
    payload = {
        "jobId": job.job_id,
        "state": job.state,
        "cursor": start,
        "oldestCursor": oldest_cursor,
        "cursorReset": cursor_reset,
        "limit": limit,
        "count": 0,
        "nextCursor": start,
        "hasMore": has_more,
        "truncated": True,
        "results": [],
        "error": "result window exceeds the response byte budget",
        "code": "response_too_large",
    }
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


# --- MCP tools ------------------------------------------------------------


@mcp.tool(name="kahin_crawl_start", annotations=_RW)
async def crawl_start(
    seeds: list[str],
    maxPages: int = 100,
    maxDepth: int = 3,
    rotationEveryPages: int = 20,
    rotationEverySeconds: int = 900,
    delayMs: int = 1000,
    maxDurationSeconds: int = 3600,
    sameOrigin: bool = True,
    extract: bool = True,
    identity: str | None = None,
) -> str:
    """Start a background crawl job on the existing Camoufox/Mirage engine
    and its current tab. No second browser or tab is opened. Returns
    {jobId, state, engine, tabId, policy}; a second concurrent job returns a
    structured crawl_busy error."""
    tool = "kahin_crawl_start"
    async with _healer_ref.safe(tool, seeds=len(seeds) if isinstance(seeds, list) else -1):
        max_pages, err = _bounded_int(maxPages, tool=tool, field="maxPages", minimum=1, maximum=10_000, default=100)
        if err:
            return err
        max_depth, err = _bounded_int(maxDepth, tool=tool, field="maxDepth", minimum=0, maximum=32, default=3)
        if err:
            return err
        rotation_pages, err = _bounded_int(
            rotationEveryPages, tool=tool, field="rotationEveryPages", minimum=1, maximum=1_000, default=20,
        )
        if err:
            return err
        rotation_seconds, err = _bounded_int(
            rotationEverySeconds, tool=tool, field="rotationEverySeconds", minimum=60, maximum=86_400, default=900,
        )
        if err:
            return err
        delay, err = _bounded_int(delayMs, tool=tool, field="delayMs", minimum=0, maximum=60_000, default=1000)
        if err:
            return err
        duration, err = _bounded_int(
            maxDurationSeconds, tool=tool, field="maxDurationSeconds", minimum=60, maximum=86_400, default=3600,
        )
        if err:
            return err
        same_origin, err = _bounded_bool(sameOrigin, tool=tool, field="sameOrigin", default=True)
        if err:
            return err
        extract_value, err = _bounded_bool(extract, tool=tool, field="extract", default=True)
        if err:
            return err
        if not isinstance(seeds, list):
            return _json_error(tool, "seeds must be a list of http/https URLs", "invalid_argument", field="seeds")
        if len(seeds) > _MAX_SEEDS:
            return _json_error(
                tool, f"seeds exceeds the maximum of {_MAX_SEEDS} URLs", "too_many_seeds",
                field="seeds", maximum=_MAX_SEEDS, received=len(seeds),
            )
        canonical_seeds: list[str] = []
        for seed in seeds:
            if not isinstance(seed, str):
                return _json_error(
                    tool, "every seed must be a string http/https URL", "invalid_argument", field="seeds",
                )
            canon = _canonical_url(seed)
            if canon is None:
                return _json_error(
                    tool, f"invalid seed URL: {seed[:200]}", "invalid_argument", field="seeds", received=seed[:200],
                )
            if canon not in canonical_seeds:
                canonical_seeds.append(canon)
        if not canonical_seeds:
            return _json_error(tool, "at least one valid seed URL is required", "invalid_argument", field="seeds")
        identity_name: str | None = None
        if identity is not None:
            if not isinstance(identity, str) or not identity or len(identity) > _MAX_IDENTITY_NAME_LENGTH:
                return _json_error(
                    tool,
                    f"identity must be a saved identity name of at most {_MAX_IDENTITY_NAME_LENGTH} characters",
                    "invalid_argument",
                    field="identity",
                )
            identity_name = identity
            from kahin.tools.agent_mirage import _identity_path  # noqa: PLC0415

            identity_path = _identity_path(identity_name)
            if identity_path is None or not identity_path.is_file():
                return _json_error(
                    tool,
                    f"unknown identity: {identity_name!r}",
                    "invalid_argument",
                    field="identity",
                )

        async with state._crawl_jobs_lock:
            active_id = state._active_crawl_id
            if active_id is not None:
                active_job = state._crawl_jobs.get(active_id)
                if active_job is not None and active_job.state not in _TERMINAL:
                    return _json_error(
                        tool,
                        "another crawl job is already active",
                        "crawl_busy",
                        jobId=active_id,
                        state=active_job.state,
                    )

        from kahin.the_twins.mirage import Mirage  # noqa: PLC0415
        from kahin.tools._common import _require_engine  # noqa: PLC0415

        engine = state._current_engine
        if not isinstance(engine, Mirage):
            return _json_error(
                tool,
                "the crawler requires the Camoufox/Mirage engine",
                "capability_requires_mirage",
                hint="start it with kahin_browser_start(engine='mirage')",
            )
        engine_error = await _require_engine()
        if engine_error:
            return engine_error
        active_identity_name = getattr(engine, "_identity_name", None)
        if identity_name is not None and active_identity_name != identity_name:
            return _json_error(
                tool,
                "active Mirage engine uses a different identity",
                "engine_config_conflict",
                requested={"identity": identity_name},
                active={"identity": active_identity_name or None},
                hint="Stop/start Mirage with the requested identity before starting the crawl.",
            )
        try:
            page = await asyncio.wait_for(engine.ensure_page(), timeout=_PAGE_SETTLE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - public tool returns JSON
            return _json_error(tool, f"no usable tab on the active engine: {exc}", "session_unavailable")
        tab_id = page.get("targetId") if isinstance(page, dict) else None

        job_id = f"crawl-{uuid.uuid4().hex[:12]}"
        config: dict[str, Any] = {
            "seeds": canonical_seeds,
            "maxPages": max_pages,
            "maxDepth": max_depth,
            "rotationEveryPages": rotation_pages,
            "rotationEverySeconds": rotation_seconds,
            "delayMs": delay,
            "maxDurationSeconds": duration,
            "sameOrigin": same_origin,
            "extract": extract_value,
        }
        job = _CrawlJob(job_id, config, canonical_seeds)
        job.tab_id = tab_id if isinstance(tab_id, str) else None
        job.identity = identity_name
        job.launch_identity = (
            identity_name
            or getattr(engine, "_identity_name", None)
            or _snapshot_identity_config(getattr(engine, "_identity_config", None))
        )
        launch_policy = getattr(engine, "_launch_policy", None)
        job.launch_headless = bool(
            launch_policy.get("headless", True)
            if isinstance(launch_policy, dict)
            else True
        )
        launch_proxy = getattr(engine, "_proxy_url", None)
        job.launch_proxy = (
            launch_proxy[:_MAX_PROXY_LENGTH]
            if isinstance(launch_proxy, str) and len(launch_proxy) <= _MAX_PROXY_LENGTH
            else None
        )
        # Rotasyon, motorun gerçek durum modunu sürdürür; mod işe göre seçilir.
        from kahin.tools.pilot import _engine_state_mode  # noqa: PLC0415

        job.launch_mode = _engine_state_mode(engine) or STATE_MODE_AGIRBAS

        async with state._crawl_jobs_lock:
            # Re-check while holding the registration lock. The first check
            # above is only an early rejection; two concurrent start calls
            # must not both pass it and then overwrite _active_crawl_id.
            active_id = state._active_crawl_id
            active_job = state._crawl_jobs.get(active_id) if active_id else None
            if active_job is not None and active_job.state not in _TERMINAL:
                return _json_error(
                    tool,
                    "another crawl job is already active",
                    "crawl_busy",
                    jobId=active_id,
                    state=active_job.state,
                )
            # Bounded retention: prune the oldest terminal jobs so the
            # in-process registry cannot grow without limit.
            terminal_ids = [
                jid for jid, existing in state._crawl_jobs.items()
                if existing.state in _TERMINAL
            ]
            if len(terminal_ids) > _MAX_RETAINED_JOBS:
                oldest = sorted(
                    terminal_ids,
                    key=lambda jid: (state._crawl_jobs[jid].finished_at or 0.0),
                )[: len(terminal_ids) - _MAX_RETAINED_JOBS]
                for jid in oldest:
                    del state._crawl_jobs[jid]
            state._crawl_jobs[job_id] = job
            state._active_crawl_id = job_id

        job.started_at = time.time()
        transition_err = await _transition(job, "running", "started")
        if transition_err:
            async with state._crawl_jobs_lock:
                state._crawl_jobs.pop(job_id, None)
                if state._active_crawl_id == job_id:
                    state._active_crawl_id = None
            return transition_err
        task = asyncio.create_task(_run_job(job))
        job.task = task
        return orjson.dumps(
            {
                "jobId": job_id,
                "state": job.state,
                "engine": "mirage",
                "tabId": job.tab_id,
                "policy": _policy_payload(job),
                "hint": "observe with kahin_crawl_status; stop with kahin_crawl_stop",
            },
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_crawl_status", annotations=_RO)
async def crawl_status(jobId: str | None = None) -> str:
    """Job state, bounded counters, queue size, current URL, current identity
    hash, rotation/recovery counts, last challenge/error and active engine
    health. Never raises; engine health degrades to a neutral payload."""
    tool = "kahin_crawl_status"
    async with _healer_ref.safe(tool, jobId=jobId):
        job, err = _resolve_job(tool, jobId)
        if err:
            return err
        health = await _engine_health_payload()
        return orjson.dumps(_status_payload(job, health), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_crawl_events", annotations=_RO)
async def crawl_events(
    jobId: str | None = None,
    cursor: int = 0,
    limit: int = 100,
    waitMs: int = 0,
) -> str:
    """Read live crawl progress deltas with an optional bounded long-poll."""
    tool = "kahin_crawl_events"
    async with _healer_ref.safe(tool, jobId=jobId, cursor=cursor, limit=limit, waitMs=waitMs):
        job, err = _resolve_job(tool, jobId)
        if err:
            return err
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return _json_error(
                tool, "cursor must be a non-negative integer", "invalid_argument", field="cursor",
            )
        limit_value, limit_err = _bounded_int(
            limit,
            tool=tool,
            field="limit",
            minimum=1,
            maximum=_MAX_RESULTS_PER_RESPONSE,
            default=100,
        )
        if limit_err:
            return limit_err
        wait_value, wait_err = _bounded_int(
            waitMs,
            tool=tool,
            field="waitMs",
            minimum=0,
            maximum=_EVENT_WAIT_MAX_MS,
            default=0,
        )
        if wait_err:
            return wait_err
        assert limit_value is not None and wait_value is not None
        return await _wait_for_events(
            job,
            cursor=cursor,
            limit=limit_value,
            wait_ms=wait_value,
        )


@mcp.tool(name="kahin_crawl_results", annotations=_RO)
async def crawl_results(jobId: str, cursor: int = 0, limit: int = 100) -> str:
    """Bounded result window with an opaque monotonic cursor. At most 100
    records per response; pass the returned nextCursor to page further."""
    tool = "kahin_crawl_results"
    async with _healer_ref.safe(tool, jobId=jobId, cursor=cursor, limit=limit):
        job, err = _resolve_job(tool, jobId)
        if err:
            return err
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return _json_error(
                tool, "cursor must be a non-negative integer", "invalid_argument", field="cursor",
            )
        limit_value, limit_err = _bounded_int(
            limit, tool=tool, field="limit", minimum=1, maximum=_MAX_RESULTS_PER_RESPONSE, default=100,
        )
        if limit_err:
            return limit_err
        return _bounded_results_response(job, cursor=cursor, limit=limit_value)


@mcp.tool(name="kahin_crawl_pause", annotations=_RW)
async def crawl_pause(jobId: str | None = None) -> str:
    """Pause the job. Challenge pauses are always explicit: after a
    CAPTCHA/access-denied the job never resumes on its own."""
    tool = "kahin_crawl_pause"
    async with _healer_ref.safe(tool, jobId=jobId):
        job, err = _resolve_job(tool, jobId)
        if err:
            return err
        async with job.lock:
            current = job.state
        if current in _TERMINAL:
            return _json_error(
                tool, f"job is already {current}; it cannot be paused", "job_terminal",
                jobId=job.job_id, state=current,
            )
        if current == "paused":
            return orjson.dumps(
                {"jobId": job.job_id, "state": "paused", "paused": True, "reason": job.state_reason},
                option=orjson.OPT_INDENT_2,
            ).decode()
        job.resume_event.clear()
        transition_err = await _transition(job, "paused", "user_pause")
        if transition_err:
            return transition_err
        return orjson.dumps(
            {"jobId": job.job_id, "state": "paused", "paused": True, "reason": "user_pause"},
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_crawl_resume", annotations=_RW)
async def crawl_resume(jobId: str | None = None) -> str:
    """Explicitly resume a paused job. Resume never solves a CAPTCHA and
    never rotates identity to dodge a challenge."""
    tool = "kahin_crawl_resume"
    async with _healer_ref.safe(tool, jobId=jobId):
        job, err = _resolve_job(tool, jobId)
        if err:
            return err
        async with job.lock:
            current = job.state
        if current in _TERMINAL:
            return _json_error(
                tool, f"job is already {current}; it cannot be resumed", "job_terminal",
                jobId=job.job_id, state=current,
            )
        if current != "paused":
            return _json_error(
                tool, f"job is {current}, not paused; resume is only valid from paused",
                "invalid_state_transition",
                jobId=job.job_id, current=current,
            )
        transition_err = await _transition(job, "running", "resumed")
        if transition_err:
            return transition_err
        job.resume_event.set()
        return orjson.dumps(
            {"jobId": job.job_id, "state": "running", "resumed": True, "reason": "resumed"},
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_crawl_stop", annotations=_DW)
async def crawl_stop(jobId: str | None = None) -> str:
    """Stop the job cleanly: cancel the background task, keep partial results
    readable, and do not close the browser."""
    tool = "kahin_crawl_stop"
    async with _healer_ref.safe(tool, jobId=jobId):
        job, err = _resolve_job(tool, jobId)
        if err:
            return err
        async with job.lock:
            current = job.state
        if current in _TERMINAL:
            return orjson.dumps(
                {"jobId": job.job_id, "state": current, "alreadyStopped": True},
                option=orjson.OPT_INDENT_2,
            ).decode()
        # Serialize cancellation with the stop/start rotation boundary. The
        # worker checks cancel_requested while holding this gate, so a stop
        # arriving during reaping cannot be followed by a surprise new boot.
        job.cancel_requested = True
        job.resume_event.set()
        try:
            await asyncio.wait_for(job.rotation_gate.acquire(), timeout=_ROTATION_GATE_TIMEOUT)
        except asyncio.TimeoutError:
            async with job.lock:
                state_now = job.state
            return _json_error(
                tool,
                f"crawl rotation is still in progress after {_ROTATION_GATE_TIMEOUT:.0f}s",
                "rotation_in_progress",
                jobId=job.job_id,
                state=state_now,
            )
        try:
            transition_err = await _transition(job, "cancelled", "user_stop")
            if transition_err:
                async with job.lock:
                    if job.state == "cancelled":
                        return orjson.dumps(
                            {"jobId": job.job_id, "state": "cancelled", "alreadyStopped": True},
                            option=orjson.OPT_INDENT_2,
                        ).decode()
                return transition_err
            task = job.task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:  # noqa: BLE001 - stop must return structured JSON
                    pass
        finally:
            job.rotation_gate.release()
        return orjson.dumps(
            {
                "jobId": job.job_id,
                "state": "cancelled",
                "stopped": True,
                "succeeded": job.succeeded,
                "failed": job.failed,
                "hint": "partial results remain readable with kahin_crawl_results; the browser stays open",
            },
            option=orjson.OPT_INDENT_2,
        ).decode()
