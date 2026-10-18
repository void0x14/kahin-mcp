"""Shared fixtures for CDP-Kahin tests."""

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from kahin.residual_self.fate import FateDB
from kahin.the_source.architect import SchemaEngine


@pytest.fixture(autouse=True)
def _hermetic_launch_version(monkeypatch, request):
    """Unit tests use fake browser binaries without a version.json.

    Production reads the Firefox version from the resolved engine's
    version.json. Tests must not depend on a cache-resident Camoufox
    install just to satisfy launch_options().
    """
    if request.node.module.__name__.endswith("test_engine_resolver"):
        return
    from kahin.the_twins import mirage

    monkeypatch.setattr(mirage, "_camoufox_ff_version", lambda _binary: "152.0.4")

    def _fake_launch_options(*, firefox_user_prefs=None, **_kwargs):
        return {"env": {}, "firefox_user_prefs": dict(firefox_user_prefs or {})}

    # Unit tests use fake browser binaries: no properties.json, fontconfig
    # or cache-resident Camoufox. The real launch path stays E2E-only.
    monkeypatch.setattr(mirage, "launch_options", _fake_launch_options)


_REAL_E2E_NODEIDS: set[str] = set()
_REAL_E2E_EXECUTED = 0
_REAL_E2E_SKIPPED: list[str] = []


def _is_real_e2e_item(item: pytest.Item) -> bool:
    """Identify tests that must exercise the Camoufox/sidecar runtime."""
    filename = Path(str(item.fspath)).name
    return filename.startswith("test_e2e_") or (
        filename == "test_mirage_ipc.py" and item.name == "test_mirage_full_flow_real_camoufox"
    )


def _skipif_is_active(marker: pytest.Mark) -> bool:
    """Evaluate the small, boolean skipif contract used by this suite.

    A string condition cannot be proven safely from this gate, so it is
    treated as an error by the caller instead of becoming a silent skip.
    """
    conditions = marker.args
    if not conditions and "condition" in marker.kwargs:
        conditions = (marker.kwargs["condition"],)
    if not conditions:
        return True
    condition = conditions[0]
    if not isinstance(condition, bool):
        raise pytest.UsageError(
            "KAHIN_REQUIRE_REAL_E2E=1 requires boolean skipif conditions for real tests; "
            f"got {condition!r} on a real E2E item"
        )
    return condition


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Never let a real-browser suite report green by skipping its runtime.

    Developer runs may still use the fast unit/fake-sidecar suite. CI and
    release verification set ``KAHIN_REQUIRE_REAL_E2E=1``; in that mode the
    explicit Camoufox/sidecar skip marker is a collection error, not a green
    result with zero real coverage.
    """
    global _REAL_E2E_NODEIDS, _REAL_E2E_EXECUTED, _REAL_E2E_SKIPPED
    _REAL_E2E_NODEIDS = set()
    _REAL_E2E_EXECUTED = 0
    _REAL_E2E_SKIPPED = []
    if os.environ.get("KAHIN_REQUIRE_REAL_E2E") != "1":
        return

    real_items = [item for item in items if _is_real_e2e_item(item)]
    if not real_items:
        raise pytest.UsageError(
            "KAHIN_REQUIRE_REAL_E2E=1 but collection contains no real E2E tests; "
            "the gate cannot pass with zero runtime coverage."
        )

    runtime_skips: list[str] = []
    for item in real_items:
        _REAL_E2E_NODEIDS.add(item.nodeid)
        if any(_skipif_is_active(marker) for marker in item.iter_markers("skipif")):
            runtime_skips.append(item.nodeid)
    if runtime_skips:
        preview = ", ".join(runtime_skips[:5])
        suffix = "..." if len(runtime_skips) > 5 else ""
        raise pytest.UsageError(
            "KAHIN_REQUIRE_REAL_E2E=1 but the real Camoufox/sidecar runtime is unavailable; "
            f"would have skipped {len(runtime_skips)} tests ({preview}{suffix}). "
            "Build/provide the sidecar and run `python -m camoufox fetch`."
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Track real runtime calls and reject runtime skips at session end."""
    global _REAL_E2E_EXECUTED
    if os.environ.get("KAHIN_REQUIRE_REAL_E2E") != "1":
        return
    if report.nodeid not in _REAL_E2E_NODEIDS:
        return
    if report.outcome == "skipped":
        _REAL_E2E_SKIPPED.append(report.nodeid)
    elif report.when == "call":
        _REAL_E2E_EXECUTED += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Make a green required-runtime session impossible without execution."""
    if os.environ.get("KAHIN_REQUIRE_REAL_E2E") != "1" or exitstatus != pytest.ExitCode.OK:
        return
    if _REAL_E2E_SKIPPED:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        message = ", ".join(_REAL_E2E_SKIPPED[:5])
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(
                "REAL E2E GATE FAILED: real Camoufox tests were skipped: " + message
            )
    elif _REAL_E2E_EXECUTED == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(
                "REAL E2E GATE FAILED: zero real Camoufox tests executed"
            )


@pytest.fixture(scope="module")
def schema() -> SchemaEngine:
    s = SchemaEngine()
    s.load()
    return s


@pytest.fixture
def fate() -> Generator[FateDB]:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    db = FateDB(path=Path(path))
    yield db
    Path(path).unlink(missing_ok=True)
