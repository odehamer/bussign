# Phone control page for the sign: serves web/index.html and a small JSON API for messages.
# It only knows about the callbacks main.py hands it, never the display itself.
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 2048
MAX_MESSAGE_LENGTH = 100

# Phone keyboards insert "smart" punctuation the LED fonts don't have
REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


def clean_text(text):
    # Keep only printable ASCII (what the BDF fonts can draw) and collapse whitespace
    for fancy, plain in REPLACEMENTS.items():
        text = text.replace(fancy, plain)
    text = "".join(c for c in text if 32 <= ord(c) < 127 or c in "\n\t")
    return " ".join(text.split())[:MAX_MESSAGE_LENGTH]


def start_server(port, pin, colors, add_message, list_messages, delete_message, page_path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BusSign"
        sys_version = ""

        def log_message(self, format, *args):
            pass  # keep the journal quiet; main.py logs queued messages itself

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
            elif self.path == "/api/messages":
                if self.authorized():
                    self.send_json(200, {"messages": list_messages(), "colors": list(colors),
                                         "max_length": MAX_MESSAGE_LENGTH})
            else:
                self.send_json(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/api/messages":
                return self.send_json(404, {"error": "Not found"})
            # Requiring JSON means other websites can't post here without a CORS preflight we never answer
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send_json(415, {"error": "Send JSON"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                return self.send_json(413, {"error": "Message too large"})
            if not self.authorized():
                return
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                return self.send_json(400, {"error": "Invalid JSON"})
            if not isinstance(data, dict):
                return self.send_json(400, {"error": "Invalid JSON"})

            text = clean_text(str(data.get("text", "")))
            color = data.get("color", "white")
            if not text:
                return self.send_json(400, {"error": "Type a message"})
            if color not in colors:
                return self.send_json(400, {"error": "Unknown color"})
            message_id = add_message(text, color, bool(data.get("repeat")))
            if message_id is None:
                return self.send_json(429, {"error": "Too many messages queued; clear some first"})
            self.send_json(201, {"id": message_id, "text": text})

        def do_DELETE(self):
            if not self.authorized():
                return
            if self.path == "/api/messages":
                return self.send_json(200, {"deleted": delete_message(None)})
            prefix = "/api/messages/"
            if self.path.startswith(prefix) and self.path[len(prefix):].isdigit():
                return self.send_json(200, {"deleted": delete_message(int(self.path[len(prefix):]))})
            self.send_json(404, {"error": "Not found"})

    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server
