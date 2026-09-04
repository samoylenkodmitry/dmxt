#!/usr/bin/env python3
"""Local web control panel for the DMXT HID gadget."""
from __future__ import annotations

import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DMXT_HOST", os.environ.get("ORANGEPI_HOST", "orangepizero3"))
PORT = int(os.environ.get("ORANGEPI_KVM_PORT", "8023"))
KEY_ROWS = [
    [("Esc", "escape"), ("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6"), ("7", "7"), ("8", "8"), ("9", "9"), ("0", "0"), ("Backspace", "backspace")],
    [("Tab", "tab"), *[(letter.upper(), letter) for letter in "qwertyuiop"], ("[", "left_bracket"), ("]", "right_bracket"), ("\\", "backslash")],
    [("Caps", "caps_lock"), *[(letter.upper(), letter) for letter in "asdfghjkl"], (";", "semicolon"), ("'", "apostrophe"), ("Enter", "enter")],
    [("ISO key", "non_us_backslash"), *[(letter.upper(), letter) for letter in "zxcvbnm"], (",", "comma"), (".", "period"), ("/", "slash")],
    [("Space", "space")],
]
ALLOWED_KEYS = {value for row in KEY_ROWS for _, value in row} | {"left", "right", "up", "down"}


def command(*arguments: str) -> tuple[bool, str]:
    completed = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=4", HOST, "opihid", *arguments],
        text=True,
        capture_output=True,
        timeout=8,
    )
    return completed.returncode == 0, (completed.stderr or completed.stdout).strip()


def page() -> bytes:
    rows = "".join(
        "<div class='row'>" + "".join(
            f"<button data-key='{value}'>{label}</button>" for label, value in row
        ) + "</div>"
        for row in KEY_ROWS
    )
    return f"""<!doctype html>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>DMXT</title>
<style>
body {{ max-width: 900px; margin: 36px auto; padding: 0 18px; background:#171717; color:#f5f5f5; font:16px -apple-system,sans-serif }}
h1 {{ margin-bottom: 4px }} p {{ color:#aaa }} .row {{ display:flex; gap:7px; margin:7px 0; flex-wrap:wrap }}
button {{ min-width:47px; padding:14px 12px; border:1px solid #555; border-radius:8px; background:#303030; color:white; font:inherit; cursor:pointer }}
button:hover {{ background:#4a4a4a }} .wide {{ min-width:125px }} #status {{ min-height:24px; color:#9fda9f; margin-top:20px }}
</style>
<h1>DMXT</h1><p>Connected to {HOST}. Input is sent over SSH through the DMXT USB keyboard/mouse.</p>
<h2>Keyboard</h2>{rows}
<h2>Mouse</h2><div class='row'>
<button data-mouse='-35,0'>←</button><button data-mouse='0,-35'>↑</button><button data-mouse='0,35'>↓</button><button data-mouse='35,0'>→</button><button class='wide' data-click='1'>Left click</button>
</div><div id='status'>Ready</div>
<script>
async function send(path, body) {{
  const status=document.querySelector('#status'); status.textContent='Sending…';
  try {{ const r=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}}); const j=await r.json(); status.textContent=j.ok?'Sent':j.error; }}
  catch(e) {{ status.textContent=e.message; }}
}}
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>send('/api/key',{{key:b.dataset.key}}));
document.querySelectorAll('[data-mouse]').forEach(b=>{{const [dx,dy]=b.dataset.mouse.split(',').map(Number);b.onclick=()=>send('/api/mouse',{{dx,dy}})}});
document.querySelector('[data-click]').onclick=()=>send('/api/click',{{}});
</script>""".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def respond(self, status: int, content: dict[str, object]) -> None:
        body = json.dumps(content).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = page()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(size))
            if self.path == "/api/key":
                key = request["key"]
                if key not in ALLOWED_KEYS:
                    raise ValueError("unsupported key")
                ok, detail = command("key", key)
            elif self.path == "/api/mouse":
                dx, dy = int(request["dx"]), int(request["dy"])
                if not -127 <= dx <= 127 or not -127 <= dy <= 127:
                    raise ValueError("invalid mouse movement")
                ok, detail = command("mouse", str(dx), str(dy))
            elif self.path == "/api/click":
                ok, detail = command("click")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.respond(HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY, {"ok": ok, "error": detail})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})


if __name__ == "__main__":
    print(f"DMXT control panel: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
