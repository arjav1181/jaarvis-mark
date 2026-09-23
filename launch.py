"""Jaarvis unified launcher — local mode vs server mode via env.

JAARVIS_MODE=server  -> voice Space bridge (browser mic, server brain)
anything else        -> desktop app (face, mic, speakers, system control)

One repo, one set of souls/tools/prompts. The mode only changes the I/O.
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def main() -> None:
    mode = os.environ.get("JAARVIS_MODE", "local").strip().lower()
    if mode == "server":
        import uvicorn
        port = int(os.environ.get("PORT", "7860"))
        uvicorn.run("space.server:app", host="0.0.0.0", port=port)
        return
    import runpy
    runpy.run_path(os.path.join(BASE, "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
