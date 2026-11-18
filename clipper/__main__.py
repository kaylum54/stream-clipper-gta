"""python -m clipper  -> starts the pipeline and the GUI on http://127.0.0.1:8765"""
import os
import sys
import threading
import webbrowser

import uvicorn

from clipper.app import create_app


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # stream titles carry emoji; cp1252 consoles crash on them
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    port = int(os.environ.get("CLIPPER_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Stream Clipper is running at {url}  (close this window to stop it)")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
