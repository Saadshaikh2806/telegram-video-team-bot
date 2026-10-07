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

    def start(self, port, dashboard=None, host='0.0.0.0'):
        health = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if dashboard and self.path.split('?')[0] not in ('/health', '/healthz'):
                    dashboard.handle(self)
                    return
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

            def do_POST(self):
                if dashboard:
                    dashboard.handle(self)
                else:
                    self.send_error(404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server
