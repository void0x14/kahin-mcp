"""Bitwarden kasa açma (tek sefer): never ayarı devredeyken BİR kez aç.

Bitwarden auto-unlock anahtarını yalnızca kasa açılırken yazar. `never` biz
girişten sonra yazıldığı için, kalıcı otomatik açılma için `never` devredeyken
bir kez açmak gerekir. Bu betik görünür tarayıcıyı kilit ekranında açar, açmayı
bekler, auto-unlock anahtarının yazıldığını doğrular ve tarayıcıyı kapatır.
"""

import asyncio
import json
from pathlib import Path

from kahin import _state as state
from kahin.passkey_session import PasskeyUISession
from kahin.passkey_ui import vault_timeout_status
from kahin.tools import pilot

LOG = Path("/tmp/kahin-bw-unlock.log")
WAIT_SECONDS = 30 * 60

KEYS_SCRIPT = """
const cb = arguments[arguments.length - 1];
try {
  const w = window; const page = w.wrappedJSObject || w;
  const api = (page.browser || page.chrome).storage.local;
  Promise.resolve(api.get()).then(
    function (all) { cb({ok: true, keys: Object.keys(all)}); },
    function (e) { cb({ok: false, error: String((e && e.message) || e)}); }
  );
} catch (e) { cb({ok: false, error: String((e && e.message) || e)}); }
"""


def log(*parts: object) -> None:
    print(" ".join(str(p) for p in parts), flush=True)


async def _auto_key(client) -> str | None:
    out = await asyncio.to_thread(
        client.execute_async_script, KEYS_SCRIPT, (), script_timeout=8000
    )
    if not isinstance(out, dict) or out.get("ok") is not True:
        return None
    for key in out.get("keys", []):
        if "auto" in str(key).lower():
            return str(key)
    return None


async def main() -> None:
    LOG.write_text("")
    log("=== visible browser: Bitwarden kilit ekranı ===")
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
        log("timeout:", json.dumps(await asyncio.to_thread(vault_timeout_status, session.client), ensure_ascii=False))
        log(f"kasa açmanı bekliyorum (en fazla {WAIT_SECONDS // 60} dk) ...")
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        seen: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            key = await _auto_key(session.client)
            if key and key != seen:
                seen = key
                log("auto-unlock anahtarı YAZILDI:", key)
                break
            await asyncio.sleep(5)
        if seen:
            log("verify:", json.dumps(await asyncio.to_thread(vault_timeout_status, session.client), ensure_ascii=False))
        else:
            log("=== zaman aşımı: auto-unlock anahtarı görülmedi")
    finally:
        await asyncio.to_thread(session.close)
    log("stop:", json.loads(await pilot.browser_stop()).get("status"))
    log("=== done ===")


asyncio.run(main())
