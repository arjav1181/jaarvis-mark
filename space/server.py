"""Jaarvis voice Space — the SAME brain as the desktop, different body.

Serves the upstream Mark dashboard UI (space/static/app.html + login.html:
their files, JS extended only for voice-return playback and soul switch) and
implements its protocol: PIN login, feed WS, command/wake/upload endpoints,
phone-audio WS. Same registries, prompt assembly, memory engine, persona trio
as local mode — only the I/O differs (browser mic vs local mic, no face).

Env: GEMINI_API_KEY (required), JAARVIS_PIN (optional gate; empty = open demo),
JAARVIS_BRIEF_MIN (default 360), PORT (default 7860).
"""
import asyncio
import base64
import json
import os
import platform
import secrets
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import brain
from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins
from core.personas import PERSONAS, valid_persona
from core import briefing as briefing_core
from memory.memory_manager import (
    load_memory, update_memory, search_memory, format_memory_for_prompt,
)
from core import undo as undo_stack

MODEL = "models/gemini-3.1-flash-live-preview"
ASST_NAME = "JAARVIS"
PIN = os.environ.get("JAARVIS_PIN", "").strip()
BRIEF_MIN = int(os.environ.get("JAARVIS_BRIEF_MIN", "360") or 360)

app = FastAPI(docs_url=None, redoc_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")
UPLOADS = os.path.join(ROOT, "memory", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

_action_registry = discover_actions(
    Path(os.path.join(ROOT, "actions")),
    logger=lambda m: print("[actions]", m))
_plugin_registry = discover_plugins(
    Path(os.path.join(ROOT, "plugins")),
    set(_action_registry.names()),
    logger=lambda m: print("[plugins]", m))

SERVER_INLINE_TOOLS = [
    {"name": "save_memory",
     "description": "Save a fact about the user (category, key, value).",
     "parameters": {"type": "OBJECT", "properties": {
         "category": {"type": "STRING", "description": "notes, preferences, projects, people"},
         "key": {"type": "STRING", "description": "short key"},
         "value": {"type": "STRING", "description": "the fact"}}, "required": []}},
    {"name": "recall_memory",
     "description": "Look up saved facts by keyword.",
     "parameters": {"type": "OBJECT", "properties": {
         "query": {"type": "STRING", "description": "what to look up"}}, "required": []}},
    {"name": "undo",
     "description": "List or reverse the assistant's own recent changes.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "'list' or empty"}}, "required": []}},
    {"name": "system_status",
     "description": "Server body stats: CPU, RAM, disk, uptime.",
     "parameters": {"type": "OBJECT", "properties": {}}},
]

_tokens: set[str] = set()
_feed_clients: set[WebSocket] = set()
_voice_clients: set[WebSocket] = set()
_soul = "jarvis"


def _server_decls():
    decls = list(SERVER_INLINE_TOOLS)
    decls += _action_registry.server_declarations()
    decls += _plugin_registry.server_declarations()
    return decls


def _system_status() -> str:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        import shutil as _sh
        disk = _sh.disk_usage("/")
        return (f"Server body: CPU {cpu}%, RAM {mem.percent}% used, "
                f"disk {disk.free // (1024**3)}G free, sir.")
    except Exception as e:
        return f"Status unavailable, sir: {e}"


def _execute_tool(name: str, args: dict) -> str:
    args = args or {}
    try:
        if name == "save_memory":
            if args.get("key") and args.get("value"):
                update_memory({args.get("category", "notes"):
                               {args.get("key"): {"value": args.get("value")}}})
            return "ok"
        if name == "recall_memory":
            return search_memory(args.get("query", ""), limit=8)
        if name == "undo":
            if str(args.get("action", "")).lower().strip() == "list":
                items = undo_stack.history()
                return ("Things I can undo:\n" + "\n".join(items)) if items else \
                    "Nothing to undo yet."
            return undo_stack.undo_last()
        if name == "system_status":
            return _system_status()
        if _action_registry.has(name):
            return _action_registry.run(name, args, {}) or "Done."
        if _plugin_registry.has(name):
            return _plugin_registry.run(name, args) or "Done."
        return f"Tool '{name}' is not available in server mode."
    except Exception as e:
        return f"Tool {name} failed: {e}"


def _instruction(persona_key: str) -> tuple[str, list]:
    decls = _server_decls()
    mem_str = format_memory_for_prompt(load_memory())
    pending = briefing_core.take_pending()
    extra = ""
    if pending:
        lead = " ".join(p.get("text", "") for p in pending)[-1500:]
        extra = ("\n[PENDING BRIEFING — speak this first, briefly: "
                 + lead + "]")
    return (brain.assemble_system(
        asst_name=ASST_NAME, user_name="", persona_key=persona_key,
        memory_str=mem_str, declarations=decls,
        platform=f"{platform.system()} {platform.release()} (server body)".strip(),
        capabilities=brain.describe_tools(decls),
        limits=brain.describe_limits(body="server", has_vision=True, has_mic=True),
        body="server") + extra, decls)


async def _broadcast(obj: dict):
    dead = []
    for ws in list(_feed_clients):
        try:
            await ws.send_json(obj)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _feed_clients.discard(ws)


async def _broadcast_audio(data: bytes):
    dead = []
    for ws in list(_voice_clients):
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _voice_clients.discard(ws)


class SharedBrain:
    """One persistent Live session (the body); phones attach as faces."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._session = None
        self._recv_task = None
        self._ready = asyncio.Event()
        self._turn_over = asyncio.Event()
        self._turn_over.set()  # idle at boot: first turn goes immediately

    async def ensure(self):
        async with self._lock:
            if self._session is not None:
                return self._session
            from google import genai
            key = os.environ.get("GEMINI_API_KEY", "")
            if not key:
                raise RuntimeError("server has no key")
            client = genai.Client(api_key=key)
            instruction, decls = _instruction(_soul)
            cm = client.aio.live.connect(
                model=MODEL,
                config={"response_modalities": ["AUDIO"],
                        "output_audio_transcription": {},
                        "input_audio_transcription": {},
                        "system_instruction": instruction,
                        "tools": [{"function_declarations": decls}]})
            session = await cm.__aenter__()
            self._cm = cm
            self._session = session
            self._ready.set()
            self._recv_task = asyncio.create_task(self._receive(session))
            return session

    async def rebuild(self, soul: str):
        async with self._lock:
            if self._recv_task:
                self._recv_task.cancel()
            cm = getattr(self, "_cm", None)
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
                self._cm = None
            self._session = None
            self._ready.clear()
        await self.ensure()

    async def _receive(self, session):
        try:
            async for m in session.receive():
                if getattr(m, "tool_call", None):
                    from google.genai import types
                    responses = []
                    for fc in m.tool_call.function_calls:
                        out = _execute_tool(fc.name, dict(fc.args or {}))
                        responses.append(types.FunctionResponse(
                            id=fc.id, name=fc.name,
                            response={"result": out}))
                    await session.send_tool_response(
                        function_responses=responses)
                    continue
                sc = m.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for p in sc.model_turn.parts:
                        inline = getattr(p, "inline_data", None)
                        if inline and inline.data:
                            await _broadcast_audio(inline.data)
                if sc.output_transcription and sc.output_transcription.text:
                    await _broadcast({"type": "log", "speaker": "jarvis",
                                      "text": sc.output_transcription.text})
                if sc.input_transcription and sc.input_transcription.text:
                    await _broadcast({"type": "log", "speaker": "user",
                                      "text": sc.input_transcription.text})
                if sc.turn_complete:
                    self._turn_over.set()
                    await _broadcast({"type": "status", "state": "active"})
        except Exception as e:
            print("[brain] receive ended:", str(e)[:120])
        finally:
            async with self._lock:
                if self._session is session:
                    self._session = None
                    self._ready.clear()

    async def _send_turn(self, turns):
        # One turn at a time: a new turn fired mid-turn stalls the session,
        # so wait for turn_complete (bounded) before submitting.
        try:
            await asyncio.wait_for(self._turn_over.wait(), timeout=150)
        except Exception:
            pass
        self._turn_over.clear()
        # One retry on a fresh session: the shared session can die quietly
        # (timeout, network blip) and ensure() can't see that.
        try:
            session = await self.ensure()
            await session.send_client_content(turns=turns, turn_complete=True)
        except Exception:
            async with self._lock:
                self._session = None
                self._ready.clear()
            session = await self.ensure()
            self._turn_over.clear()
            await session.send_client_content(turns=turns, turn_complete=True)

    async def say(self, text: str):
        await self._send_turn({"role": "user", "parts": [{"text": text}]})

    async def see(self, raw: bytes, mime: str, note: str):
        await self._send_turn({"role": "user", "parts": [
            {"text": ("[USER-PROVIDED IMAGE — a photo from the user's "
                      "world, not your face.] " + (note or
                       "Describe what you see, briefly."))},
            {"inline_data": {"mime_type": mime, "data": raw}}]})

    async def hear(self, pcm: bytes):
        session = await self.ensure()
        await session.send_realtime_input(
            audio={"mime_type": "audio/pcm;rate=16000", "data": pcm})


brain_box = SharedBrain()


def _authed(req: Request) -> bool:
    tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(tok) and tok in _tokens


@app.get("/", include_in_schema=False)
async def index(req: Request):
    html = open(os.path.join(STATIC, "app.html"), encoding="utf-8").read()
    host = (req.headers.get("host") or "localhost").split(":")[0]
    return HTMLResponse(html.replace("__IP__", host).replace("__PORT__", ""))


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(os.path.join(STATIC, "login.html"))


@app.get("/static/crypto.js", include_in_schema=False)
async def crypto_js():
    return FileResponse(os.path.join(STATIC, "crypto-js.min.js"),
                        media_type="application/javascript")


@app.post("/login")
async def login(req: Request):
    body = await req.json()
    pin = str(body.get("pin", "")).strip().upper()
    if len(pin) < 6:
        return JSONResponse({"ok": False}, status_code=401)
    if PIN and pin != PIN.upper():
        return JSONResponse({"ok": False}, status_code=401)
    tok = secrets.token_urlsafe(32)
    _tokens.add(tok)
    await _broadcast({"type": "sys", "text": "Remote connection established."})
    return JSONResponse({"ok": True, "token": tok})


@app.post("/api/device-login")
async def device_login(req: Request):
    return JSONResponse({"ok": False, "error": "use PIN entry"})


@app.post("/api/command")
async def command(req: Request):
    if not _authed(req):
        return JSONResponse({"ok": False}, status_code=401)
    body = await req.json()
    if body.get("enc"):
        return JSONResponse({"ok": False,
                             "error": "AES pairing is desktop-only; send plaintext over TLS"},
                            status_code=400)
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": False}, status_code=400)
    await _broadcast({"type": "log", "speaker": "user", "text": text})
    try:
        await brain_box.say(text)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]})
    return JSONResponse({"ok": True})


@app.post("/api/wake")
async def wake(req: Request):
    if not _authed(req):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        await brain_box.ensure()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]})
    await _broadcast({"type": "status", "state": "active"})
    return JSONResponse({"ok": True})


@app.post("/api/soul")
async def soul(req: Request):
    if not _authed(req):
        return JSONResponse({"ok": False}, status_code=401)
    body = await req.json()
    key = valid_persona(body.get("soul", ""))
    global _soul
    _soul = key
    try:
        await brain_box.rebuild(key)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]})
    name = PERSONAS[key]["name"]
    await _broadcast({"type": "sys", "text": f"{name} taking over."})
    return JSONResponse({"ok": True, "name": name})


@app.post("/api/upload")
async def upload(req: Request, file: UploadFile = File(...)):
    if not _authed(req):
        return JSONResponse({"ok": False}, status_code=401)
    raw = await file.read()
    if len(raw) > 8_000_000:
        return JSONResponse({"ok": False, "error": "file too large"})
    name = (file.filename or "upload")[:120].replace("/", "_")
    with open(os.path.join(UPLOADS, f"{int(time.time())}_{name}"), "wb") as f:
        f.write(raw)
    for ws in list(_feed_clients):
        try:
            await ws.send_json({"type": "file_received", "name": name,
                                "size": len(raw)})
        except Exception:
            pass
    ctype = (file.content_type or "")
    if ctype.startswith("image/"):
        try:
            await brain_box.see(raw, ctype, "The user shared this photo. Describe it briefly.")
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:120]})
    else:
        await _broadcast({"type": "sys",
                          "text": f"Received {name} ({len(raw)} bytes)."})
    return JSONResponse({"ok": True, "name": name, "size": len(raw)})


@app.websocket("/ws")
async def feed(ws: WebSocket):
    token = ws.query_params.get("token", "")
    if not token or token not in _tokens:
        await ws.close(code=4401)
        return
    await ws.accept()
    _feed_clients.add(ws)
    try:
        await ws.send_json({"type": "status", "state": "active"})
        await ws.send_json({"type": "sys",
                            "text": f"{PERSONAS[_soul]['name']} online — {'' if PIN else 'open demo mode. '}"})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _feed_clients.discard(ws)


@app.websocket("/ws/phone-audio")
async def phone_audio(ws: WebSocket):
    token = ws.query_params.get("token", "")
    if not token or token not in _tokens:
        await ws.close(code=4401)
        return
    await ws.accept()
    _voice_clients.add(ws)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("bytes"):
                try:
                    await brain_box.hear(msg["bytes"])
                except Exception as e:
                    await ws.send_json({"type": "error",
                                        "message": str(e)[:120]})
            elif msg.get("text") is not None:
                try:
                    d = json.loads(msg["text"])
                    if isinstance(d, dict) and d.get("type") == "end":
                        break
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        _voice_clients.discard(ws)


async def _briefing_loop():
    while True:
        try:
            await asyncio.sleep(BRIEF_MIN * 60)
            briefing_core.scheduled_check(
                load_memory(), os.environ.get("GEMINI_API_KEY", ""))
        except Exception:
            pass


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_briefing_loop())
    try:
        await brain_box.ensure()
        print("[brain] shared session online")
    except Exception as e:
        print("[brain] will connect on first use:", str(e)[:120])
