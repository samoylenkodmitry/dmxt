#!/usr/bin/env python3
"""Tailnet-only browser control surface for the DMXT HID gadget."""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

KEYS = {chr(ord("a") + index): 0x04 + index for index in range(26)}
KEYS.update({
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "enter": 0x28, "escape": 0x29, "backspace": 0x2A, "tab": 0x2B,
    "space": 0x2C, "minus": 0x2D, "equals": 0x2E, "left_bracket": 0x2F,
    "right_bracket": 0x30, "backslash": 0x31, "semicolon": 0x33,
    "apostrophe": 0x34, "grave": 0x35, "comma": 0x36, "period": 0x37,
    "slash": 0x38, "caps_lock": 0x39, "right": 0x4F, "left": 0x50,
    "down": 0x51, "up": 0x52, "delete": 0x4C, "home": 0x4A,
    "end": 0x4D, "page_up": 0x4B, "page_down": 0x4E,
    **{f"f{index}": 0x3A + index - 1 for index in range(1, 13)},
    "print_screen": 0x46, "scroll_lock": 0x47, "pause": 0x48,
    "insert": 0x49, "num_lock": 0x53, "keypad_divide": 0x54,
    "keypad_multiply": 0x55, "keypad_subtract": 0x56,
    "keypad_add": 0x57, "keypad_enter": 0x58,
    **{f"keypad_{index}": 0x62 if index == 0 else 0x58 + index for index in range(10)},
    "keypad_decimal": 0x63, "non_us_backslash": 0x64, "menu": 0x65,
    "keypad_equals": 0x67,
    **{f"f{index}": 0x68 + index - 13 for index in range(13, 25)},
    "execute": 0x74, "help": 0x75, "select": 0x77, "stop": 0x78,
    "again": 0x79, "undo": 0x7A, "cut": 0x7B, "copy": 0x7C,
    "paste": 0x7D, "find": 0x7E, "mute": 0x7F,
    "volume_up": 0x80, "volume_down": 0x81,
})
MODIFIERS = {
    "left_ctrl": 0x01, "left_shift": 0x02, "left_alt": 0x04, "left_cmd": 0x08,
    "right_ctrl": 0x10, "right_shift": 0x20, "right_alt": 0x40, "right_cmd": 0x80,
    "ctrl": 0x01, "shift": 0x02, "alt": 0x04, "cmd": 0x08,
}
PUNCTUATION = {
    "-": ("minus", 0), "=": ("equals", 0), "[": ("left_bracket", 0),
    "]": ("right_bracket", 0), "\\": ("backslash", 0), ";": ("semicolon", 0),
    "'": ("apostrophe", 0), "`": ("grave", 0), ",": ("comma", 0),
    ".": ("period", 0), "/": ("slash", 0), "!": ("1", 2), "@": ("2", 2),
    "#": ("3", 2), "$": ("4", 2), "%": ("5", 2), "^": ("6", 2),
    "&": ("7", 2), "*": ("8", 2), "(": ("9", 2), ")": ("0", 2),
    "_": ("minus", 2), "+": ("equals", 2), "{": ("left_bracket", 2),
    "}": ("right_bracket", 2), "|": ("backslash", 2), ":": ("semicolon", 2),
    '"': ("apostrophe", 2), "~": ("grave", 2), "<": ("comma", 2),
    ">": ("period", 2), "?": ("slash", 2), " ": ("space", 0),
}


class HID:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.keyboard = Path("/dev/hidg0")
        self.mouse = Path("/dev/hidg1")
        self.pointer = Path("/dev/hidg2")
        self.pointer_x = 0
        self.pointer_y = 0
        self.pointer_button = 0
        self.held_keys: list[str] = []
        self.held_modifiers: set[str] = set()

    def _report(self) -> None:
        modifier = 0
        for name in self.held_modifiers:
            modifier |= MODIFIERS[name]
        usages = [KEYS[name] for name in self.held_keys[:6]]
        usages.extend([0] * (6 - len(usages)))
        with self.keyboard.open("wb", buffering=0) as endpoint:
            endpoint.write(bytes((modifier, 0, *usages)))

    def down(self, key: str) -> None:
        with self.lock:
            if key in MODIFIERS:
                self.held_modifiers.add(key)
            elif key not in self.held_keys:
                if len(self.held_keys) >= 6:
                    raise ValueError("at most six normal keys can be held")
                self.held_keys.append(key)
            self._report()

    def up(self, key: str) -> None:
        with self.lock:
            self.held_modifiers.discard(key)
            if key in self.held_keys:
                self.held_keys.remove(key)
            self._report()

    def release_all(self) -> None:
        with self.lock:
            self.held_keys.clear()
            self.held_modifiers.clear()
            self._report()
            if self.pointer_button:
                with self.pointer.open("wb", buffering=0) as endpoint:
                    endpoint.write(struct.pack("<BHH", 0, self.pointer_x, self.pointer_y))
                self.pointer_button = 0

    def key(self, key: str, modifiers: list[str] | None = None) -> None:
        with self.lock:
            previous_keys = self.held_keys.copy()
            previous_modifiers = self.held_modifiers.copy()
            self.held_keys = [key]
            self.held_modifiers = set(modifiers or [])
            self._report()
            time.sleep(0.025)
            self.held_keys = previous_keys
            self.held_modifiers = previous_modifiers
            self._report()
            time.sleep(0.01)

    def text(self, text: str) -> tuple[int, int]:
        sent = 0
        for character in text:
            if character.isalpha() and character.isascii():
                self.key(character.lower(), ["shift"] if character.isupper() else [])
                sent += 1
            elif character.isdigit():
                self.key(character)
                sent += 1
            elif character in PUNCTUATION:
                key, shift = PUNCTUATION[character]
                self.key(key, ["shift"] if shift else [])
                sent += 1
        return sent, len(text) - sent

    def move(self, dx: int, dy: int, wheel: int = 0) -> None:
        with self.lock, self.mouse.open("wb", buffering=0) as endpoint:
            endpoint.write(bytes((0, dx & 0xFF, dy & 0xFF, wheel & 0xFF)))

    def click(self, button: int) -> None:
        with self.lock, self.mouse.open("wb", buffering=0) as endpoint:
            endpoint.write(bytes((button, 0, 0, 0)))
            time.sleep(0.04)
            endpoint.write(bytes(4))

    def point(self, x: int, y: int, button: int = 0) -> None:
        with self.lock, self.pointer.open("wb", buffering=0) as endpoint:
            self.pointer_x = x
            self.pointer_y = y
            self.pointer_button = button
            endpoint.write(struct.pack("<BHH", button, x, y))


hid = HID()


class VideoFeed:
    """Capture one MJPEG source and fan its latest frame out to web clients."""

    def __init__(self) -> None:
        self.device = os.environ.get("DMXT_VIDEO_DEVICE", "/dev/video1")
        self.size = os.environ.get("DMXT_VIDEO_SIZE", "1280x720")
        self.fps = os.environ.get("DMXT_VIDEO_FPS", "60")
        self.condition = threading.Condition()
        self.frame: bytes | None = None
        self.sequence = 0
        self.updated_at = 0.0
        self.error = "Waiting for video"
        threading.Thread(target=self._capture, name="dmxt-video", daemon=True).start()

    def _publish(self, frame: bytes) -> None:
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.updated_at = time.monotonic()
            self.error = ""
            self.condition.notify_all()

    def _capture(self) -> None:
        command = [
            "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", self.size,
            "-framerate", self.fps, "-i", self.device, "-an", "-c:v", "copy",
            "-f", "mjpeg", "pipe:1",
        ]
        while True:
            process: subprocess.Popen[bytes] | None = None
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                assert process.stdout is not None
                buffer = bytearray()
                while chunk := process.stdout.read(65536):
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            if len(buffer) > 1:
                                del buffer[:-1]
                            break
                        end = buffer.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            if start:
                                del buffer[:start]
                            if len(buffer) > 8 * 1024 * 1024:
                                buffer.clear()
                            break
                        self._publish(bytes(buffer[start:end + 2]))
                        del buffer[:end + 2]
                raise RuntimeError("Video capture stopped")
            except (OSError, RuntimeError) as error:
                with self.condition:
                    self.error = str(error)
                    self.condition.notify_all()
            finally:
                if process and process.poll() is None:
                    process.terminate()
            time.sleep(2)

    def status(self) -> dict[str, object]:
        with self.condition:
            age = time.monotonic() - self.updated_at if self.updated_at else None
            return {
                "ok": age is not None and age < 5,
                "device": self.device,
                "size": self.size,
                "fps": int(self.fps),
                "age_ms": round(age * 1000) if age is not None else None,
                "error": self.error or None,
            }

    def snapshot(self, after: int) -> tuple[int, bytes | None]:
        with self.condition:
            if self.sequence <= after:
                self.condition.wait_for(lambda: self.sequence > after, timeout=3)
            return self.sequence, self.frame

    def stream(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        handler.send_header("Pragma", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        sequence = -1
        try:
            while True:
                with self.condition:
                    if not self.condition.wait_for(
                        lambda previous=sequence: self.sequence != previous, timeout=10
                    ):
                        return
                    sequence, frame = self.sequence, self.frame
                if frame is None:
                    continue
                header = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode() + b"\r\n\r\n"
                )
                handler.wfile.write(header + frame + b"\r\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


class FastVideoFeed:
    """Compatibility facade for the native capture service."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def status(self) -> dict[str, object]:
        try:
            with urlopen(f"{self.base_url}/health", timeout=1) as response:
                return json.loads(response.read())["video"]
        except (OSError, KeyError, ValueError) as error:
            return {"ok": False, "error": str(error)}

    def snapshot(self, after: int) -> tuple[int, bytes | None]:
        try:
            with urlopen(f"{self.base_url}/snapshot?after={after}", timeout=4) as response:
                return int(response.headers["X-DMXT-Sequence"]), response.read()
        except (OSError, TypeError, ValueError):
            return after, None

    def stream(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        sequence = 0
        try:
            while True:
                sequence, frame = self.snapshot(sequence)
                if frame is None:
                    continue
                header = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                )
                handler.wfile.write(header + frame + b"\r\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


video_feed: VideoFeed | FastVideoFeed | None = None

KEYBOARD_ROWS = [
    [("Esc", "escape"), *[(f"F{i}", f"f{i}") for i in range(1, 13)], ("Print", "print_screen"), ("Scroll", "scroll_lock"), ("Pause", "pause")],
    [("`", "grave"), *[(str(i), str(i)) for i in range(1, 10)], ("0", "0"), ("-", "minus"), ("=", "equals"), ("Backspace", "backspace"), ("Insert", "insert"), ("Home", "home"), ("PgUp", "page_up"), ("Num", "num_lock"), ("÷", "keypad_divide"), ("×", "keypad_multiply"), ("−", "keypad_subtract")],
    [("Tab", "tab"), *[(letter.upper(), letter) for letter in "qwertyuiop"], ("[", "left_bracket"), ("]", "right_bracket"), ("\\", "backslash"), ("Delete", "delete"), ("End", "end"), ("PgDn", "page_down"), ("KP7", "keypad_7"), ("KP8", "keypad_8"), ("KP9", "keypad_9"), ("+", "keypad_add")],
    [("Caps", "caps_lock"), *[(letter.upper(), letter) for letter in "asdfghjkl"], (";", "semicolon"), ("'", "apostrophe"), ("Return", "enter"), ("KP4", "keypad_4"), ("KP5", "keypad_5"), ("KP6", "keypad_6")],
    [("⇧ Left", "left_shift"), ("ISO", "non_us_backslash"), *[(letter.upper(), letter) for letter in "zxcvbnm"], (",", "comma"), (".", "period"), ("/", "slash"), ("⇧ Right", "right_shift"), ("↑", "up"), ("KP1", "keypad_1"), ("KP2", "keypad_2"), ("KP3", "keypad_3"), ("KP Enter", "keypad_enter")],
    [("⌃ Left", "left_ctrl"), ("⌥ Left", "left_alt"), ("⌘ Left", "left_cmd"), ("Space", "space"), ("⌘ Right", "right_cmd"), ("⌥ Right", "right_alt"), ("Menu", "menu"), ("⌃ Right", "right_ctrl"), ("←", "left"), ("↓", "down"), ("→", "right"), ("KP0", "keypad_0"), ("KP .", "keypad_decimal")],
]
EXTENDED_KEYS = [
    *[(f"F{i}", f"f{i}") for i in range(13, 25)],
    ("Execute", "execute"), ("Help", "help"), ("Select", "select"),
    ("Stop", "stop"), ("Again", "again"), ("Undo", "undo"),
    ("Cut", "cut"), ("Copy", "copy"), ("Paste", "paste"), ("Find", "find"),
    ("Mute", "mute"), ("Vol −", "volume_down"), ("Vol +", "volume_up"),
]


def keyboard_html() -> str:
    rows = []
    for row in KEYBOARD_ROWS:
        buttons = "".join(f'<button class="kbd" data-hid="{key}">{label}</button>' for label, key in row)
        rows.append(f'<div class="keyrow">{buttons}</div>')
    extended = "".join(f'<button class="kbd" data-hid="{key}">{label}</button>' for label, key in EXTENDED_KEYS)
    rows.append(f'<details class="advanced"><summary>Extended keys</summary><div class="keyrow extended">{extended}</div></details>')
    return "".join(rows)


def page() -> bytes:
    document = r"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>DMXT</title>
<style>
:root { color-scheme:dark; font:16px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#070b14; color:#f5f7fb; --panel:rgba(19,27,43,.88); --line:#293650; --muted:#95a3ba; --cyan:#3dd9eb; --blue:#4776ff; --danger:#ff6b7a }
* { box-sizing:border-box } html,body { min-height:100%; overscroll-behavior:none } body { margin:0; background:radial-gradient(circle at 12% -8%,#183158 0,transparent 34rem),radial-gradient(circle at 92% 0,#22205c 0,transparent 30rem),#070b14; } body.touching { overflow:hidden; touch-action:none }
button,input { font:inherit } button { min-height:44px; border:1px solid var(--line); border-radius:12px; background:#182238; color:#f7f9fc; padding:9px 14px; cursor:pointer; transition:border-color .16s,background .16s,transform .08s,box-shadow .16s; touch-action:manipulation } button:hover:not(:disabled) { border-color:#526b98; background:#202e4a } button:active:not(:disabled),.kbd.held { transform:translateY(1px); background:#284fb7; border-color:#73c9ff; box-shadow:0 0 0 2px rgba(61,217,235,.18) } button:disabled { opacity:.42; cursor:not-allowed }
.topbar { position:sticky; top:0; z-index:10; display:flex; align-items:center; justify-content:space-between; gap:18px; padding:15px max(20px,calc((100vw - 1420px)/2)); border-bottom:1px solid rgba(70,92,130,.34); background:rgba(7,11,20,.82); backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px) }.brand { display:flex; align-items:center; gap:12px }.mark { width:42px; height:42px; display:grid; place-items:center; border-radius:13px; background:linear-gradient(145deg,var(--cyan),var(--blue)); color:#06111d; font-weight:900; letter-spacing:-1px; box-shadow:0 9px 28px rgba(50,117,255,.3) }.brand h1 { margin:0; font-size:20px; letter-spacing:.12em }.brand small { color:var(--muted); display:block; margin-top:2px }
#status { display:flex; align-items:center; gap:8px; max-width:48%; padding:8px 12px; border:1px solid #245b55; border-radius:999px; background:#0d2a29; color:#9ef2dd; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }#status::before { content:""; width:8px; height:8px; flex:0 0 auto; border-radius:50%; background:#49e3ae; box-shadow:0 0 0 4px rgba(73,227,174,.12) }#status.busy { color:#dbe6ff; border-color:#354a70; background:#152139 }#status.busy::before { background:#68a1ff }#status.error { color:#ffc0c7; border-color:#71313c; background:#30141b }#status.error::before { background:var(--danger) }
main { width:min(1420px,100%); margin:auto; padding:22px }.safety { display:flex; align-items:center; justify-content:space-between; gap:22px; margin-bottom:18px; padding:14px 16px; border:1px solid #684327; border-radius:16px; background:linear-gradient(100deg,rgba(92,48,19,.72),rgba(36,27,28,.86)) }.safety.enabled { border-color:#235d57; background:linear-gradient(100deg,rgba(14,66,61,.68),rgba(18,31,41,.86)) }.safetyCopy { display:flex; align-items:center; gap:12px }.safetyIcon { width:36px; height:36px; flex:0 0 auto; display:grid; place-items:center; border-radius:11px; background:rgba(255,255,255,.08) }.safety strong { display:block; margin-bottom:3px }.safety p { margin:0; color:#c5ad9a; font-size:13px }.safety.enabled p { color:#9fd7cc }.relayButton { white-space:nowrap }
.videoPanel { margin-bottom:18px }.videoTop { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:15px 18px }.videoTop>div,.videoTools { min-width:0 }.videoTop h2 { margin:0; font-size:16px }.videoTools { display:flex; align-items:center; gap:10px }.videoState { display:flex; align-items:center; gap:7px; color:var(--muted); font-size:12px }.videoState::before { content:""; width:7px; height:7px; flex:0 0 auto; border-radius:50%; background:#f4b942 }.videoState.live { color:#9ef2dd }.videoState.live::before { background:#49e3ae; box-shadow:0 0 0 4px rgba(73,227,174,.1) }#clarityToggle.active { border-color:#2f7a87; background:#123c49; color:#b8f6ff }.videoStage { position:relative; display:grid; place-items:center; width:min(100%,128vh); margin-inline:auto; aspect-ratio:16/9; overflow:hidden; outline:none; background:#02040a; cursor:default; touch-action:none; user-select:none; -webkit-user-select:none }.videoStage img,.videoStage canvas { display:block; width:100%; height:100%; object-fit:contain; pointer-events:none }.videoStage.active { box-shadow:inset 0 0 0 2px var(--cyan) }.videoHint { position:absolute; left:50%; bottom:14px; transform:translateX(-50%); max-width:calc(100% - 24px); padding:7px 11px; border:1px solid rgba(124,150,190,.28); border-radius:999px; background:rgba(5,10,20,.72); color:#d3deef; font-size:12px; white-space:nowrap; backdrop-filter:blur(8px); pointer-events:none }
.controlGrid { display:grid; grid-template-columns:minmax(0,1.55fr) minmax(320px,.7fr); gap:18px; align-items:stretch }.sideColumn { display:grid; gap:18px; grid-template-rows:auto 1fr }.panel { border:1px solid rgba(55,72,103,.82); border-radius:20px; background:linear-gradient(150deg,rgba(23,33,52,.94),rgba(12,18,31,.96)); box-shadow:0 18px 50px rgba(0,0,0,.22); overflow:hidden }.panelBody { padding:18px }.panelHeader { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:14px }.panelHeader h2 { margin:0; font-size:16px; letter-spacing:.02em }.panelHeader p,.hint { margin:4px 0 0; color:var(--muted); font-size:13px; line-height:1.45 }.eyebrow { color:var(--cyan); font-size:11px; font-weight:800; letter-spacing:.13em; text-transform:uppercase }
#pad { height:clamp(330px,45vh,520px); display:grid; place-items:center; margin:0; border:1px solid #3c4e6d; border-radius:17px; outline:none; text-align:center; background:radial-gradient(circle at 50% 42%,rgba(61,217,235,.09),transparent 13rem),linear-gradient(145deg,#111d31,#0b1220); touch-action:none!important; user-select:none; -webkit-user-select:none; -webkit-touch-callout:none; transition:border-color .2s,box-shadow .2s }.padContent { max-width:380px; padding:24px }.padGlyph { width:62px; height:62px; display:grid; place-items:center; margin:0 auto 16px; border:1px solid #395171; border-radius:20px; background:#16263e; color:var(--cyan); font-size:27px }.padContent strong { display:block; font-size:18px; margin-bottom:7px }.padContent span { color:var(--muted); font-size:13px; line-height:1.45 }#pad.active { border-color:var(--cyan); box-shadow:inset 0 0 0 1px var(--cyan),0 0 35px rgba(61,217,235,.1) }#pad.active .padGlyph { background:#12363e; border-color:#3dd9eb }
.mouseActions { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px; margin-top:10px }.mouseActions button { padding-inline:8px }.primary { border-color:#2a7281; background:linear-gradient(145deg,#12616d,#174f70) }.danger { border-color:#71313c; background:#351820; color:#ffc3c9 }
.textRow { display:flex; gap:8px; margin-top:15px }.textRow input { min-width:0; flex:1; height:48px; padding:0 14px; border:1px solid #33425f; border-radius:12px; outline:none; background:#0c1424; color:#fff }.textRow input:focus { border-color:var(--cyan); box-shadow:0 0 0 3px rgba(61,217,235,.12) }.textRow input:disabled { opacity:.5 }.textRow button { flex:0 0 auto }
.quickGrid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-top:15px }.quickGrid button { padding:7px 6px; min-width:0 }.quickGrid .wide { grid-column:span 2 }
.keyboardPanel { margin-top:18px }.keyboardTop { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 18px 0 }.keyboardWrap { overflow-x:auto; padding:10px 18px 18px; overscroll-behavior-x:contain; -webkit-overflow-scrolling:touch; scrollbar-color:#425878 transparent }.keyboardInner { min-width:1240px }.scrollCue { position:sticky; left:0; width:fit-content; margin:0 0 8px; color:var(--muted); font-size:12px }.keyrow { display:flex; gap:5px; margin:5px 0 }.kbd { min-width:48px; min-height:48px; padding:6px; white-space:nowrap; user-select:none; -webkit-user-select:none; -webkit-touch-callout:none; touch-action:none }.kbd[data-hid="space"] { min-width:290px }.advanced { margin-top:14px; border-top:1px solid #26344d; padding-top:12px }.advanced summary { position:sticky; left:0; width:fit-content; cursor:pointer; color:#b7c5db; font-size:13px }.extended { margin-top:10px }.foot { padding:22px; text-align:center; color:#63708a; font-size:12px }
@media (max-width:900px) { .controlGrid { grid-template-columns:1fr }.sideColumn { grid-template-columns:1fr 1fr; grid-template-rows:auto }.topbar { padding-inline:16px } }
@media (max-width:640px) { .topbar { padding:11px 12px; overflow:hidden }.brand { min-width:0 }.mark { width:38px; height:38px; flex:0 0 auto; border-radius:11px }.brand h1 { font-size:18px }.brand small { display:none }#status { min-width:0; max-width:46%; flex:0 1 auto; font-size:12px; padding:7px 9px }main { padding:12px }.videoPanel,.safety { margin-bottom:12px }.videoTop { flex-wrap:wrap; align-items:flex-start; gap:10px; padding:12px 14px }.videoTools { width:100%; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px }.videoState { grid-column:1/-1; font-size:11px }.videoTools button { min-width:0; min-height:38px; padding:7px 5px; font-size:11px; white-space:nowrap }.videoHint { bottom:9px; font-size:10px; padding:6px 9px }.safety { display:block; padding:13px }.safetyCopy,.safetyCopy>div:last-child { min-width:0 }.safety p { line-height:1.35; overflow-wrap:anywhere }.relayButton { width:100%; margin-top:12px }.controlGrid,.sideColumn { gap:12px }.sideColumn { grid-template-columns:1fr }.panel { border-radius:17px }.panelBody { padding:14px }.panelHeader { display:block; margin-bottom:10px }.panelHeader p { max-width:none; overflow-wrap:anywhere }#pad { height:min(46vh,390px); min-height:270px; border-radius:14px }.padGlyph { width:54px; height:54px; margin-bottom:12px }.padContent strong { font-size:16px }.mouseActions { grid-template-columns:repeat(3,1fr) }.mouseActions button { font-size:13px }.textRow { display:grid; grid-template-columns:1fr auto }.quickGrid { grid-template-columns:repeat(4,1fr) }.keyboardPanel { margin-top:12px }.keyboardTop { align-items:flex-start; padding:14px 14px 0 }.keyboardTop .panelHeader p { max-width:210px }.keyboardWrap { padding:9px 14px 14px }.keyboardInner { min-width:1120px }.kbd { min-width:44px; min-height:46px; font-size:13px }.foot { padding:16px } }
</style></head><body>
<header class="topbar"><div class="brand"><div class="mark">DX</div><div><h1>DMXT</h1><small>Remote input console</small></div></div><div id="status" class="busy" role="status" aria-live="polite">Connecting…</div></header>
<main>
<section class="panel videoPanel"><div class="videoTop"><div><span class="eyebrow">Display</span><h2>Live HDMI</h2></div><div class="videoTools"><span id="videoState" class="videoState">Connecting</span><button id="clarityToggle" class="active" aria-pressed="true" title="Progressively cleans and sharpens pixels while the picture is still">Clarity: Auto</button><button id="audioToggle">Enable sound</button><button id="fullscreenVideo">Fullscreen</button></div></div><div id="videoStage" class="videoStage" data-pointer-surface tabindex="0" aria-label="Live HDMI video and pointer control surface"><canvas id="videoFeed" width="1280" height="720" aria-label="Live HDMI capture"></canvas><audio id="remoteAudio" autoplay playsinline></audio><div class="videoHint">Desktop: point directly · Touch: drag</div></div></section>
<section id="safety" class="safety"><div class="safetyCopy"><div class="safetyIcon">◆</div><div><strong id="safetyTitle">Safe mode</strong><p id="safetyText">Relay is off while this browser may be on the USB target.</p></div></div><button id="relay" class="relayButton">Enable desktop relay</button></section>
<div class="controlGrid">
<section class="panel"><div class="panelBody"><div class="panelHeader"><div><span class="eyebrow">Pointer</span><h2>Trackpad</h2></div><p>Tap to click · Esc releases desktop capture</p></div><div id="pad" data-pointer-surface tabindex="0"><div class="padContent"><div class="padGlyph">↗</div><strong>Move the pointer</strong><span>Drag on touchscreens. On desktop, click once to capture the mouse and keyboard.</span></div></div><div class="mouseActions"><button class="primary" data-click="1">● Left</button><button data-click="2">◉ Right</button><button data-click="4">● Middle</button><button data-wheel="-3">↑ Scroll</button><button data-wheel="3">↓ Scroll</button></div></div></section>
<div class="sideColumn">
<section class="panel"><div class="panelBody"><div class="panelHeader"><div><span class="eyebrow">Type</span><h2>Send text</h2></div></div><p class="hint">Enter text on your phone and send it to the focused field.</p><div class="textRow"><input id="typing" aria-label="Text to send" autocapitalize="none" autocomplete="off" autocorrect="off" spellcheck="false" enterkeyhint="send" placeholder="Type something…"><button class="primary" id="sendText">Send</button></div></div></section>
<section class="panel"><div class="panelBody"><div class="panelHeader"><div><span class="eyebrow">Shortcuts</span><h2>Quick keys</h2></div></div><div class="quickGrid"><button class="kbd" data-hid="escape">Esc</button><button class="kbd" data-hid="tab">Tab</button><button class="kbd" data-hid="backspace">⌫</button><button class="kbd" data-hid="enter">Return</button><button class="kbd" data-hid="left_cmd">⌘</button><button class="kbd" data-hid="left_alt">⌥</button><button class="kbd" data-hid="left_ctrl">⌃</button><button class="kbd" data-hid="left_shift">⇧</button><button class="kbd" data-hid="left">←</button><button class="kbd" data-hid="down">↓</button><button class="kbd" data-hid="up">↑</button><button class="kbd" data-hid="right">→</button></div></div></section>
</div></div>
<section class="panel keyboardPanel"><div class="keyboardTop"><div class="panelHeader"><div><span class="eyebrow">Keyboard</span><h2>Full layout</h2><p>Keys are holdable. Hold a modifier, then press another key.</p></div></div><button id="releaseAll" class="danger">Release all</button></div><div class="keyboardWrap"><div class="keyboardInner"><div class="scrollCue">← drag sideways to explore →</div>__KEYBOARD__</div></div></section>
</main><footer class="foot">DMXT · Tailnet-secured USB HID control</footer>
<script>
const status=document.querySelector('#status'), safety=document.querySelector('#safety'), pad=document.querySelector('#pad'), typing=document.querySelector('#typing'), relay=document.querySelector('#relay'), sendText=document.querySelector('#sendText'), releaseAll=document.querySelector('#releaseAll'), videoFeed=document.querySelector('#videoFeed'), videoStage=document.querySelector('#videoStage'), videoState=document.querySelector('#videoState'), clarityToggle=document.querySelector('#clarityToggle'), audioToggle=document.querySelector('#audioToggle'), remoteAudio=document.querySelector('#remoteAudio'), fullscreenVideo=document.querySelector('#fullscreenVideo'), pointerSurfaces=document.querySelectorAll('[data-pointer-surface]'); let pointer=null, moved=false, mx=0,my=0, moveSending=false, wheelPixels=0,wheelSending=false, pointQueue=[],pointSending=false, relayEnabled=navigator.maxTouchPoints>0||matchMedia('(pointer:coarse)').matches,audioPC=null,inputSocket=null; const held=new Set(), keyQueues=new Map(),delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function apiPath(path) { const prefix=location.pathname.startsWith('/dmxt')?'/dmxt':location.pathname.startsWith('/kvm')?'/kvm':''; return prefix+path }
function fastSocketUrl(path){const scheme=location.protocol==='https:'?'wss:':'ws:';if(location.pathname.startsWith('/dmxt')||location.pathname.startsWith('/kvm'))return `${scheme}//${location.host}/dmxt-fast${path}`;return `${scheme}//${location.hostname}:8024${path}`}
function videoWorkerMain(){
const width=1280,height=720;let canvas,gl,ctx,latest,busy=false,clarity=true,resetHistory=true,currentTexture,historyTextures,framebuffers,historyIndex=0,historyReady=false,accumulateProgram,displayProgram;
const vertexSource=`#version 300 es
out vec2 uv;
void main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));uv=p;gl_Position=vec4(p*2.0-1.0,0.0,1.0);}`;
const accumulateSource=`#version 300 es
precision mediump float;
in vec2 uv;out vec4 color;uniform sampler2D currentFrame;uniform sampler2D historyFrame;uniform vec2 texel;uniform float resetFrame;
float changeAt(vec2 point){vec3 current=texture(currentFrame,vec2(point.x,1.0-point.y)).rgb;vec3 history=texture(historyFrame,point).rgb;vec3 difference=abs(current-history);return max(difference.r,max(difference.g,difference.b));}
void main(){vec2 sourceUv=vec2(uv.x,1.0-uv.y);vec3 current=texture(currentFrame,sourceUv).rgb;if(resetFrame>0.5){color=vec4(current,0.0);return;}vec4 history=texture(historyFrame,uv);vec2 radius=texel*2.0;float delta=changeAt(uv);delta=max(delta,changeAt(uv+vec2(radius.x,0.0)));delta=max(delta,changeAt(uv-vec2(radius.x,0.0)));delta=max(delta,changeAt(uv+vec2(0.0,radius.y)));delta=max(delta,changeAt(uv-vec2(0.0,radius.y)));float stable=1.0-smoothstep(0.010,0.035,delta);float confidence=stable*min(1.0,history.a+0.12);float historyWeight=stable*mix(0.18,0.84,confidence);color=vec4(mix(current,history.rgb,historyWeight),confidence);}`;
const displaySource=`#version 300 es
precision mediump float;
in vec2 uv;out vec4 color;uniform sampler2D picture;uniform vec2 texel;uniform float sharpness;uniform float flipY;
void main(){vec2 sampleUv=vec2(uv.x,mix(uv.y,1.0-uv.y,flipY));vec3 center=texture(picture,sampleUv).rgb;vec3 cross=texture(picture,sampleUv+vec2(texel.x,0.0)).rgb+texture(picture,sampleUv-vec2(texel.x,0.0)).rgb+texture(picture,sampleUv+vec2(0.0,texel.y)).rgb+texture(picture,sampleUv-vec2(0.0,texel.y)).rgb;vec3 sharpened=center+(center*4.0-cross)*sharpness;color=vec4(clamp(sharpened,0.0,1.0),1.0);}`;
function compile(type,source){const shader=gl.createShader(type);gl.shaderSource(shader,source);gl.compileShader(shader);if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(shader));return shader}
function program(fragment){const result=gl.createProgram();gl.attachShader(result,compile(gl.VERTEX_SHADER,vertexSource));gl.attachShader(result,compile(gl.FRAGMENT_SHADER,fragment));gl.linkProgram(result);if(!gl.getProgramParameter(result,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(result));return result}
function texture(){const result=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,result);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA8,width,height,0,gl.RGBA,gl.UNSIGNED_BYTE,null);return result}
function target(textureValue){const result=gl.createFramebuffer();gl.bindFramebuffer(gl.FRAMEBUFFER,result);gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,textureValue,0);if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)throw new Error('Incomplete video framebuffer');return result}
function bind(unit,textureValue,name,programValue){gl.activeTexture(gl.TEXTURE0+unit);gl.bindTexture(gl.TEXTURE_2D,textureValue);gl.uniform1i(gl.getUniformLocation(programValue,name),unit)}
function accumulate(destination,reset){gl.bindFramebuffer(gl.FRAMEBUFFER,framebuffers[destination]);gl.useProgram(accumulateProgram);bind(0,currentTexture,'currentFrame',accumulateProgram);bind(1,historyTextures[reset?1-destination:historyIndex],'historyFrame',accumulateProgram);gl.uniform2f(gl.getUniformLocation(accumulateProgram,'texel'),1/width,1/height);gl.uniform1f(gl.getUniformLocation(accumulateProgram,'resetFrame'),reset?1:0);gl.drawArrays(gl.TRIANGLES,0,3)}
function display(textureValue,sharpness,flipY){gl.bindFramebuffer(gl.FRAMEBUFFER,null);gl.useProgram(displayProgram);bind(0,textureValue,'picture',displayProgram);gl.uniform2f(gl.getUniformLocation(displayProgram,'texel'),1/width,1/height);gl.uniform1f(gl.getUniformLocation(displayProgram,'sharpness'),sharpness);gl.uniform1f(gl.getUniformLocation(displayProgram,'flipY'),flipY);gl.drawArrays(gl.TRIANGLES,0,3)}
function initialise(value){canvas=value;canvas.width=width;canvas.height=height;try{gl=canvas.getContext('webgl2',{alpha:false,antialias:false,depth:false,stencil:false,desynchronized:true,preserveDrawingBuffer:false});if(!gl)throw new Error('WebGL 2 unavailable');gl.viewport(0,0,width,height);gl.bindVertexArray(gl.createVertexArray());accumulateProgram=program(accumulateSource);displayProgram=program(displaySource);currentTexture=texture();historyTextures=[texture(),texture()];framebuffers=historyTextures.map(target);gl.bindFramebuffer(gl.FRAMEBUFFER,null);postMessage({type:'renderer',mode:'clarity'})}catch(error){gl=null;ctx=canvas.getContext('2d',{alpha:false,desynchronized:true});postMessage({type:'renderer',mode:'direct'})}}
function render(bitmap){if(!gl){ctx.drawImage(bitmap,0,0,width,height);return}gl.bindTexture(gl.TEXTURE_2D,currentTexture);gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,false);gl.texSubImage2D(gl.TEXTURE_2D,0,0,0,gl.RGBA,gl.UNSIGNED_BYTE,bitmap);if(!clarity){display(currentTexture,0,1);resetHistory=true;return}if(resetHistory||!historyReady){historyIndex=0;accumulate(historyIndex,true);historyReady=true;resetHistory=false}else{const destination=1-historyIndex;accumulate(destination,false);historyIndex=destination}display(historyTextures[historyIndex],0.10,0)}
async function pump(){busy=true;while(latest){const data=latest;latest=null;try{const bitmap=await createImageBitmap(new Blob([data],{type:'image/jpeg'}));if(!latest)render(bitmap);bitmap.close()}catch(error){}}busy=false}
onmessage=event=>{const data=event.data;if(data.canvas){initialise(data.canvas);return}if(data.clarity!==undefined){clarity=data.clarity;resetHistory=true;return}if(data.frame){latest=data.frame;if(!busy)pump()}};
}
let videoWorker=null,videoPending=null,videoDrawing=false,videoContext=null,clarityEnabled=localStorage.getItem('dmxtClarity')!=='off';
function updateClarityButton(){clarityToggle.textContent=clarityEnabled?'Clarity: Auto':'Clarity: Off';clarityToggle.classList.toggle('active',clarityEnabled);clarityToggle.setAttribute('aria-pressed',String(clarityEnabled));if(videoWorker)videoWorker.postMessage({clarity:clarityEnabled})}
function initVideoRenderer(){try{if(videoFeed.transferControlToOffscreen&&window.Worker){const canvas=videoFeed.transferControlToOffscreen(),source=`(${videoWorkerMain.toString()})()`;videoWorker=new Worker(URL.createObjectURL(new Blob([source],{type:'text/javascript'})));videoWorker.onmessage=event=>{if(event.data.type==='renderer'&&event.data.mode==='direct'){clarityToggle.disabled=true;clarityToggle.textContent='Clarity unavailable'}else if(event.data.type==='renderer')updateClarityButton()};videoWorker.postMessage({canvas,clarity:clarityEnabled},[canvas]);return}}catch(error){}videoContext=videoFeed.getContext('2d',{alpha:false,desynchronized:true});clarityToggle.disabled=true;clarityToggle.textContent='Clarity unavailable'}
clarityToggle.onclick=()=>{clarityEnabled=!clarityEnabled;localStorage.setItem('dmxtClarity',clarityEnabled?'auto':'off');updateClarityButton()};updateClarityButton();initVideoRenderer();
async function drawVideoFrame(data){if(videoWorker){videoWorker.postMessage({frame:data},[data]);return}videoPending=data;if(videoDrawing)return;videoDrawing=true;while(videoPending){const current=videoPending;videoPending=null;try{const bitmap=await createImageBitmap(new Blob([current],{type:'image/jpeg'}));if(!videoPending)videoContext.drawImage(bitmap,0,0,1280,720);bitmap.close()}catch(error){}}videoDrawing=false}
async function connectVideo(){while(true){videoState.textContent='Connecting';videoState.classList.remove('live');try{const ws=new WebSocket(fastSocketUrl('/video'));ws.binaryType='arraybuffer';await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});await new Promise(resolve=>{ws.onmessage=event=>{if(!document.hidden)drawVideoFrame(event.data);videoState.textContent='Live · native';videoState.classList.add('live')};ws.onclose=resolve;ws.onerror=resolve})}catch(error){}videoState.textContent='Reconnecting';videoState.classList.remove('live');await delay(200)}}fullscreenVideo.onclick=()=>{const request=videoStage.requestFullscreen||videoStage.webkitRequestFullscreen;if(request)request.call(videoStage)};connectVideo();
function iceComplete(pc){if(pc.iceGatheringState==='complete')return Promise.resolve();return new Promise(resolve=>{const changed=()=>{if(pc.iceGatheringState==='complete'){pc.removeEventListener('icegatheringstatechange',changed);resolve()}};pc.addEventListener('icegatheringstatechange',changed)})}async function stopAudio(){if(audioPC){audioPC.close();audioPC=null}remoteAudio.srcObject=null;audioToggle.textContent='Enable sound';audioToggle.classList.remove('primary')}async function toggleAudio(){if(audioPC){await stopAudio();return}audioToggle.disabled=true;audioToggle.textContent='Connecting…';const pc=new RTCPeerConnection();audioPC=pc;try{pc.addTransceiver('audio',{direction:'recvonly'});pc.ontrack=event=>{remoteAudio.srcObject=event.streams[0]};pc.onconnectionstatechange=()=>{if(['failed','closed','disconnected'].includes(pc.connectionState))stopAudio()};await pc.setLocalDescription(await pc.createOffer());await Promise.race([iceComplete(pc),delay(600)]);const response=await fetch(apiPath('/audio/webrtc'),{method:'POST',headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp});if(!response.ok)throw new Error(await response.text());await pc.setRemoteDescription({type:'answer',sdp:await response.text()});await remoteAudio.play();audioToggle.textContent='Sound on';audioToggle.classList.add('primary')}catch(error){await stopAudio();setStatus('Sound unavailable','error')}finally{audioToggle.disabled=false}}audioToggle.onclick=toggleAudio;
function setStatus(message,tone='ok'){status.textContent=message;status.className=tone==='error'?'error':tone==='busy'?'busy':''}
async function send(path,data={},quiet=false){if(!relayEnabled){setStatus('Safe mode · relay off','error');return false}if(!quiet)setStatus('Sending…','busy');try{const r=await fetch(apiPath(path),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}),j=await r.json(),message=j.sent===undefined?'Sent':`Sent ${j.sent}${j.unsupported?` · ${j.unsupported} skipped`:''}`;if(!quiet||!j.ok)setStatus(j.ok?message:j.error,j.ok?'ok':'error');return j.ok}catch(e){setStatus('Connection failed','error');return false}}
async function checkConnection(){setStatus('Connecting…','busy');try{const r=await fetch(apiPath('/health'),{cache:'no-store'}),j=await r.json();setStatus(j.ok?'DMXT connected':'DMXT unavailable',j.ok?'ok':'error')}catch(e){setStatus('Connection failed','error')}}
async function setRelay(enabled) { relayEnabled=enabled;safety.classList.toggle('enabled',enabled);document.querySelector('#safetyTitle').textContent=enabled?'Relay enabled':'Safe mode';document.querySelector('#safetyText').textContent=enabled?'All input is relayed · ⌘ Esc releases control.':'Relay is off while this browser may be on the USB target.';relay.textContent=enabled?'Disable relay':'Enable desktop relay';relay.className=enabled?'relayButton danger':'relayButton';typing.disabled=!enabled;sendText.disabled=!enabled;document.querySelectorAll('[data-click],[data-wheel],[data-hid],#releaseAll').forEach(button=>button.disabled=!enabled);if(enabled){videoStage.focus();if(navigator.keyboard?.lock)try{await navigator.keyboard.lock()}catch(error){}}else{if(navigator.keyboard?.unlock)navigator.keyboard.unlock();releaseEverything(true);if(document.pointerLockElement)document.exitPointerLock()} }
relay.onclick=()=>setRelay(!relayEnabled); setRelay(relayEnabled);
function rawInput(report){if(!inputSocket||inputSocket.readyState!==WebSocket.OPEN)return false;inputSocket.send(Uint8Array.from(report));return true}
async function connectInput(){while(true){let ws;try{ws=new WebSocket(fastSocketUrl('/input'));await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});inputSocket=ws;if(relayEnabled)sendKeyboardReport();setStatus('DMXT connected · native');await new Promise(resolve=>{ws.onclose=resolve;ws.onerror=resolve})}catch(error){}if(inputSocket===ws)inputSocket=null;await delay(200)}}
async function flushMove(){if(moveSending)return;moveSending=true;while(mx||my){const x=Math.max(-127,Math.min(127,Math.round(mx))),y=Math.max(-127,Math.min(127,Math.round(my)));mx-=x;my-=y;if(x||y){if(rawInput([1,x&255,y&255,0]))continue;await send('/api/mouse',{dx:x,dy:y},true)}}moveSending=false;if(mx||my)flushMove()}function queueMove(dx,dy){mx+=dx;my+=dy;flushMove()}
async function flushWheel(){if(wheelSending)return;wheelSending=true;while(Math.abs(wheelPixels)>=40){const wheel=Math.max(-12,Math.min(12,Math.trunc(wheelPixels/40)));wheelPixels-=wheel*40;if(rawInput([1,0,0,wheel&255]))continue;await send('/api/mouse',{dx:0,dy:0,wheel},true)}wheelSending=false;if(Math.abs(wheelPixels)>=40)flushWheel()}function queueWheel(e){e.preventDefault();const scale=e.deltaMode===1?16:e.deltaMode===2?400:1;wheelPixels+=e.deltaY*scale;flushWheel()}
async function flushPoint(){if(pointSending)return;pointSending=true;while(pointQueue.length)await send('/api/pointer',pointQueue.shift(),true);pointSending=false;if(pointQueue.length)flushPoint()}function queuePoint(e,button=e.buttons){const r=videoStage.getBoundingClientRect(),report={x:Math.round(Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*32767),y:Math.round(Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))*32767),button};if(rawInput([2,button,report.x&255,report.x>>8,report.y&255,report.y>>8]))return;const last=pointQueue[pointQueue.length-1];if(last&&last.button===button)pointQueue[pointQueue.length-1]=report;else pointQueue.push(report);flushPoint()}
const codes={Escape:'escape',Tab:'tab',Enter:'enter',Backspace:'backspace',Space:'space',ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',Delete:'delete',Insert:'insert',Home:'home',End:'end',PageUp:'page_up',PageDown:'page_down',Minus:'minus',Equal:'equals',BracketLeft:'left_bracket',BracketRight:'right_bracket',Backslash:'backslash',IntlBackslash:'non_us_backslash',Semicolon:'semicolon',Quote:'apostrophe',Backquote:'grave',Comma:'comma',Period:'period',Slash:'slash',CapsLock:'caps_lock',PrintScreen:'print_screen',ScrollLock:'scroll_lock',Pause:'pause',ContextMenu:'menu',NumLock:'num_lock',NumpadDivide:'keypad_divide',NumpadMultiply:'keypad_multiply',NumpadSubtract:'keypad_subtract',NumpadAdd:'keypad_add',NumpadEnter:'keypad_enter',NumpadDecimal:'keypad_decimal',NumpadEqual:'keypad_equals',ControlLeft:'left_ctrl',ShiftLeft:'left_shift',AltLeft:'left_alt',MetaLeft:'left_cmd',ControlRight:'right_ctrl',ShiftRight:'right_shift',AltRight:'right_alt',MetaRight:'right_cmd',AudioVolumeMute:'mute',AudioVolumeDown:'volume_down',AudioVolumeUp:'volume_up'};
function namedKey(e) { if(/^Key[A-Z]$/.test(e.code)) return e.code.slice(3).toLowerCase(); if(/^Digit[0-9]$/.test(e.code)) return e.code.slice(5); if(/^F([1-9]|1[0-9]|2[0-4])$/.test(e.code))return e.code.toLowerCase(); if(/^Numpad[0-9]$/.test(e.code))return 'keypad_'+e.code.slice(6); return codes[e.code]; }
const modifierBits={left_ctrl:1,left_shift:2,left_alt:4,left_cmd:8,right_ctrl:16,right_shift:32,right_alt:64,right_cmd:128},fixedUsages={enter:0x28,escape:0x29,backspace:0x2a,tab:0x2b,space:0x2c,minus:0x2d,equals:0x2e,left_bracket:0x2f,right_bracket:0x30,backslash:0x31,semicolon:0x33,apostrophe:0x34,grave:0x35,comma:0x36,period:0x37,slash:0x38,caps_lock:0x39,print_screen:0x46,scroll_lock:0x47,pause:0x48,insert:0x49,home:0x4a,page_up:0x4b,delete:0x4c,end:0x4d,page_down:0x4e,right:0x4f,left:0x50,down:0x51,up:0x52,num_lock:0x53,keypad_divide:0x54,keypad_multiply:0x55,keypad_subtract:0x56,keypad_add:0x57,keypad_enter:0x58,keypad_decimal:0x63,non_us_backslash:0x64,menu:0x65,keypad_equals:0x67,execute:0x74,help:0x75,select:0x77,stop:0x78,again:0x79,undo:0x7a,cut:0x7b,copy:0x7c,paste:0x7d,find:0x7e,mute:0x7f,volume_up:0x80,volume_down:0x81};
function usageFor(key){if(/^[a-z]$/.test(key))return 0x04+key.charCodeAt(0)-97;if(/^[1-9]$/.test(key))return 0x1e+Number(key)-1;if(key==='0')return 0x27;if(/^f([1-9]|1[0-9]|2[0-4])$/.test(key)){const n=Number(key.slice(1));return n<=12?0x3a+n-1:0x68+n-13}if(/^keypad_[0-9]$/.test(key)){const n=Number(key.slice(7));return n===0?0x62:0x59+n-1}return fixedUsages[key]}
function sendKeyboardReport(){let modifiers=0,usages=[];for(const key of held){if(modifierBits[key])modifiers|=modifierBits[key];else{const usage=usageFor(key);if(usage&&usages.length<6)usages.push(usage)}}while(usages.length<6)usages.push(0);return rawInput([3,modifiers,0,...usages])}
function queueKey(key,down){if(!key)return;if(sendKeyboardReport())return Promise.resolve(true);const previous=keyQueues.get(key)||Promise.resolve();const next=previous.then(()=>send(down?'/api/key-down':'/api/key-up',{key},true));keyQueues.set(key,next.catch(()=>{}));return next}
window.addEventListener('keydown',e=>{if(!relayEnabled||document.activeElement===typing)return;if(e.code==='Escape'&&(e.metaKey||held.has('left_cmd')||held.has('right_cmd'))){e.preventDefault();setRelay(false);setStatus('Relay released with ⌘ Esc');return}const key=namedKey(e);if(!key)return;e.preventDefault();if(e.repeat||held.has(key))return;held.add(key);queueKey(key,true)});window.addEventListener('keyup',e=>{if(!relayEnabled||document.activeElement===typing)return;const key=namedKey(e);if(!key)return;e.preventDefault();held.delete(key);queueKey(key,false)});
function sendClick(button){if(!rawInput([5,button]))send('/api/click',{button},true)}
pointerSurfaces.forEach(surface=>{surface.addEventListener('contextmenu',e=>e.preventDefault());surface.addEventListener('touchmove',e=>e.preventDefault(),{passive:false});surface.addEventListener('pointerdown',e=>{if(!relayEnabled){setStatus('Enable relay to control the target','error');return}if(e.pointerType==='mouse'){e.preventDefault();surface.focus();const button=e.button===2?2:e.button===1?4:1;if(surface===videoStage){surface.setPointerCapture(e.pointerId);queuePoint(e,e.buttons||button);return}sendClick(button);if(document.pointerLockElement!==surface){try{const capture=surface.requestPointerLock();if(capture&&capture.catch)capture.catch(()=>setStatus('Click sent · mouse capture blocked','error'))}catch(error){setStatus('Click sent · mouse capture blocked','error')}}return}e.preventDefault();document.body.classList.add('touching');pointer={x:e.clientX,y:e.clientY};moved=false;surface.setPointerCapture(e.pointerId)});surface.addEventListener('pointermove',e=>{if(!relayEnabled)return;if(e.pointerType==='mouse'&&surface===videoStage){e.preventDefault();queuePoint(e);return}if(!pointer)return;e.preventDefault();const dx=e.clientX-pointer.x,dy=e.clientY-pointer.y;if(Math.abs(dx)+Math.abs(dy)>5)moved=true;queueMove(dx,dy);pointer={x:e.clientX,y:e.clientY}});surface.addEventListener('pointerup',e=>{if(e.pointerType==='mouse'&&surface===videoStage){queuePoint(e,0);return}finishTouch(true)});surface.addEventListener('pointercancel',e=>{if(e.pointerType==='mouse'&&surface===videoStage){releaseEverything();return}finishTouch(false)})});document.addEventListener('pointerlockchange',()=>pointerSurfaces.forEach(surface=>surface.classList.toggle('active',document.pointerLockElement===surface)));document.addEventListener('mousemove',e=>{if(relayEnabled&&document.pointerLockElement)queueMove(e.movementX,e.movementY)});function finishTouch(click){if(click&&relayEnabled&&pointer&&!moved)sendClick(1);pointer=null;document.body.classList.remove('touching')}
window.addEventListener('wheel',e=>{if(relayEnabled&&document.activeElement!==typing)queueWheel(e)},{passive:false});
async function submitText(){const text=typing.value;if(!text){setStatus('Enter text first','error');return}if(await send('/api/text',{text}))typing.value='';typing.focus()} sendText.onclick=submitText;typing.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();submitText()}});
async function releaseEverything(force=false){held.clear();pointQueue=[];document.querySelectorAll('.kbd.held').forEach(b=>{b.classList.remove('held');b.setAttribute('aria-pressed','false')});while(pointSending)await delay(2);if(!(relayEnabled||force))return;if(!rawInput([4]))fetch(apiPath('/api/release-all'),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',keepalive:true}).catch(()=>{})} releaseAll.onclick=()=>{releaseEverything();setStatus('All keys released')};
document.querySelectorAll('[data-hid]').forEach(button=>{button.setAttribute('aria-pressed','false');const key=button.dataset.hid;const release=()=>{if(!button.classList.contains('held'))return;button.classList.remove('held');button.setAttribute('aria-pressed','false');held.delete(key);queueKey(key,false)};button.addEventListener('pointerdown',e=>{if(!relayEnabled||button.classList.contains('held'))return;e.preventDefault();if(navigator.vibrate)navigator.vibrate(8);button.setPointerCapture(e.pointerId);button.classList.add('held');button.setAttribute('aria-pressed','true');held.add(key);queueKey(key,true)});button.addEventListener('pointerup',release);button.addEventListener('pointercancel',release);button.addEventListener('contextmenu',e=>e.preventDefault())}); document.querySelectorAll('[data-click]').forEach(b=>b.onclick=()=>sendClick(+b.dataset.click));document.querySelectorAll('[data-wheel]').forEach(b=>b.onclick=()=>{const wheel=+b.dataset.wheel;if(!rawInput([1,0,0,wheel&255]))send('/api/mouse',{dx:0,dy:0,wheel},true)});
window.addEventListener('blur',releaseEverything);document.addEventListener('visibilitychange',()=>{if(document.hidden)releaseEverything()});
checkConnection();connectInput();
</script></body></html>"""
    return document.replace("__KEYBOARD__", keyboard_html()).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *_: object) -> None:
        pass

    def reply(self, status: HTTPStatus, content: dict[str, object]) -> None:
        body = json.dumps(content).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        request_url = urlsplit(self.path)
        path = request_url.path
        if path.endswith("/health"):
            self.reply(HTTPStatus.OK, {
                "ok": True,
                "video": video_feed.status() if video_feed else {"ok": False, "error": "Video feed is not running"},
            })
            return
        if path in ("/video", "/kvm/video", "/dmxt/video"):
            if not video_feed:
                self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "Video feed is not running"})
                return
            video_feed.stream(self)
            self.close_connection = True
            return
        if path in ("/snapshot", "/kvm/snapshot", "/dmxt/snapshot"):
            if not video_feed:
                self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "Video feed is not running"})
                return
            try:
                after = int(parse_qs(request_url.query).get("after", ["-1"])[0])
            except ValueError:
                self.reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid frame sequence"})
                return
            sequence, frame = video_feed.snapshot(after)
            if frame is None:
                self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "No video frame available"})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("X-DMXT-Sequence", str(sequence))
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return
        if path not in ("/", "/kvm", "/kvm/", "/dmxt", "/dmxt/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = page()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            if self.path.endswith("/audio/webrtc"):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("invalid SDP size")
                offer = self.rfile.read(length)
                request = Request(
                    "http://127.0.0.1:1984/api/webrtc?src=dmxt_audio",
                    data=offer,
                    headers={"Content-Type": "application/sdp"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    answer = response.read()
                self.send_response(HTTPStatus.CREATED)
                self.send_header("Content-Type", "application/sdp")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)
                return
            response: dict[str, object] = {"ok": True}
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length))
            if self.path.endswith("/key-down"):
                key = request["key"]
                if key not in KEYS and key not in MODIFIERS:
                    raise ValueError("unsupported key")
                hid.down(key)
            elif self.path.endswith("/key-up"):
                key = request["key"]
                if key not in KEYS and key not in MODIFIERS:
                    raise ValueError("unsupported key")
                hid.up(key)
            elif self.path.endswith("/release-all"):
                hid.release_all()
            elif self.path.endswith("/key"):
                key, modifiers = request["key"], request.get("modifiers", [])
                if key not in KEYS or not isinstance(modifiers, list) or any(item not in MODIFIERS for item in modifiers):
                    raise ValueError("unsupported key")
                hid.key(key, modifiers)
            elif self.path.endswith("/text"):
                value = request["text"]
                if not isinstance(value, str) or len(value) > 512:
                    raise ValueError("invalid text")
                sent, unsupported = hid.text(value)
                if value and not sent:
                    raise ValueError("no supported US-keyboard characters")
                response.update({"sent": sent, "unsupported": unsupported})
            elif self.path.endswith("/pointer"):
                x, y, button = int(request["x"]), int(request["y"]), int(request.get("button", 0))
                if not 0 <= x <= 32767 or not 0 <= y <= 32767 or button not in (0, 1, 2, 4):
                    raise ValueError("invalid absolute pointer report")
                hid.point(x, y, button)
            elif self.path.endswith("/mouse"):
                dx, dy, wheel = int(request["dx"]), int(request["dy"]), int(request.get("wheel", 0))
                if not all(-127 <= value <= 127 for value in (dx, dy, wheel)):
                    raise ValueError("invalid mouse movement")
                hid.move(dx, dy, wheel)
            elif self.path.endswith("/click"):
                button = int(request.get("button", 1))
                if button not in (1, 2, 4):
                    raise ValueError("invalid mouse button")
                hid.click(button)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.reply(HTTPStatus.OK, response)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
        except OSError as error:
            self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})


class Server(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    fast_url = os.environ.get("DMXT_FAST_URL")
    video_feed = FastVideoFeed(fast_url) if fast_url else VideoFeed()
    Server(("127.0.0.1", 8023), Handler).serve_forever()
