"""Local static frontend preview. No backend connection, proxy or credentials."""

import argparse
import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend/dist"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api" or path.startswith("/api/"):
            self.send_error(503, "Static preview: backend is not connected")
            return
        target = Path(self.translate_path(path)).resolve()
        if not target.is_relative_to(DIST.resolve()):
            self.send_error(403)
            return
        if not target.exists() and not Path(path).suffix:
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Existing Anaconda required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=2)
    args = parser.parse_args()
    if not 0 < args.hours <= 8 or not (DIST / "index.html").is_file():
        parser.error("Build the frontend first; preview duration must be within eight hours")
    server = ThreadingHTTPServer(("127.0.0.1", 5174), partial(Handler, directory=str(DIST)))
    timer = Timer(args.hours * 3600, server.shutdown)
    timer.daemon = True
    timer.start()
    print(json.dumps({"url": "http://127.0.0.1:5174/", "backend_connected": False}), flush=True)
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
