"""Internal state — shared browser engine instance."""

import asyncio
import json
import os
from collections import deque
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

from kahin.the_twins.chassis import BrowserEngine

_current_engine: BrowserEngine | None = None
# Last engine death is intentionally retained across buffer cleanup and a
# replacement start so health/stats can explain why the previous browser
# disappeared instead of reporting only a generic "unavailable" state.
_last_engine_death: dict[str, Any] | None = None
# Browser lifecycle calls can arrive concurrently from an MCP client.  Keep
# the check/stop/start sequence atomic so two requests can never boot two
# browser processes before either one publishes its engine to this module.
_lifecycle_lock = asyncio.Lock()
_current_event_log: deque[dict[str, Any]] = deque(maxlen=5000)
# Monotonic event marker used by navigation waits. The bounded deque may
# evict entries, so its current length is not a stable cursor.
_event_seq = 0
_network_requests: deque[dict[str, Any]] = deque(maxlen=10000)
_console_messages: deque[dict[str, Any]] = deque(maxlen=5000)
_browser_lock_file: Any = None

# --- Crawler registry ----------------------------------------------------
# Deliberately separate from the event/network/console buffers. Engine
# rotation and crash recovery stop/start the browser, which routes through
# ``browser_stop``/``browser_start`` and calls ``clear_state()``; a crawl job
# must keep its URL queue, result ledger and job state intact across those
# lifecycle calls, so ``clear_state()`` never touches these containers.
# The registry lock serializes job creation/pruning plus the single-active
# job slot; per-job state transitions are guarded by the job's own lock
# (``_CrawlJob.lock`` in kahin/tools/crawler_mirage.py).
_crawl_jobs: dict[str, Any] = {}
_crawl_jobs_lock = asyncio.Lock()
_active_crawl_id: str | None = None


def acquire_browser_lock(mode: str = "ağırbaş") -> dict[str, Any] | None:
    """Claim the machine-wide browser owner slot, or report its owner.

    In 'keş' mode (ephemeral), sessions do not touch persistent profiles
    and must not block on or claim the machine-wide exclusive browser.lock slot.
    """
    if mode == "keş":
        return None
    global _browser_lock_file
    if fcntl is None or _browser_lock_file is not None:
        return None
    configured = os.environ.get("KAHIN_BROWSER_LOCK_PATH")
    path = (
        Path(configured).expanduser()
        if configured
        else Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "kahin" / "browser.lock"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner_text = handle.read(4096)
            handle.close()
            try:
                owner = json.loads(owner_text) if owner_text else None
            except json.JSONDecodeError:
                owner = None
            return {"path": str(path), "owner": owner}
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "path": str(path)}))
        handle.flush()
        _browser_lock_file = handle
        return None
    except OSError:
        # A lock filesystem that cannot be created must not make the browser
        # unusable; the in-process lifecycle lock still prevents local races.
        return None


def release_browser_lock() -> None:
    global _browser_lock_file
    handle, _browser_lock_file = _browser_lock_file, None
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, AttributeError):
        pass
    try:
        handle.close()
    except OSError:
        pass


def clear_state() -> None:
    # Event/network/console buffers only. Crawler state (_crawl_jobs and the
    # active job slot) is intentionally preserved so rotation/recovery can
    # stop and start the engine without losing the crawl.
    _current_event_log.clear()
    _network_requests.clear()
    _console_messages.clear()
