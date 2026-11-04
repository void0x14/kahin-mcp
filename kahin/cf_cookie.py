"""Cloudflare `cf_clearance` minter that never opens a browser.

Cloudflare's interstitial is a web page: it ships an obfuscated
"orchestrate" bundle that reads `window._cf_chl_opt`, runs its sensors and
talks to `/cdn-cgi/challenge-platform/*`. The bundle does not need V8 — it
needs a DOM, a clock and the network. This module provides exactly that:

* `kahin/addons/cf/shim.js` — the DOM/window surface the bundle expects,
* `kahin/addons/cf/cfhost.c` — the QuickJS host that runs it (same engine
  the nox add-on uses),
* this driver — the network: every request the page makes is performed here
  with the same TLS profile the scraper will replay with.

The mint is only accepted when the *page* opens with the cookie: the cookie
alone is not proof (a stale `cf_clearance` can sit in a jar while the
interstitial keeps being served), so the last step is always a fresh fetch of
the challenged URL.

Contract:
    store = CfCookieStore("https://pastebin.com/login")
    store.refresh()           # mint + verify
    header = store.cookie_header()   # "cf_clearance=..." or ""
    store.stop()
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Frame markers for the host pipe.
_FRAME_REQUEST = b"R"
_FRAME_RESPONSE = b"S"
_FRAME_DONE = b"D"

MAX_FRAME = 16 * 1024 * 1024
MAX_CHALLENGE_BYTES = 512 * 1024
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
CHALLENGE_TIMEOUT = 30
MINT_TIMEOUT = 120

# Cloudflare asks for the interstitial with these markers; a page that carries
# one of them is a challenge, not the content.
_INTERSTITIAL_MARKERS = (
    "just a moment",
    "checking your browser",
    "please complete the captcha",
    "cf-browser-verification",
    "challenges.cloudflare.com",
    "_cf_chl_opt",
)

_CLEARANCE_COOKIE = "cf_clearance"

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# The challenge bundle is served from the challenged origin; a script that
# points somewhere else is not part of this challenge.
_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_INLINE_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_CF_OPT = re.compile(r"window\._cf_chl_opt\s*=", re.IGNORECASE)


class CfCookieError(RuntimeError):
    """Raised when a clearance cookie cannot be produced or verified."""


def _kahin_home() -> Path:
    configured = os.environ.get("KAHIN_HOME")
    if configured:
        home = Path(configured).expanduser()
        if not home.is_absolute():
            raise ValueError("KAHIN_HOME must be an absolute path")
        return home
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / "kahin"
    return Path("~/.local/share/kahin").expanduser()


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent / "addons" / "cf"


def _host_binary() -> Path:
    override = os.environ.get("KAHIN_CF_HOST", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise CfCookieError(f"KAHIN_CF_HOST points at a missing file: {path}")
        return path
    name = "cfhost.exe" if os.name == "nt" else "cfhost"
    for candidate in (_addon_dir() / "bin" / name, _addon_dir() / name):
        if candidate.is_file():
            return candidate
    raise CfCookieError(
        f"cf host binary is missing; run `python -m kahin.cf_cookie --build` "
        f"or set KAHIN_CF_HOST (looked in {_addon_dir() / 'bin' / name})"
    )


def _shim_path() -> Path:
    path = _addon_dir() / "shim.js"
    if not path.is_file():
        raise CfCookieError(f"cf shim is missing: {path}")
    return path


def is_interstitial(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _INTERSTITIAL_MARKERS)


def _extract_bootstrap(html: str) -> str:
    """Pull the inline script that seeds window._cf_chl_opt."""
    for match in _INLINE_SCRIPT.finditer(html):
        code = match.group(1)
        if _CF_OPT.search(code):
            return code
    raise CfCookieError("challenge page carries no _cf_chl_opt bootstrap")


def _read_exact(stream: Any, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return b""
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: Any) -> tuple[str, bytes] | None:
    header = _read_exact(stream, 5)
    if len(header) != 5:
        return None
    length = int.from_bytes(header[1:5], "little")
    if length > MAX_FRAME:
        raise CfCookieError(f"host frame is too large: {length}")
    body = _read_exact(stream, length) if length else b""
    if len(body) != length:
        return None
    return header[:1].decode("ascii", "replace"), body


def _write_frame(stream: Any, kind: bytes, payload: bytes) -> None:
    stream.write(kind + len(payload).to_bytes(4, "little") + payload)
    stream.flush()


class _Transport:
    """Performs the page's requests with one impersonated TLS session."""

    def __init__(self, user_agent: str, referer: str, origin: str) -> None:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise CfCookieError(f"curl_cffi is required: {exc}") from exc
        self._session = curl_requests.Session(impersonate="chrome131")
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": referer,
                "Origin": origin,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )
        self.user_agent = user_agent
        self.requests: list[dict[str, Any]] = []
        self.clearance: str = ""

    def perform(self, method: str, url: str, headers: str, body: str) -> tuple[int, str, dict[str, str]]:
        request_headers: dict[str, str] = {}
        for line in headers.splitlines():
            if not line.strip():
                continue
            name, _, value = line.partition(":")
            if name.strip():
                request_headers[name.strip()] = value.strip()
        request_headers.setdefault("User-Agent", self.user_agent)

        kwargs: dict[str, Any] = {"headers": request_headers, "timeout": CHALLENGE_TIMEOUT}
        if method.upper() != "GET" and body:
            kwargs["data"] = body.encode("utf-8", "replace")
        response = self._session.request(method.upper(), url, **kwargs)
        import sys as _sys
        print(f"[cf] {method.upper()} {url} -> {response.status_code}", file=_sys.stderr, flush=True)

        response_headers = {k.lower(): v for k, v in response.headers.items()}
        set_cookie = response.headers.get("set-cookie", "")
        self.requests.append(
            {
                "method": method.upper(),
                "url": url,
                "status": response.status_code,
                "setCookie": set_cookie[:400],
            }
        )
        for piece in set_cookie.split(","):
            match = re.match(r"\s*([A-Za-z0-9_\-]+)=", piece)
            if match and match.group(1) == _CLEARANCE_COOKIE:
                value = piece.split("=", 1)[1].split(";")[0].strip()
                if value:
                    self.clearance = value
        return response.status_code, response.text or "", response_headers

    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for cookie in self._session.cookies.jar:
            out[cookie.name] = cookie.value
        return out

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


class CfCookieStore:
    """Keeps one `cf_clearance` cookie fresh for one host."""

    def __init__(
        self,
        url: str,
        *,
        user_agent: str = _DEFAULT_UA,
        timeout: int = MINT_TIMEOUT,
        write_file: bool = True,
    ) -> None:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("url must include a scheme and a hostname")
        self.url = url
        self.origin = f"{parsed.scheme}://{parsed.hostname}"
        self.host = parsed.hostname
        self.user_agent = user_agent
        self.timeout = timeout
        self.write_file = write_file
        self.cookie: str = ""
        self.minted_at: float = 0.0
        self.elapsed_ms: int = 0
        self.verified: bool = False
        self.evidence: list[dict[str, Any]] = []
        self.transport: _Transport | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = __import__("threading").Lock()

    # ------------------------------------------------------------------ files

    def cookie_file(self) -> Path:
        return _kahin_home() / "cf" / f"{self.host}.cookie"

    def cookie_header(self) -> str:
        if not self.cookie:
            return ""
        return f"{_CLEARANCE_COOKIE}={self.cookie}"

    # ------------------------------------------------------------------- mint

    def _fetch_challenge(self, transport: _Transport) -> str:
        response = transport._session.get(self.url, timeout=CHALLENGE_TIMEOUT)
        body = response.text or ""
        if response.status_code == 200 and not is_interstitial(body):
            raise CfCookieError(
                f"{self.url} answered {response.status_code} without a challenge; "
                "no clearance cookie is needed"
            )
        return body

    def _run_host(self, transport: _Transport, bootstrap: str, page_html: str = "") -> dict[str, Any]:
        binary = _host_binary()
        shim = _shim_path()
        with tempfile.TemporaryDirectory(prefix="kahin-cf-") as workdir:
            script_path = Path(workdir) / "bootstrap.js"
            html_path = Path(workdir) / "page.html"
            html_path.write_text(page_html, encoding="utf-8")
            script_path.write_text(bootstrap, encoding="utf-8")
            env = dict(os.environ)
            env["CF_HTML_FILE"] = str(html_path)
            env["CF_SHIM"] = str(shim)
            env["CF_SCRIPTS"] = str(script_path)
            env["CF_USER_AGENT"] = self.user_agent
            env["CF_LOCATION"] = self.url
            self._proc = subprocess.Popen(
                [str(binary), "--solve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            import sys as _sys, threading as _threading
            def _drain_stderr():
                stream = self._proc.stderr
                if stream is None:
                    return
                for line in stream:
                    print("[cfhost] " + line.decode("utf-8", "replace").rstrip(), file=_sys.stderr, flush=True)
            _threading.Thread(target=_drain_stderr, daemon=True).start()
            assert self._proc.stdout is not None and self._proc.stdin is not None
            state: dict[str, Any] = {}
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                frame = _read_frame(self._proc.stdout)
                if frame is None:
                    break
                kind, payload = frame
                if kind == "R":
                    request = payload.decode("utf-8", "replace")
                    method, url, headers, body = _split_request(request)
                    status, text, response_headers = transport.perform(method, url, headers, body)
                    _write_frame(
                        self._proc.stdin,
                        _FRAME_RESPONSE,
                        _encode_response(status, response_headers, text),
                    )
                    continue
                if kind == "D":
                    try:
                        state = json.loads(payload.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        state = {}
                    break
            self._close_process()
        return state

    def _close_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def refresh(self) -> str:
        """Mint a clearance cookie and prove it by loading the page again."""
        with self._lock:
            started = time.monotonic()
            transport = _Transport(self.user_agent, self.origin + "/", self.origin)
            self.transport = transport
            try:
                challenge = self._fetch_challenge(transport)
                bootstrap = _extract_bootstrap(challenge)
                state = self._run_host(transport, bootstrap, challenge)
                self.evidence = transport.requests
                clearance = transport.clearance or _cookie_from_state(state)
                if not clearance:
                    raise CfCookieError(
                        "challenge finished without a cf_clearance cookie "
                        f"(page state: {json.dumps(state)[:400]})"
                    )
                self.cookie = clearance
                self.minted_at = time.time()
                self.verified = self._verify(transport)
                if not self.verified:
                    raise CfCookieError(
                        "cf_clearance was issued but the page still serves the interstitial"
                    )
                self.elapsed_ms = int((time.monotonic() - started) * 1000)
                self._publish()
                return clearance
            finally:
                transport.close()

    def _verify(self, transport: _Transport) -> bool:
        """Ground truth is the page, not the cookie (cf-hata-mezarligi M11)."""
        response = transport._session.get(self.url, timeout=CHALLENGE_TIMEOUT)
        body = response.text or ""
        self.evidence.append(
            {
                "method": "GET",
                "url": self.url,
                "status": response.status_code,
                "setCookie": "verify",
            }
        )
        return response.status_code == 200 and not is_interstitial(body)

    def _publish(self) -> None:
        if not self.write_file:
            return
        path = self.cookie_file()
        if path.is_symlink():
            raise CfCookieError(f"cookie file must not be a symlink: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "host": self.host,
            "origin": self.origin,
            "url": self.url,
            "name": _CLEARANCE_COOKIE,
            "value": self.cookie,
            "minted_at": self.minted_at,
            "user_agent": self.user_agent,
            "verified": self.verified,
            "elapsed_ms": self.elapsed_ms,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def status(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "url": self.url,
            "cookie_name": _CLEARANCE_COOKIE,
            "held": bool(self.cookie),
            "verified": self.verified,
            "age_seconds": round(0.0 if not self.minted_at else time.time() - self.minted_at, 3),
            "elapsed_ms": self.elapsed_ms,
            "cookie_file": str(self.cookie_file()) if self.write_file else None,
            "requests": self.evidence,
        }

    def stop(self) -> None:
        with self._lock:
            self._close_process()
            if self.transport is not None:
                self.transport.close()
                self.transport = None

def browser_clearance(
    url: str,
    *,
    timeout: int = 90,
    user_agent: str = _DEFAULT_UA,
    write_file: bool = True,
) -> dict[str, Any]:
    """Tarayıcı yolu: Cloudflare'ın challenge'ı kahin's headless Camoufox
    motorunda çözülür ve `cf_clearance` dosyaya yazılır.

    Bu yol yalnızca JS/canvas gerektiren challenge'lar için kullanılır; düz CF
    için tarayıcısız motor yolu yeterlidir. Motor `kes` modunda açılır:
    profil geçici, pencere görünmez, kapanınca hiçbir iz kalmaz.

    Dönüş: {"cleared", "cookie", "user_agent", "elapsed_ms", "reason"}"""
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        from kahin.tools.cf_clear_mirage import _cf_cookies, _challenge_probe
        from kahin.tools.pilot_mirage import _capture_page_session
    except ImportError as exc:
        return {
            "cleared": False,
            "reason": f"kahin browser tools unavailable: {exc}",
            "elapsed_ms": elapsed_ms(),
        }

    host = (urlparse(url).hostname or "").lower()

    async def _run() -> dict[str, Any]:
        from kahin.tools._common import _mirage_engine
        from kahin.tools.cf_clear_mirage import _cf_cookies, _challenge_probe
        from kahin.tools.pilot_mirage import _capture_page_session

        session_id, error = await _capture_page_session("cf_cookie")
        if error:
            return {"cleared": False, "reason": str(error)}
        assert session_id is not None
        engine = _mirage_engine()
        last = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await engine.call("Page.navigate", {"url": url}, session_id=session_id)
            except Exception as exc:  # noqa: BLE001 - a failed attempt is evidence
                last = f"navigate failed: {exc}"
            for _ in range(30):
                await asyncio.sleep(1.0)
                try:
                    cookies = await _cf_cookies(session_id, host)
                except Exception:  # noqa: BLE001
                    cookies = {}
                if _CLEARANCE_COOKIE in cookies:
                    value = cookies[_CLEARANCE_COOKIE]
                    try:
                        probe = await _challenge_probe(session_id)
                    except Exception:  # noqa: BLE001
                        probe = {}
                    kind = probe.get("kind") if isinstance(probe, dict) else None
                    return {
                        "cleared": kind is None,
                        "cookie": value,
                        "user_agent": user_agent,
                        "kind": kind,
                    }
            last = "no cf_clearance yet"
        return {"cleared": False, "reason": last or "timeout"}

    import asyncio

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - the caller gets the reason
        return {
            "cleared": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": elapsed_ms(),
        }
    result["elapsed_ms"] = elapsed_ms()
    if result.get("cleared") and write_file:
        path = _kahin_home() / "cf" / f"{host}.cookie"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "host": host,
            "origin": f"https://{host}",
            "url": url,
            "name": _CLEARANCE_COOKIE,
            "value": result["cookie"],
            "minted_at": time.time(),
            "user_agent": user_agent,
            "verified": True,
            "elapsed_ms": result["elapsed_ms"],
            "source": "browser",
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
        result["cookie_file"] = str(path)
    return result



def _split_request(payload: str) -> tuple[str, str, str, str]:
    head, _, body = payload.partition("\n\n")
    lines = head.split("\n")
    method = lines[0] if lines else "GET"
    url = lines[1] if len(lines) > 1 else ""
    headers = "\n".join(lines[2:])
    return method, url, headers, body


def _encode_response(status: int, headers: dict[str, str], body: str) -> bytes:
    out = [str(status)]
    for name, value in headers.items():
        out.append(f"{name}: {value}")
    return ("\n".join(out) + "\n\n" + body).encode("utf-8", "replace")


def _cookie_from_state(state: dict[str, Any]) -> str:
    raw = str(state.get("cookie") or "")
    for piece in raw.split(";"):
        piece = piece.strip()
        if piece.startswith(_CLEARANCE_COOKIE + "="):
            return piece.split("=", 1)[1].strip()
    return ""


# ---------------------------------------------------------------- build helper


def build_host(destination: Path | None = None) -> Path:
    """Compile the cf host against a freshly built QuickJS."""
    import subprocess as sp

    addon = _addon_dir()
    target = destination or (addon / "bin" / ("cfhost.exe" if os.name == "nt" else "cfhost"))
    source_root = Path(
        os.environ.get("KAHIN_QUICKJS_SRC", "").strip() or (addon / "_build" / "quickjs")
    )
    if not (source_root / "CMakeLists.txt").is_file():
        raise CfCookieError(
            f"QuickJS source is missing at {source_root}; set KAHIN_QUICKJS_SRC or "
            "vendor quickjs-ng there before building"
        )
    build_dir = source_root / "build"
    sp.run(
        ["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release", source_root],
        check=True,
        capture_output=True,
    )
    sp.run(["cmake", "--build", str(build_dir), "-j"], check=True, capture_output=True)
    lib = build_dir / "libqjs.a"
    if not lib.is_file():
        raise CfCookieError(f"QuickJS static library was not produced: {lib}")

    target.parent.mkdir(parents=True, exist_ok=True)
    sp.run(
        [
            os.environ.get("CC", "cc"), "-O2", "-o", str(target),
            str(addon / "cfhost.c"), "-I", str(build_dir), "-I", str(source_root),
            str(lib), "-lm", "-lpthread", "-ldl",
        ],
        check=True,
    )
    return target


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="kahin.cf_cookie", description=__doc__)
    parser.add_argument("--url", default="https://pastebin.com/login")
    parser.add_argument("--build", action="store_true", help="compile the cf host")
    parser.add_argument("--json", action="store_true", help="print status as JSON")
    args = parser.parse_args(argv)

    if args.build:
        print(build_host())
        return 0

    store = CfCookieStore(args.url)
    try:
        store.refresh()
    except CfCookieError as exc:
        print(f"!error {exc}", file=__import__("sys").stderr)
        return 1
    finally:
        store.stop()
    if args.json:
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    else:
        print(store.cookie_header())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
