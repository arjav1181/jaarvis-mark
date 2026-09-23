#!/usr/bin/env python3
"""E2E vision half: upload an image, he describes it. Own process lifetime
(the box reaps long runs, so vision rides separately from the core suite)."""
import http.client as http_client_mod
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket
from PIL import Image, ImageDraw

PORT = int(os.environ.get("E2E_PORT", "7899"))
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name, detail[:100])
    if not cond:
        fails.append(name)


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
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/", timeout=5).read()
                break
            except Exception:
                time.sleep(2)
        tok = json.load(urllib.request.urlopen(urllib.request.Request(
            BASE + "/login", data=json.dumps({"pin": "E2E123"}).encode(),
            headers={"Content-Type": "application/json"}),
            timeout=20))["token"]
        ws = websocket.create_connection(f"{WS_BASE}/ws?token={tok}",
                                         timeout=30)
        ws.settimeout(15)
        try:
            for _ in range(5):
                m = json.loads(ws.recv())
                if m.get("type") == "status":
                    break
        except Exception:
            pass
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
        check("vision setup: upload accepted", resp.status == 200)
        ws.settimeout(400)
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
              "green" in seen.lower() or "rectangle" in seen.lower(),
              seen[:100])
        ws.close()
    finally:
        proc.terminate()
    print("E2E-VISION", "GREEN" if not fails else f"RED {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
