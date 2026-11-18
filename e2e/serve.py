"""Runs the real app for browser tests: background loops off, live checks faked as offline (no network).
CLIPPER_DATA, CLIPPER_PORT and POSTFORME_BASE come from the environment."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from clipper.app import create_app  # noqa: E402
from clipper.pipeline import Pipeline  # noqa: E402
from clipper.platforms import LiveInfo  # noqa: E402


async def offline(streamers, cfg, http):
    return {s["id"]: LiveInfo(live=False) for s in streamers}


if __name__ == "__main__":
    app = create_app(lambda: Pipeline(check_live=offline, use_whisper=False), start_loops=False)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["CLIPPER_PORT"]), log_level="warning")
