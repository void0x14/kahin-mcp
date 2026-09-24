"""Connect Marionette to Kahin's already-running Mirage browser for Bitwarden setup."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from marionette_driver.marionette import Marionette

from kahin.bitwarden import BITWARDEN_GECKO_ID

CONNECT_TIMEOUT_SECONDS = 8.0
PREFS_TIMEOUT_SECONDS = 5.0
RETRY_INTERVAL_SECONDS = 0.1
_UUIDS_PREF = "extensions.webextensions.uuids"


def _bitwarden_uuid(prefs_path: Path) -> str:
    """Read only the WebExtension UUID mapping from Firefox's prefs.js."""
    try:
        contents = prefs_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Firefox prefs.js is not available yet") from exc

    pattern = re.compile(
        r'user_pref\(\s*"' + re.escape(_UUIDS_PREF)
        + r'"\s*,\s*("(?:\\.|[^"\\])*")\s*\)\s*;'
    )
    for match in pattern.finditer(contents):
        try:
            mapping = json.loads(json.loads(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            continue
        value = mapping.get(BITWARDEN_GECKO_ID) if isinstance(mapping, dict) else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
            return value
    raise RuntimeError("Bitwarden extension UUID is not present in Firefox prefs.js")


def _connect_client(port: int, timeout: float, retry_interval: float) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        client = Marionette(host="127.0.0.1", port=port, app="firefox", socket_timeout=3)
        try:
            client.start_session()
            return client
        except Exception as exc:
            last_error = exc
            try:
                client.delete_session()
            except Exception:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("could not connect to the running browser's Marionette endpoint") from last_error
        time.sleep(min(retry_interval, remaining))


class PasskeyUISession:
    """A Marionette connection scoped to its own setup tab."""

    @classmethod
    def open(cls, engine: Any) -> "PasskeyUISession":
        """Open a dedicated Bitwarden tab in an existing Mirage browser."""
        return _open_session(cls, engine)

    def __init__(
        self,
        client: Any,
        original_handle: str,
        setup_handle: str,
        original_target: str | None,
        popup_url: str,
    ) -> None:
        self.client = client
        self.original_handle = original_handle
        self.setup_handle = setup_handle
        self.popup_url = popup_url
        # Marionette tab creation can change Mirage's selected target. The
        # async tool layer restores it with await engine.switch_page(value).
        self.original_target = original_target
        self._closed = False

    def is_alive(self) -> bool:
        """Report Marionette session liveness without inspecting page contents."""
        return not self._closed and bool(getattr(self.client, "session_id", None))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_setup_tab(self.client, self.original_handle, self.setup_handle)

    def __getattr__(self, name: str) -> Any:
        # Keep the returned session convenient for callers using Marionette APIs.
        return getattr(self.client, name)


def _close_setup_tab(client: Any, original_handle: str, setup_handle: str) -> None:
    """Best-effort bounded cleanup using Marionette's finite socket timeout."""
    try:
        handles = list(client.window_handles)
    except Exception:
        handles = []

    if setup_handle in handles and len(handles) > 1:
        try:
            client.switch_to_window(setup_handle)
            client.close()
        except Exception:
            pass

    try:
        handles = list(client.window_handles)
    except Exception:
        handles = []
    if original_handle in handles:
        try:
            client.switch_to_window(original_handle)
        except Exception:
            pass

    try:
        client.delete_session()
    except Exception:
        pass


def _open_session(session_type: type[PasskeyUISession], engine: Any) -> PasskeyUISession:
    """Attach to an existing Mirage Marionette endpoint and open Bitwarden's popup.

    This helper never launches, terminates, or otherwise owns the browser process.
    The returned session delegates Marionette calls to its ``client`` and owns
    only the setup tab. Call :meth:`PasskeyUISession.close` when interaction ends.
    """
    port = getattr(engine, "_marionette_port", None)
    profile = getattr(engine, "_profile_dir", None)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("running Mirage engine has no valid Marionette port")
    if profile is None:
        raise ValueError("running Mirage engine has no persistent profile")
    profile_path = Path(profile)

    client = _connect_client(port, CONNECT_TIMEOUT_SECONDS, RETRY_INTERVAL_SECONDS)
    original_handle: str | None = None
    setup_handle: str | None = None
    handles_before_open: set[str] = set()
    original_target = getattr(engine, "_current_target", None)
    try:
        original_handle = client.current_window_handle
        handles_before_open = set(client.window_handles)
        opened = client.open(type="tab", focus=True)
        setup_handle = opened["handle"]
        client.switch_to_window(setup_handle)

        deadline = time.monotonic() + PREFS_TIMEOUT_SECONDS
        extension_uuid: str | None = None
        last_error: RuntimeError | None = None
        while True:
            try:
                extension_uuid = _bitwarden_uuid(profile_path / "prefs.js")
                break
            except RuntimeError as exc:
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Bitwarden extension UUID lookup timed out") from last_error
            time.sleep(min(RETRY_INTERVAL_SECONDS, remaining))

        popup_url = f"moz-extension://{extension_uuid}/popup/index.html"
        client.navigate(popup_url)
        return session_type(client, original_handle, setup_handle, original_target, popup_url)
    except Exception:
        if original_handle is not None and setup_handle is None:
            try:
                new_handles = set(client.window_handles) - handles_before_open
                if len(new_handles) == 1:
                    setup_handle = new_handles.pop()
            except Exception:
                pass
        if original_handle is not None and setup_handle is not None:
            _close_setup_tab(client, original_handle, setup_handle)
        else:
            try:
                client.delete_session()
            except Exception:
                pass
        raise
