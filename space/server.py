"""Jaarvis voice Space — brain on the server, voice in the browser.

Browser captures mic PCM (16k mono) -> WS -> this server -> Gemini Live
-> spoken audio + transcript stream back. The Gemini key lives in Space
Secrets (GEMINI_API_KEY) and never reaches the browser.

Run: uvicorn server:app --host 0.0.0.0 --port 7860
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.personas import PERSONAS

MODEL = "models/gemini-3.1-flash-live-preview"

# Souls come from core/personas.py — the SAME file the desktop app uses.
# Server mode and local mode differ only in I/O, never in character.
SOULS = {k: v["brief"] for k, v in PERSONAS.items()}

app = FastAPI(docs_url=None, redoc_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(HERE, "static", "hud.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")


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
    soul = str(hello.get("soul", "jarvis")).lower()
    if soul not in SOULS:
        soul = "jarvis"
    brief = SOULS[soul]
    try:
        client = _client()
    except RuntimeError:
        await ws.send_text(json.dumps({"type": "error",
                                       "message": "server has no key"}))
        await ws.close(code=4401)
        return

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
                audio={"mime_type": "audio/pcm;rate=16000",
                       "data": chunk})

    try:
        async with client.aio.live.connect(
                model=MODEL,
                config={"response_modalities": ["AUDIO"],
                        "output_audio_transcription": {},
                        "input_audio_transcription": {},
                        "system_instruction": brief}) as session:
            queue: asyncio.Queue = asyncio.Queue()
            tin = asyncio.create_task(pump_in(session, queue))
            taud = asyncio.create_task(pump_audio(session, queue))
            try:
                async for m in session.receive():
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
