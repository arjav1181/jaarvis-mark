"""Switch the active Jaarvis soul by voice ("switch to Ultron").

Saves the persona and tells the user to continue — the running Live session
keeps the old soul until it reconnects, so the reply names who is taking
over; the switch lands on the next session build (same mechanism as a voice
change). Tone is not policy: all three souls share the same safety gates.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.personas import PERSONAS, valid_persona
from memory.config_manager import save_persona


def switch_persona_action(parameters: dict, ctx: dict) -> str:
    persona = valid_persona((parameters or {}).get("persona", ""))
    save_persona(persona)
    who = PERSONAS[persona]["name"]
    addr = PERSONAS[persona]["address"]
    return (f"{who} is taking over, {addr}. "
            f"He'll be fully himself from the next reply — talk to him.")


TOOL = {
    "name": "switch_persona",
    "description": ("Switch the speaking soul between jarvis, friday and "
                    "ultron when the user asks (e.g. 'switch to Ultron', "
                    "'let Friday take over')."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "persona": {
                "type": "STRING",
                "description": "Which soul: jarvis, friday or ultron"
            }
        },
        "required": [
            "persona"
        ]
    },
    "handler": switch_persona_action,
}
