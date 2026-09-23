#!/usr/bin/env python3
"""E2E for the Jaarvis voice Space (their dashboard UI + our bridge).

Boots the real server on a scratch port and drives it like a phone:
login -> feed -> command turn (tool) -> image upload (vision) ->
phone-audio socket -> soul switch. Fails loudly on the first broken seam.
Needs GEMINI_API_KEY in env (uses the text-cheap Live path, ~1 turn each).
"""
import io
import http.client as http_client_mod
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = int(os.environ.get("E2E_PORT", "7899"))
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name, detail[:100])
    if not cond:
        fails.append(name)


def call(method, path, body=None, headers=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    env = dict(os.environ, JAARVIS_PIN="E2E123", PORT=str(PORT))
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=os.path.join(ROOT, "space"), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                if call("GET", "/")[0] == 200:
                    break
            except Exception:
                time.sleep(1)
        body = call("GET", "/")[1].decode()
        check("hud serves their UI", "JAARVIS" in body and "mic-btn" in body)
        check("login page", call("GET", "/login")[0] == 200)
        check("crypto.js vendored", call("GET", "/static/crypto.js")[0] == 200)
        code, raw = call("POST", "/login", {"pin": "WRONG1"})
        check("bad PIN rejected", code == 401)
        code, raw = call("POST", "/login", {"pin": "E2E123"})
        tok = json.loads(raw).get("token", "")
        check("PIN login issues token", code == 200 and len(tok) > 20)
        code, _ = call("POST", "/api/command", {"text": "hi"}, token="bad")
        check("command needs token", code == 401)

        import websocket
        ws = websocket.create_connection(f"{WS_BASE}/ws?token={tok}", timeout=30)
        got_status = False
        ws.settimeout(15)
        try:
            for _ in range(5):
                m = json.loads(ws.recv())
                if m.get("type") == "status":
                    got_status = True
                    break
        except Exception:
            pass
        check("feed status event", got_status)

        # Command turn with a real tool (save_memory) via shared brain.
        code, _ = call("POST", "/api/command",
                       {"text": "Remember that my test reactor is E2E-One. One short sentence."},
                       token=tok)
        check("command accepted", code == 200)
        heard = ""
        ws.settimeout(400)
        try:
            t0 = time.time()
            while time.time() - t0 < 390:
                m = json.loads(ws.recv())
                if m.get("type") == "log" and m.get("speaker") == "jarvis":
                    heard += m.get("text", "")
                    if len(heard) > 10:
                        break
        except Exception as e:
            heard += f" [exc {type(e).__name__}]"
        check("brain answered on feed", len(heard) > 10, heard[:80])

        # Vision: upload an image, he describes it.
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (200, 120), (10, 20, 40))
        d = ImageDraw.Draw(im)
        d.rectangle([60, 20, 140, 100], outline=(80, 255, 170), width=5)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        boundary = "E2EBOUND"
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; "
                   f'name="file"; filename="e2e.png"\r\nContent-Type: image/png\r\n\r\n').encode() \
            + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        conn = http_client_mod.HTTPConnection("127.0.0.1", PORT, timeout=30)
        conn.request("POST", "/api/upload", payload,
                     {"Content-Type": f"multipart/form-data; boundary={boundary}",
                      "Authorization": f"Bearer {tok}"})
        resp = conn.getresponse()
        check("image upload accepted", resp.status == 200)
        # Drain any tail of the previous turn before waiting on vision.
        ws.settimeout(3)
        try:
            while True:
                ws.recv()
        except Exception:
            pass
        ws.settimeout(300)
        seen = ""
        try:
            t0 = time.time()
            while time.time() - t0 < 390:
                m = json.loads(ws.recv())
                if m.get("type") == "log" and m.get("speaker") == "jarvis":
                    seen += m.get("text", "")
                    if len(seen) > 30:
                        break
        except Exception:
            pass
        check("vision answered (green rectangle, dark)",
              "green" in seen.lower() or "rectangle" in seen.lower(), seen[:100])

        # Phone-audio socket: protocol-level (PCM in, stays open, session alive).
        wv = websocket.create_connection(
            f"{WS_BASE}/ws/phone-audio?token={tok}", timeout=20)
        wv.send_binary(b"\x00\x00" * 1024)
        time.sleep(2)
        check("phone-audio socket alive", wv.connected)
        wv.close()
        ws.close()

        # Soul switch rebuilds the shared brain.
        code, raw = call("POST", "/api/soul", {"soul": "ultron"}, token=tok)
        check("soul switch", code == 200 and json.loads(raw).get("name") == "Ultron")
    finally:
        proc.terminate()
    print("E2E", "GREEN" if not fails else f"RED {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
