"""One-time Bitwarden setup: open a VISIBLE browser at the Bitwarden login,
wait for the human login, then disable lock/timeout at the storage root.

Run once: the human logs in a single time; the script then writes
``vaultTimeout = "never"`` (action ``lock``), verifies the read-back, and
stops the browser. Everything after the login is code-driven.
"""

import asyncio
import json
import time
from pathlib import Path

from kahin import _state as state
from kahin.passkey_session import PasskeyUISession
from kahin.passkey_ui import (
    configure_vault_timeout_root,
    prepare_login_ui,
    vault_timeout_status,
)
from kahin.tools import pilot

LOG = Path("/tmp/kahin-bw-login.log")
WAIT_SECONDS = 30 * 60


def log(*parts: object) -> None:
    print(" ".join(str(p) for p in parts), flush=True)


async def main() -> None:
    LOG.write_text("")
    log("=== visible ağırbaş browser + Bitwarden login ===")
    result = json.loads(
        await pilot.browser_start(mode="ağırbaş", passkey_mode=True, headless=False)
    )
    log("start:", result.get("status"), "| bitwarden:", result.get("bitwarden"))
    if result.get("status") != "started":
        log("START FAILED:", json.dumps(result, ensure_ascii=False))
        return

    engine = state._current_engine
    session = await asyncio.to_thread(PasskeyUISession.open, engine)
    log("bitwarden popup:", session.popup_url)
    try:
        log("login ui:", await asyncio.to_thread(prepare_login_ui, session.client, session.popup_url))
    except Exception as exc:  # noqa: BLE001 - keep the browser open regardless
        log("prepare_login_ui failed:", exc)

    log(f"waiting up to {WAIT_SECONDS // 60} min for the one-time login ...")
    deadline = time.time() + WAIT_SECONDS
    last: object = None
    configured = False
    while time.time() < deadline:
        try:
            status = await asyncio.to_thread(vault_timeout_status, session.client)
        except Exception as exc:  # noqa: BLE001 - transient marionette hiccup
            status = {"status": "error", "state": str(exc)}
        if status != last:
            log("status:", json.dumps(status, ensure_ascii=False))
            last = status
        if status.get("status") == "ok":
            applied = await asyncio.to_thread(configure_vault_timeout_root, session.client)
            log("configure_vault_timeout_root:", json.dumps(applied, ensure_ascii=False))
            if applied.get("status") == "configured":
                log("verify:", json.dumps(await asyncio.to_thread(vault_timeout_status, session.client), ensure_ascii=False))
                configured = True
                break
        await asyncio.sleep(5)

    if not configured:
        log("=== no login detected within the window; rerun the script")

    await asyncio.to_thread(session.close)
    log("stop:", json.loads(await pilot.browser_stop()).get("status"))
    log("=== done ===")


asyncio.run(main())
