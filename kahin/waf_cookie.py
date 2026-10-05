"""Baidu ADAS `nox_jst_v1` cookie minter for WAF-protected HTTP scraping.

Gitee (and other sites behind Baidu ADAS) answer an HTML challenge instead of the
requested page until the client carries a `nox_jst_v1` cookie. That cookie is a
signature produced by the site's own `nox_*.js` bundle. The bundle is a JSVMP that
runs fine outside a real browser: it needs a `window` with `resetNoxJstV1()` and a
`document.cookie` setter, nothing more.

This module runs that bundle on a QuickJS runtime (kahin/addons/nox), keeps one
host process alive, and serves the cookie as a file so a scraper never waits on
the minter. Measured on this host: peak RSS 10.3-11.4 MB, cold start 88-135 ms,
~9 ms per mint, and 10/10 unique cookies per process.

The declared TTL comes from the challenge itself (`window.__noxExpire`). Measured
against the live WAF, a cookie dies between minute 27 and minute 30, so the
refresh interval is `ttl * REFRESH_FRACTION` rather than the TTL itself.

Contract:
    store = NoxCookieStore(origin="gitee.com", probe_path="/explore")
    store.ensure_fresh()          # mint only when the cookie is older than the interval
    header = store.cookie_header() # "nox_jst_v1=..." or "" when unavailable
    store.stop()                  # stop the background host
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Fraction of the declared TTL at which we re-mint. Measured: last valid at
# minute 27, first 405 at minute 30. Refreshing at 0.66 keeps 7+ minutes of margin.
REFRESH_FRACTION = 0.66
MIN_REFRESH_SECONDS = 60.0

MAX_SCRIPT_BYTES = 8 * 1024 * 1024
MAX_CHALLENGE_BYTES = 256 * 1024
# A WAF script fetch must not be clamped to the challenge-page limit: the nox
# bundle is ~316 KB and the challenge page is ~400 bytes.
MAX_SCRIPT_READ = 8 * 1024 * 1024
MAX_COOKIE_BYTES = 4 * 1024
MAX_SCRIPTS = 4
CHALLENGE_TIMEOUT = 20
MINT_TIMEOUT = 30

_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_NOX_SCRIPT = re.compile(r"(nox_[A-Za-z0-9_.-]+\.js)", re.IGNORECASE)
_EXPIRE = re.compile(r"__noxExpire\s*=\s*(\d+)")
_COOKIE_NAME = "nox_jst_v1"
_DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
_STEALTH_GLOBALS = ("__KA_USER_AGENT", "__KA_LOCATION")


class NoxCookieError(RuntimeError):
    """Raised when a cookie cannot be produced or the challenge is unusable."""


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


def _cache_home() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return cache / "kahin" / "nox"


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent / "addons" / "nox"


def _host_binary() -> Path:
    """Locate the compiled nox host.

    Checked in order: an explicit override, the wheel-local build output, then the
    checkout (development link), so both installed and in-repo runs work.
    """
    override = os.environ.get("KAHIN_NOX_HOST", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise NoxCookieError(f"KAHIN_NOX_HOST points at a missing file: {path}")
        return path
    name = "noxhost.exe" if os.name == "nt" else "noxhost"
    for candidate in (_addon_dir() / "bin" / name, _addon_dir() / name):
        if candidate.is_file():
            return candidate
    raise NoxCookieError(
        f"nox host binary is missing; run `python -m kahin.waf_cookie --build` "
        f"or set KAHIN_NOX_HOST (looked in {_addon_dir() / 'bin' / name})"
    )


def _shim_path() -> Path:
    path = _addon_dir() / "shim.js"
    if not path.is_file():
        raise NoxCookieError(f"nox shim is missing: {path}")
    return path


def _http_get(url: str, timeout: int = CHALLENGE_TIMEOUT, limit: int = MAX_CHALLENGE_BYTES) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, body, headers
    except urllib.error.HTTPError as exc:
        body = exc.read(limit + 1)
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, body, headers
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise NoxCookieError(f"request failed for {url}: {exc}") from exc


def _find_scripts(html: str, origin: str) -> list[str]:
    """Collect the WAF bundle URLs from the challenge page.

    The path prefix is generated per deployment, so it cannot be hardcoded; the
    URLs must come from the challenge itself.
    """
    from urllib.parse import urljoin

    scripts: list[str] = []
    for match in _SCRIPT_SRC.finditer(html):
        candidate = urljoin(origin, match.group(1))
        if not candidate.startswith(origin):
            raise NoxCookieError(f"challenge references a script outside {origin}: {candidate}")
        if _NOX_SCRIPT.search(candidate) or "gangplank" in candidate:
            if candidate not in scripts:
                scripts.append(candidate)
    if not scripts:
        raise NoxCookieError("challenge page carries no nox/gangplank script")
    if len(scripts) > MAX_SCRIPTS:
        raise NoxCookieError(f"challenge references more than {MAX_SCRIPTS} scripts")
    return scripts


def _declared_ttl(html: str) -> int | None:
    match = _EXPIRE.search(html)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _download_scripts(urls: list[str], cache: Path) -> list[Path]:
    cache.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        if not name or "/" in name or name in (".", ".."):
            raise NoxCookieError(f"unusable script name from {url}")
        destination = cache / name
        if destination.is_symlink():
            raise NoxCookieError(f"script cache entry must not be a symlink: {destination}")
        if destination.is_file() and destination.stat().st_size <= MAX_SCRIPT_BYTES:
            paths.append(destination)
            continue
        status, body, _ = _http_get(url, limit=MAX_SCRIPT_READ)
        if status != 200 or not body:
            raise NoxCookieError(f"script fetch returned {status} for {url}")
        if len(body) > MAX_SCRIPT_READ:
            raise NoxCookieError(f"script exceeds {MAX_SCRIPT_READ} bytes: {url}")
        temporary = cache / f".{name}.tmp"
        temporary.write_bytes(body)
        temporary.replace(destination)
        paths.append(destination)
    return paths


class _NoxHost:
    """One long-lived nox host process; each request mints a fresh cookie."""

    def __init__(self, scripts: list[Path], user_agent: str, location: str) -> None:
        self._binary = _host_binary()
        env = dict(os.environ)
        env["NOX_SHIM"] = str(_shim_path())
        env["NOX_SCRIPTS"] = ":".join(str(p) for p in scripts)
        env["NOX_USER_AGENT"] = user_agent
        env["NOX_LOCATION"] = location
        self._proc = subprocess.Popen(
            [str(self._binary), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        # stderr carries !ready / !error lines; drain them so the pipe never fills.
        self._stderr: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        ready = self._await_ready()
        if not ready:
            raise NoxCookieError(f"nox host did not become ready: {'; '.join(self._stderr) or 'no error'}")

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr.append(line.strip())
            del self._stderr[:-32]

    def _await_ready(self) -> bool:
        deadline = time.monotonic() + MINT_TIMEOUT
        while time.monotonic() < deadline:
            if "!ready" in self._stderr:
                return True
            if self._proc.poll() is not None:
                return False
            time.sleep(0.02)
        return False

    def mint(self) -> str:
        with self._lock:
            if self._proc.poll() is not None:
                raise NoxCookieError(f"nox host exited with {self._proc.returncode}")
            assert self._proc.stdin is not None and self._proc.stdout is not None
            try:
                self._proc.stdin.write("mint\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise NoxCookieError(f"nox host stdin is closed: {exc}") from exc
            deadline = time.monotonic() + MINT_TIMEOUT
            while time.monotonic() < deadline:
                line = self._proc.stdout.readline()
                if line:
                    value = line.strip()
                    if value:
                        if value.startswith("!"):
                            raise NoxCookieError(f"nox host reported: {value}")
                        return value
                    continue
                if self._proc.poll() is not None:
                    raise NoxCookieError(
                        f"nox host exited with {self._proc.returncode}: {'; '.join(self._stderr)}"
                    )
                time.sleep(0.005)
            raise NoxCookieError("nox host did not answer within the timeout")

    def close(self) -> None:
        if self._proc.poll() is not None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write("refresh\n")
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class NoxCookieStore:
    """Keeps one `nox_jst_v1` cookie fresh and readable as a file."""

    def __init__(
        self,
        origin: str = "gitee.com",
        *,
        probe_path: str = "/explore",
        user_agent: str = _DEFAULT_UA,
        cookie_name: str = _COOKIE_NAME,
        write_file: bool = True,
    ) -> None:
        if not origin.startswith("https://") and not origin.startswith("http://"):
            raise ValueError("origin must include the scheme, e.g. https://gitee.com")
        self.origin = origin.rstrip("/")
        self.probe_path = probe_path if probe_path.startswith("/") else f"/{probe_path}"
        self.user_agent = user_agent
        self.cookie_name = cookie_name
        self.cookie: str = ""
        self.minted_at: float = 0.0
        self.declared_ttl: int | None = None
        self.scripts: list[str] = []
        self.write_file = write_file
        self._host: _NoxHost | None = None
        self._lock = threading.Lock()

    @property
    def probe_url(self) -> str:
        return f"{self.origin}{self.probe_path}"

    @property
    def refresh_interval(self) -> float:
        """Seconds between re-mints: a fraction of the TTL the WAF declares."""
        ttl = self.declared_ttl or 1800
        return max(MIN_REFRESH_SECONDS, ttl * REFRESH_FRACTION * 60.0)

    def age(self) -> float:
        return 0.0 if not self.minted_at else time.time() - self.minted_at

    def is_fresh(self) -> bool:
        return bool(self.cookie) and self.age() < self.refresh_interval

    def cookie_file(self) -> Path:
        return _kahin_home() / "nox" / f"{self.origin.split('://')[-1].replace('/', '_')}.cookie"

    def cookie_header(self) -> str:
        """`name=value`, or an empty string when no cookie is held."""
        if not self.cookie:
            return ""
        return f"{self.cookie_name}={self.cookie}"

    def _ensure_host(self, scripts: list[Path]) -> _NoxHost:
        if self._host is not None:
            return self._host
        self._host = _NoxHost(scripts, self.user_agent, self.probe_url)
        return self._host

    def _discover(self) -> tuple[list[Path], int | None]:
        status, body, _ = _http_get(self.probe_url)
        html = body[:MAX_CHALLENGE_BYTES].decode("utf-8", "replace")
        if status == 200 and _COOKIE_NAME not in html:
            raise NoxCookieError(
                f"{self.probe_url} answered {status} without a WAF challenge; "
                "the cookie may no longer be required"
            )
        urls = _find_scripts(html, self.origin)
        ttl = _declared_ttl(html)
        self.scripts = urls
        return _download_scripts(urls, _cache_home()), ttl

    def refresh(self, force: bool = True) -> str:
        """Mint a new cookie (or reuse a fresh one) and return it."""
        with self._lock:
            if not force and self.is_fresh():
                return self.cookie
            scripts, ttl = self._discover()
            if ttl:
                self.declared_ttl = ttl
            host = self._ensure_host(scripts)
            value = host.mint()
            if not value or len(value) > MAX_COOKIE_BYTES:
                raise NoxCookieError("nox host returned an unusable cookie value")
            self.cookie = value
            self.minted_at = time.time()
            self._publish()
            return value

    def ensure_fresh(self) -> str:
        """Mint only when the held cookie is older than the refresh interval."""
        return self.refresh(force=not self.is_fresh())

    def _publish(self) -> None:
        if not self.write_file:
            return
        path = self.cookie_file()
        if path.is_symlink():
            raise NoxCookieError(f"cookie file must not be a symlink: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "origin": self.origin,
            "name": self.cookie_name,
            "value": self.cookie,
            "minted_at": self.minted_at,
            "declared_ttl": self.declared_ttl,
            "refresh_interval": self.refresh_interval,
            "scripts": self.scripts,
            "stealth_globals": list(_STEALTH_GLOBALS),
        }
        payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)

    def status(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "cookie_name": self.cookie_name,
            "held": bool(self.cookie),
            "age_seconds": round(self.age(), 3),
            "declared_ttl": self.declared_ttl,
            "refresh_interval": round(self.refresh_interval, 3),
            "fresh": self.is_fresh(),
            "scripts": self.scripts,
            "cookie_file": str(self.cookie_file()) if self.write_file else None,
            "host_running": self._host is not None,
        }

    def stop(self) -> None:
        with self._lock:
            if self._host is not None:
                self._host.close()
                self._host = None


# ---------------------------------------------------------------- build helper


def build_host(destination: Path | None = None) -> Path:
    """Compile the nox host against a freshly built QuickJS.

    Requires cmake and a C compiler. Used by `python -m kahin.waf_cookie --build`
    and by bin/setup.mjs during install, because a compiled binary cannot be
    shipped inside the wheel for every platform.
    """
    import subprocess as sp

    addon = _addon_dir()
    target = destination or (addon / "bin" / ("noxhost.exe" if os.name == "nt" else "noxhost"))
    source_root = Path(
        os.environ.get("KAHIN_QUICKJS_SRC", "").strip() or (addon / "_build" / "quickjs")
    )
    if not (source_root / "CMakeLists.txt").is_file():
        raise NoxCookieError(
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
        raise NoxCookieError(f"QuickJS static library was not produced: {lib}")

    target.parent.mkdir(parents=True, exist_ok=True)
    sp.run(
        [
            os.environ.get("CC", "cc"), "-O2", "-o", str(target),
            str(addon / "noxhost.c"), "-I", str(build_dir), "-I", str(source_root),
            str(lib), "-lm", "-lpthread", "-ldl",
        ],
        check=True,
    )
    return target


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="kahin.waf_cookie", description=__doc__)
    parser.add_argument("--origin", default="https://gitee.com")
    parser.add_argument("--probe-path", default="/explore")
    parser.add_argument("--build", action="store_true", help="compile the nox host")
    parser.add_argument("--json", action="store_true", help="print status as JSON")
    args = parser.parse_args(argv)

    if args.build:
        print(build_host())
        return 0

    store = NoxCookieStore(args.origin, probe_path=args.probe_path)
    try:
        store.refresh()
    except NoxCookieError as exc:
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
