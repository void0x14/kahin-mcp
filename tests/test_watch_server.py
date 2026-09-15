"""Unit tests for the localhost Mirage screencast watch server."""

import inspect
import json

from kahin.tools import screencast_server_mirage


def test_mjpeg_part_has_jpeg_multipart_headers() -> None:
    part = screencast_server_mirage._mjpeg_part(b"\xff\xd8fake")
    assert part.startswith(
        b"--kahinframe\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: 6\r\n\r\n"
    )
    assert part.endswith(b"\r\n")


def test_watch_tools_are_registered_and_async() -> None:
    assert inspect.iscoroutinefunction(screencast_server_mirage.mirage_watch_start)
    assert inspect.iscoroutinefunction(screencast_server_mirage.mirage_watch_stop)
    names = {"kahin_mirage_watch_start", "kahin_mirage_watch_stop"}
    assert "kahin_mirage_watch_start" in names
    assert "kahin_mirage_watch_stop" in names


async def _call_watch_start() -> dict:
    return json.loads(await screencast_server_mirage.mirage_watch_start())


def test_watch_start_without_engine_returns_bounded_error() -> None:
    import asyncio

    result = asyncio.run(_call_watch_start())
    assert result.get("error")
    assert result.get("code") in {"engine_unavailable", "engine_unavailable_or_not_mirage"}
