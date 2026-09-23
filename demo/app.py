"""Jaarvis Space demo — text chat with the three souls (Gradio).

Runs on Hugging Face Spaces (or anywhere). The Gemini key comes from the
environment (Space Secrets as GEMINI_API_KEY) — never in code.
Full voice + face + system control live in the desktop app (see repo root).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gradio as gr
from google import genai

from core.personas import PERSONAS

# Souls come from core/personas.py — the SAME file everything else uses.

_client = None


def client():
    global _client
    if _client is None:
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("Set GEMINI_API_KEY (Space Secrets) first.")
        _client = genai.Client(api_key=key)
    return _client


def chat(message, history, persona):
    soul = PERSONAS.get(persona, PERSONAS["jarvis"])
    name, brief = soul["name"], soul["brief"]
    sys = brief + " You are JAARVIS, a real-time personal AI assistant."
    turns = [{"role": "user" if i % 2 == 0 else "model",
              "parts": [{"text": h[0] if isinstance(h, (list, tuple)) else h}]}
             for i, h in enumerate(history or [])]
    resp = client().models.generate_content(
        model="gemini-2.5-flash",
        contents=turns + [{"role": "user", "parts": [{"text": message}]}],
        config={"system_instruction": sys},
    )
    return resp.text or "(no reply — try again)"


with gr.Blocks(title="JAARVIS") as demo:
    gr.Markdown("# JAARVIS — text demo\nThree souls, one assistant. "
                "Voice + face + system control live in the desktop app.")
    persona = gr.Dropdown(choices=list(PERSONAS), value="jarvis", label="Soul")
    gr.ChatInterface(fn=chat, additional_inputs=[persona])

if __name__ == "__main__":
    demo.launch()
