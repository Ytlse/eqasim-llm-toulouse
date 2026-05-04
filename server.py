"""
Minimal HTTP service wrapper for the eqasim synpp pipeline.

Endpoints:
  GET  /health   → 200 "ok"
  POST /generate → runs generate_population.py logic with the given population_size
                   body: {"population_size": N}
                   blocks until generation completes, returns {"status": "ok"|"error", "file": "..."}
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import generate_population as genpop


_lock = threading.Lock()


class EqasimHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: suppress default access-log noise
        print(f"[eqasim-server] {fmt % args}")

    def _send_json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/generate":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        population_size = body.get("population_size")
        generate_personality = str(body.get("generate_personality", False)).lower() == "true"
        force = str(body.get("force", False)).lower() == "true"
        bbox = body.get("bbox")  # optional [min_lon, min_lat, max_lon, max_lat]

        # Serialise concurrent requests — synpp is not re-entrant
        with _lock:
            try:
                result_file = genpop.run(
                    population_size=population_size,
                    generate_personality=generate_personality,
                    force=force,
                    bbox=bbox,
                )
                self._send_json(200, {"status": "ok", "file": result_file or ""})
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                self._send_json(500 if code != 0 else 200, {"status": "error", "exit_code": code})
            except Exception as exc:
                self._send_json(500, {"status": "error", "detail": str(exc)})


def main():
    port = 8003
    server = HTTPServer(("0.0.0.0", port), EqasimHandler)
    print(f"[eqasim-server] Listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
