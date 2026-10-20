# Phone control page for the sign: serves web/index.html, the live screen, and a small JSON API for
# settings, the pixel painter and the games. It only knows about the callbacks main.py hands it, never the display.
import base64
import binascii
import hmac
import json
import re
import threading
import time
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 2048
MAX_DRAWING_BODY_BYTES = 64 * 1024  # a whole 64x32 drawing, or a long batch of strokes


def start_server(port, pin, page_path, settings_fields, get_settings, update_settings, get_screen_png,
                 get_active_mode, drawing, blackjack, hangman, slots, players, sportsbook, pool):
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

        def send_png(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
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
            saved = re.fullmatch(r"/api/drawings/(\d{10,16})\.png", self.path)
            # Game states are shown from one player's point of view: /api/blackjack?player=ID
            route, _, query = self.path.partition("?")
            viewer = (parse_qs(query).get("player") or [None])[0]
            if self.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif not self.path.startswith("/api/"):
                self.send_json(404, {"error": "Not found"})
            elif not self.authorized():
                return
            elif self.path == "/api/settings":
                # active_mode is what's on screen, which differs from the chosen mode in auto
                self.send_json(200, {"values": get_settings(), "fields": settings_fields,
                                     "active_mode": get_active_mode()})
            elif self.path == "/api/screen.png":
                # What's on the sign right now, as a 64x32 PNG
                self.send_png(get_screen_png())
            elif self.path == "/api/drawing":
                self.send_json(200, {"width": drawing.width, "height": drawing.height,
                                     "image": base64.b64encode(drawing.get_bytes()).decode()})
            elif self.path == "/api/drawings":
                self.send_json(200, {"drawings": drawing.list_saved()})
            elif route == "/api/blackjack":
                self.send_json(200, blackjack.state(viewer))
            elif route == "/api/hangman":
                self.send_json(200, hangman.state())
            elif route == "/api/slots":
                self.send_json(200, slots.state(time.monotonic(), viewer))
            elif route == "/api/pool":
                self.send_json(200, pool.state(time.monotonic(), viewer))
            elif route == "/api/bets":
                self.send_json(200, sportsbook.state(viewer))
            elif route == "/api/players":
                self.send_json(200, {"players": players.public()})
            elif saved:
                body = drawing.saved_png(saved.group(1))
                if body is None:
                    return self.send_json(404, {"error": "Not found"})
                self.send_png(body)
            else:
                self.send_json(404, {"error": "Not found"})

        def read_json(self, max_bytes=MAX_BODY_BYTES):
            # Returns the request's JSON object, or None after sending an error response
            # Requiring JSON means other websites can't post here without a CORS preflight we never answer
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.send_json(415, {"error": "Send JSON"})
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > max_bytes:
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
            load = re.fullmatch(r"/api/drawings/(\d{10,16})/load", self.path)
            if self.path == "/api/settings":
                data = self.read_json()
                if data is None:
                    return
                values, error = update_settings(data)
                if error:
                    return self.send_json(400, {"error": error})
                return self.send_json(200, {"values": values})
            if self.path == "/api/drawing/pixels":
                data = self.read_json(MAX_DRAWING_BODY_BYTES)
                if data is None:
                    return
                error = drawing.set_pixels(data.get("pixels"))
                return self.send_json(400, {"error": error}) if error else self.send_json(200, {"ok": True})
            if self.path == "/api/drawing":
                data = self.read_json(MAX_DRAWING_BODY_BYTES)
                if data is None:
                    return
                try:
                    image = base64.b64decode(str(data.get("image", "")), validate=True)
                except (binascii.Error, ValueError):
                    return self.send_json(400, {"error": "image must be base64"})
                error = drawing.set_all(image)
                return self.send_json(400, {"error": error}) if error else self.send_json(200, {"ok": True})
            if self.path == "/api/drawings":
                if self.read_json() is None:
                    return
                drawing_id, error = drawing.save_snapshot()
                return self.send_json(400, {"error": error}) if error else self.send_json(201, {"id": drawing_id})
            if self.path == "/api/blackjack":
                # {"action": "deal" | "hit" | "stand" | "double" | "rebuy"} or {"action": "bet", "amount": 50}
                data = self.read_json()
                if data is None:
                    return
                player = data.get("player")
                error = blackjack.act(data.get("action"), data.get("amount"), player)
                return self.send_json(400, {"error": error}) if error else self.send_json(200, blackjack.state(player))
            if self.path == "/api/slots":
                # {"action": "spin"}, {"action": "bet", "amount": 25} or {"action": "rebuy"}
                data = self.read_json()
                if data is None:
                    return
                player = data.get("player")
                error = slots.act(data.get("action"), data.get("amount"), time.monotonic(), player)
                return self.send_json(400, {"error": error}) if error else self.send_json(200, slots.state(time.monotonic(), player))
            if self.path == "/api/pool":
                # {"action": "new", "opponent": ID}, {"action": "aim" or "shoot", "angle": radians, "power": 0-1},
                # {"action": "place", "x": 10, "y": 16} (ball in hand) or {"action": "forfeit"}
                data = self.read_json()
                if data is None:
                    return
                player = data.get("player")
                error = pool.act(data.get("action"), data, player, time.monotonic())
                return self.send_json(400, {"error": error}) if error else self.send_json(200, pool.state(time.monotonic(), player))
            if self.path == "/api/bets":
                # {"action": "place", "game": ID, "team": "ATL", "amount": 50} or {"action": "cancel", "bet": 3}
                data = self.read_json()
                if data is None:
                    return
                player = data.get("player")
                error = sportsbook.act(data.get("action"), data, player)
                return self.send_json(400, {"error": error}) if error else self.send_json(200, sportsbook.state(player))
            if self.path == "/api/players":
                # {"action": "create", "name": "Sam"} or {"action": "delete", "player": ID}
                data = self.read_json()
                if data is None:
                    return
                if data.get("action") == "create":
                    player_id, error = players.create(data.get("name"))
                    if error:
                        return self.send_json(400, {"error": error})
                    return self.send_json(201, {"id": player_id, "players": players.public()})
                if data.get("action") == "delete":
                    error = players.delete(data.get("player"))
                    return self.send_json(404, {"error": error}) if error else self.send_json(200, {"players": players.public()})
                return self.send_json(400, {"error": "Unknown action"})
            if self.path == "/api/hangman":
                # {"action": "start", "word": "FALCONS"}, {"action": "guess", "letter": "A"},
                # {"action": "random"} or {"action": "reset"}
                data = self.read_json()
                if data is None:
                    return
                action = data.get("action")
                error = hangman.act(action, data.get("word") if action == "start" else data.get("letter"), data.get("player"))
                return self.send_json(400, {"error": error}) if error else self.send_json(200, hangman.state())
            if load:
                if self.read_json() is None:
                    return
                error = drawing.load_saved(load.group(1))
                return self.send_json(404, {"error": error}) if error else self.send_json(200, {"ok": True})
            self.send_json(404, {"error": "Not found"})

        def do_DELETE(self):
            if not self.authorized():
                return
            saved = re.fullmatch(r"/api/drawings/(\d{10,16})", self.path)
            if not saved:
                return self.send_json(404, {"error": "Not found"})
            error = drawing.delete_saved(saved.group(1))
            self.send_json(404, {"error": error}) if error else self.send_json(200, {"ok": True})

    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server
