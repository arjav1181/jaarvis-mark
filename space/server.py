"""Jaarvis voice Space — the SAME brain as the desktop, different body.

Local mode and server mode import from core/ identically: same registries,
same prompt assembly, same memory engine, same persona trio. The ONLY
differences are architectural:
  voice in   : browser mic PCM (here) vs local mic (desktop)
  voice out  : streamed PCM to browser (here) vs speakers (desktop)
  face       : web orb (here) vs holographic head (desktop)
  tools      : server-safe subset (opt-in per tool) vs full local set
  memory file: server-side store (here) vs local store (desktop)

The Gemini key lives in Space Secrets (GEMINI_API_KEY), never in code.
Run: JAARVIS_MODE=server python launch.py  (or uvicorn server:app)
"""
import asyncio
import json
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core import brain
from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins
from core.personas import valid_persona
from core import briefing as briefing_core
from memory import memory_manager
from memory.memory_manager import (
    load_memory, update_memory, search_memory, format_memory_for_prompt,
)
from core import undo as undo_stack

MODEL = "models/gemini-3.1-flash-live-preview"
ASST_NAME = "JAARVIS"

app = FastAPI(docs_url=None, redoc_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_action_registry = discover_actions(
    Path(os.path.join(ROOT, "actions")),
    logger=lambda m: print("[actions]", m))
_plugin_registry = discover_plugins(
    Path(os.path.join(ROOT, "plugins")),
    set(_action_registry.names()),
    logger=lambda m: print("[plugins]", m))

# Inline core tools, mirrored from the desktop executor (same names, same
# shapes, same memory functions — only the UI/vision/camera halves stay local).
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
    """Same tools, same functions as local mode (no UI/vision halves)."""
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


def _build_instruction(persona_key: str) -> tuple[str, list]:
    decls = _server_decls()
    mem = load_memory()
    mem_str = format_memory_for_prompt(mem)
    instruction = brain.assemble_system(
        asst_name=ASST_NAME, user_name="", persona_key=persona_key,
        memory_str=mem_str, declarations=decls,
        platform=f"{platform.system()} {platform.release()} (server body)".strip(),
        capabilities=brain.describe_tools(decls),
        limits=brain.describe_limits(body="server", has_vision=False, has_mic=True),
        body="server")
    return instruction, decls


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(HERE, "static", "hud.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")

BRIEF_MIN = int(os.environ.get("JAARVIS_BRIEF_MIN", "360") or 360)


async def _briefing_loop():
    """Server proactive engine: word a briefing on schedule, hold as pending,
    deliver on next connect ('while you were away...')."""
    import asyncio as _aio
    while True:
        try:
            await _aio.sleep(BRIEF_MIN * 60)
            briefing_core.scheduled_check(
                load_memory(), os.environ.get("GEMINI_API_KEY", ""))
        except Exception:
            pass


@app.on_event("startup")
async def _start_briefings():
    asyncio.create_task(_briefing_loop())


def _client():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY first.")
    return genai.Client(api_key=key)


@app.websocket("/ws/voice")
async def voice(ws: WebSocket):
    await ws.accept()
    try:
        hello = json.loads(await ws.receive_text())
    except Exception:
        await ws.close(code=4400)
        return
    persona = valid_persona(hello.get("soul", "jarvis"))
    try:
        client = _client()
    except RuntimeError:
        await ws.send_text(json.dumps({"type": "error",
                                       "message": "server has no key"}))
        await ws.close(code=4401)
        return
    instruction, decls = _build_instruction(persona)
    pending = briefing_core.take_pending()
    if pending:
        # "While you were away..." — the first thing said on connect.
        lead = ("While you were away: " +
                " ".join(p.get("text", "") for p in pending)[-1500:])
        instruction += ("\n[PENDING BRIEFING — speak this first, briefly: "
                        + lead + "]")

    async def pump_in(session, queue):
        while True:
            msg = await ws.receive()
            if msg.get("text") is not None:
                try:
                    d = json.loads(msg["text"])
                except Exception:
                    continue
                if d.get("type") == "text" and d.get("text"):
                    await session.send_client_content(
                        turns={"role": "user",
                               "parts": [{"text": d["text"]}]},
                        turn_complete=True)
                elif d.get("type") == "image" and d.get("data"):
                    # Server vision: a frame from the user's world (screenshot,
                    # photo, camera). Same contract as screen_process — one
                    # labelled frame, answered in the same turn.
                    import base64
                    try:
                        raw = base64.b64decode(d["data"][:8_000_000])
                    except Exception:
                        raw = b""
                    if raw:
                        mime = str(d.get("mime", "image/jpeg"))[:64]
                        await session.send_client_content(
                            turns={"role": "user", "parts": [
                                {"text": ("[USER-PROVIDED IMAGE — a photo from "
                                          "the user's world, not your face.] "
                                          + (d.get("text") or
                                             "Describe what you see, briefly."))},
                                {"inline_data": {"mime_type": mime,
                                                 "data": raw}}]},
                            turn_complete=True)
                elif d.get("type") == "end":
                    return
            elif msg.get("bytes") is not None:
                await queue.put(msg["bytes"])

    async def pump_audio(session, queue):
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            await session.send_realtime_input(
                audio={"mime_type": "audio/pcm;rate=16000", "data": chunk})

    try:
        async with client.aio.live.connect(
                model=MODEL,
                config={"response_modalities": ["AUDIO"],
                        "output_audio_transcription": {},
                        "input_audio_transcription": {},
                        "system_instruction": instruction,
                        "tools": [{"function_declarations": decls}]}) as session:
            queue: asyncio.Queue = asyncio.Queue()
            tin = asyncio.create_task(pump_in(session, queue))
            taud = asyncio.create_task(pump_audio(session, queue))
            try:
                async for m in session.receive():
                    if getattr(m, "tool_call", None):
                        responses = []
                        for fc in m.tool_call.function_calls:
                            from google.genai import types
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
                                await ws.send_bytes(inline.data)
                    if sc.output_transcription and sc.output_transcription.text:
                        await ws.send_text(json.dumps(
                            {"type": "said",
                             "text": sc.output_transcription.text}))
                    if sc.input_transcription and sc.input_transcription.text:
                        await ws.send_text(json.dumps(
                            {"type": "heard",
                             "text": sc.input_transcription.text}))
                    if sc.turn_complete:
                        await ws.send_text(json.dumps({"type": "turn_end"}))
            finally:
                tin.cancel()
                taud.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_text(json.dumps({"type": "error",
                                           "message": str(exc)[:120]}))
        except Exception:
            pass
    try:
        await ws.close()
    except Exception:
        pass
