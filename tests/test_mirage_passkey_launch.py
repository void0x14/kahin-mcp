"""Passkey launch wiring for the existing Mirage Camoufox process."""

from pathlib import Path

import pytest

from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.mirage import Mirage


FAKE_SIDECAR = """\
#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "Browser.health":
        print(json.dumps({"id": request["id"], "result": {"alive": True}}), flush=True)
"""


@pytest.fixture
def launch_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    sidecar = tmp_path / "fake-sidecar.py"
    sidecar.write_text(FAKE_SIDECAR)
    sidecar.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: sidecar)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: sidecar)
    monkeypatch.setattr(
        mirage_mod,
        "launch_options",
        lambda **kwargs: {
            "env": {"CAMOU_CONFIG_TEST": "kept"},
            "firefox_user_prefs": {"test.existing-pref": True},
        },
    )
    captured: dict[str, object] = {}
    create_subprocess_exec = mirage_mod.asyncio.create_subprocess_exec

    async def capture_subprocess(*args: object, **kwargs: object):
        captured["env"] = kwargs["env"]
        return await create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(mirage_mod.asyncio, "create_subprocess_exec", capture_subprocess)
    monkeypatch.setenv("MOZ_MARIONETTE", "1")
    return {"captured": captured, "profile": tmp_path / "persistent-profile"}


@pytest.mark.asyncio
async def test_passkey_mode_sets_marionette_on_existing_launch(launch_capture):
    engine = Mirage()
    profile = launch_capture["profile"]
    captured = launch_capture["captured"]

    await engine.start(
        headless=False,
        passkey_mode=True,
        persistent_profile=True,
        profile_dir=str(profile),
    )
    try:
        port = engine._marionette_port
        assert engine._headless is False
        assert engine._passkey_mode is True
        assert isinstance(port, int) and 0 < port < 65536
        assert captured["env"]["MOZ_MARIONETTE"] == "1"
        assert captured["env"]["CAMOU_CONFIG_TEST"] == "kept"
        user_js = (profile / "user.js").read_text()
        assert f'user_pref("marionette.port", {port});' in user_js
        assert 'user_pref("test.existing-pref", true);' in user_js
    finally:
        await engine.stop()

    assert engine._marionette_port is None
    assert engine._passkey_mode is False


@pytest.mark.asyncio
async def test_default_mode_does_not_enable_marionette(launch_capture):
    engine = Mirage()
    profile = launch_capture["profile"]
    captured = launch_capture["captured"]

    await engine.start(persistent_profile=True, profile_dir=str(profile))
    try:
        assert engine._headless is True
        assert engine._marionette_port is None
        assert engine._passkey_mode is False
        assert "MOZ_MARIONETTE" not in captured["env"]
        user_js = (profile / "user.js").read_text()
        assert "marionette.port" not in user_js
        assert 'user_pref("test.existing-pref", true);' in user_js
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_passkey_mode_requires_persistent_profile():
    engine = Mirage()
    with pytest.raises(ValueError, match="requires persistent_profile=True"):
        await engine.start(passkey_mode=True, persistent_profile=False)
    assert engine._marionette_port is None
    assert engine._passkey_mode is False
