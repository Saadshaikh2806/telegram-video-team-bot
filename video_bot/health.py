"""Public health endpoint. It never returns tokens, jobs, names, or group IDs."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Health:
    def __init__(self):
        self.last_progress = time.monotonic()
        self.server = None

    def beat(self):
        self.last_progress = time.monotonic()

    def start(self, port):
        health = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ('/', '/health', '/healthz'):
                    self.send_error(404)
                    return
                ok = time.monotonic() - health.last_progress < 180
                data = json.dumps({'status': 'ok' if ok else 'stalled'}).encode()
                self.send_response(200 if ok else 503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server
