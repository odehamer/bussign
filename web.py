# Phone control page for the sign: serves web/index.html, the live screen and a small JSON API for settings.
# It only knows about the callbacks main.py hands it, never the display itself.
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 2048


def start_server(port, pin, page_path, settings_fields, get_settings, update_settings, get_screen_png,
                 get_active_mode):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BusSign"
        sys_version = ""

        def log_message(self, format, *args):
            pass  # keep the journal quiet; main.py logs settings changes itself

        def send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if not pin:
                return True
            if hmac.compare_digest(self.headers.get("X-Pin", "").encode(), pin.encode()):
                return True
            self.send_json(401, {"error": "PIN required"})
            return False

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/settings":
                if self.authorized():
                    # active_mode is what's on screen, which differs from the chosen mode in auto
                    self.send_json(200, {"values": get_settings(), "fields": settings_fields,
                                         "active_mode": get_active_mode()})
            elif self.path == "/api/screen.png":
                # What's on the sign right now, as a 64x32 PNG
                if self.authorized():
                    body = get_screen_png()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
            else:
                self.send_json(404, {"error": "Not found"})

        def read_json(self):
            # Returns the request's JSON object, or None after sending an error response
            # Requiring JSON means other websites can't post here without a CORS preflight we never answer
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.send_json(415, {"error": "Send JSON"})
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self.send_json(413, {"error": "Request too large"})
                return None
            if not self.authorized():
                return None
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                data = None
            if not isinstance(data, dict):
                self.send_json(400, {"error": "Invalid JSON"})
                return None
            return data

        def do_POST(self):
            if self.path == "/api/settings":
                data = self.read_json()
                if data is None:
                    return
                values, error = update_settings(data)
                if error:
                    return self.send_json(400, {"error": error})
                return self.send_json(200, {"values": values})
            self.send_json(404, {"error": "Not found"})

    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server
