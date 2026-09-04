#!/usr/bin/env python3
"""Local MCP bridge for viewing and controlling a DMXT device."""

from __future__ import annotations

import atexit
import base64
import json
import os
import time
from contextlib import suppress
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server import MCPServer
from mcp.types import ImageContent, TextContent

BASE_URL = os.environ.get("DMXT_URL", "http://127.0.0.1:8023").rstrip("/")
SCREEN_WIDTH = int(os.environ.get("DMXT_SCREEN_WIDTH", "1280"))
SCREEN_HEIGHT = int(os.environ.get("DMXT_SCREEN_HEIGHT", "720"))


class DMXTClient:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.last_sequence = -1

    def _open(
        self, path: str, payload: dict[str, Any] | None = None, timeout: float = 10
    ) -> tuple[bytes, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers
        except HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"DMXT returned HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(
                f"Cannot reach DMXT at {self.base_url}: {error}"
            ) from error

    def json(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body, _ = self._open(path, payload)
        return json.loads(body)

    def snapshot(self, wait_for_next: bool = False) -> tuple[int, bytes]:
        after = self.last_sequence if wait_for_next else -1
        body, headers = self._open(f"/snapshot?after={after}", timeout=5)
        self.last_sequence = int(headers.get("X-DMXT-Sequence", self.last_sequence))
        return self.last_sequence, body

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.json(path, payload)

    def point(self, x: int, y: int, button: int = 0) -> None:
        if not 0 <= x <= 32767 or not 0 <= y <= 32767:
            raise ValueError("x and y must be between 0 and 32767")
        self.post("/api/pointer", {"x": x, "y": y, "button": button})


client = DMXTClient()
mcp = MCPServer(
    "DMXT",
    description="View and control a computer connected to a DMXT Orange Pi KVM.",
    instructions=(
        "Call view_screen before acting and after meaningful input. Coordinates use the "
        "captured screen dimensions reported by view_screen. Release held inputs when finished."
    ),
)


def _image_result(sequence: int, jpeg: bytes) -> list[TextContent | ImageContent]:
    return [
        TextContent(
            type="text",
            text=f"DMXT frame {sequence}; screen coordinates are {SCREEN_WIDTH}x{SCREEN_HEIGHT}.",
        ),
        ImageContent(
            type="image", data=base64.b64encode(jpeg).decode(), mimeType="image/jpeg"
        ),
    ]


def _absolute(x: int, y: int) -> tuple[int, int]:
    if not 0 <= x < SCREEN_WIDTH or not 0 <= y < SCREEN_HEIGHT:
        raise ValueError(
            f"coordinates must be inside the {SCREEN_WIDTH}x{SCREEN_HEIGHT} captured screen"
        )
    return round(x * 32767 / (SCREEN_WIDTH - 1)), round(y * 32767 / (SCREEN_HEIGHT - 1))


@mcp.tool()
def health() -> dict[str, Any]:
    """Check DMXT connectivity and current capture status."""
    return client.json("/health")


@mcp.tool()
def view_screen(wait_for_next_frame: bool = False) -> list[TextContent | ImageContent]:
    """Return the latest screen image. Optionally wait for a frame newer than the last view."""
    sequence, jpeg = client.snapshot(wait_for_next_frame)
    return _image_result(sequence, jpeg)


@mcp.tool()
def move_pointer(x: int, y: int) -> str:
    """Move to an exact pixel coordinate in the captured screen image."""
    client.point(*_absolute(x, y))
    return f"Pointer moved to ({x}, {y})."


@mcp.tool()
def click(x: int, y: int, button: str = "left", count: int = 1) -> str:
    """Click at a captured-screen coordinate with the left, right, or middle button."""
    buttons = {"left": 1, "right": 2, "middle": 4}
    if button not in buttons:
        raise ValueError("button must be left, right, or middle")
    if not 1 <= count <= 3:
        raise ValueError("count must be between 1 and 3")
    px, py = _absolute(x, y)
    for index in range(count):
        client.point(px, py, buttons[button])
        try:
            time.sleep(0.03)
        finally:
            client.point(px, py, 0)
        if index + 1 < count:
            time.sleep(0.08)
    return f"{button.title()} click sent at ({x}, {y}) x{count}."


@mcp.tool()
def drag(
    start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 350
) -> str:
    """Drag with the left mouse button between two captured-screen coordinates."""
    duration_ms = max(50, min(duration_ms, 5000))
    steps = max(2, min(60, round(duration_ms / 20)))
    x, y = start_x, start_y
    try:
        for index in range(steps + 1):
            ratio = index / steps
            x = round(start_x + (end_x - start_x) * ratio)
            y = round(start_y + (end_y - start_y) * ratio)
            client.point(*_absolute(x, y), button=1)
            if index < steps:
                time.sleep(duration_ms / steps / 1000)
    finally:
        client.point(*_absolute(x, y), button=0)
    return f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y})."


@mcp.tool()
def scroll(clicks: int) -> str:
    """Scroll vertically; negative values scroll up and positive values scroll down."""
    remaining = max(-1200, min(clicks, 1200))
    while remaining:
        step = max(-127, min(127, remaining))
        client.post("/api/mouse", {"dx": 0, "dy": 0, "wheel": step})
        remaining -= step
    return f"Scrolled {max(-1200, min(clicks, 1200))} clicks."


@mcp.tool()
def type_text(text: str) -> dict[str, Any]:
    """Type US-keyboard text on the target computer."""
    if len(text) > 512:
        raise ValueError("text must contain at most 512 characters")
    return client.post("/api/text", {"text": text})


@mcp.tool()
def key(key_name: str, modifiers: list[str] | None = None) -> dict[str, Any]:
    """Press and release one named key with optional modifiers such as cmd, shift, alt, or ctrl."""
    return client.post("/api/key", {"key": key_name, "modifiers": modifiers or []})


@mcp.tool()
def key_down(key_name: str) -> dict[str, Any]:
    """Hold a named key or modifier until key_up or release_all is called."""
    return client.post("/api/key-down", {"key": key_name})


@mcp.tool()
def key_up(key_name: str) -> dict[str, Any]:
    """Release one named key or modifier."""
    return client.post("/api/key-up", {"key": key_name})


@mcp.tool()
def release_all() -> dict[str, Any]:
    """Release every key and mouse button held through DMXT."""
    return client.post("/api/release-all", {})


def _release_on_exit() -> None:
    with suppress(RuntimeError):
        client.post("/api/release-all", {})


atexit.register(_release_on_exit)

if __name__ == "__main__":
    mcp.run("stdio")
