# Pixel painter: the drawing shown in drawing mode, edited from the control page, plus a small
# gallery of saved drawings. Stored as PNGs in a folder next to main.py.
import io
import os
import re
import threading
import time

from PIL import Image

MAX_SAVED = 60
SAVE_DELAY = 5  # seconds after the last stroke before the current drawing is written to disk


class Drawing:
    def __init__(self, folder, width=64, height=32, on_edit=None):
        self.folder = folder
        self.width, self.height = width, height
        self.on_edit = on_edit  # called after any edit from the control page
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self.pixels = bytearray(width * height * 3)
        self.version = 0
        self.cached = (-1, None)
        self.dirty_since = None
        current = os.path.join(folder, "current.png")
        if os.path.exists(current):
            try:
                self.pixels = bytearray(self._read_png(current))
            except Exception as e:
                print(f"Couldn't read {current}, starting blank: {e}")
        threading.Thread(target=self._save_loop, name="drawing-save", daemon=True).start()

    # ---- the current drawing ----
    def image(self):
        # The drawing as an RGB image, rebuilt only after it changes
        with self.lock:
            if self.cached[0] != self.version:
                self.cached = (self.version, Image.frombytes("RGB", (self.width, self.height), bytes(self.pixels)))
            return self.cached[1]

    def get_bytes(self):
        with self.lock:
            return bytes(self.pixels)

    def set_pixels(self, pixels):
        # pixels: [[x, y, r, g, b], ...]; returns an error message or None
        if not isinstance(pixels, list) or not pixels or len(pixels) > self.width * self.height:
            return "Send between 1 and %d pixels" % (self.width * self.height)
        for p in pixels:
            if (not isinstance(p, list) or len(p) != 5
                    or not all(isinstance(v, int) and not isinstance(v, bool) for v in p)
                    or not (0 <= p[0] < self.width and 0 <= p[1] < self.height)
                    or not all(0 <= v <= 255 for v in p[2:])):
                return "Each pixel must be [x, y, r, g, b] inside the sign"
        with self.lock:
            for x, y, r, g, b in pixels:
                i = (y * self.width + x) * 3
                self.pixels[i:i + 3] = bytes((r, g, b))
            self._changed()
        self._edited()
        return None

    def set_all(self, data):
        if not isinstance(data, (bytes, bytearray)) or len(data) != self.width * self.height * 3:
            return "The drawing must be exactly %d bytes" % (self.width * self.height * 3)
        with self.lock:
            self.pixels = bytearray(data)
            self._changed()
        self._edited()
        return None

    # ---- saved drawings ----
    def save_snapshot(self):
        saved = self.list_saved()
        if len(saved) >= MAX_SAVED:
            return None, f"The gallery holds {MAX_SAVED} drawings; delete one first"
        drawing_id = str(int(time.time() * 1000))
        self._write_png(self._path(drawing_id), self.get_bytes())
        return drawing_id, None

    def list_saved(self):
        names = [n[:-4] for n in os.listdir(self.folder) if re.fullmatch(r"\d{10,16}\.png", n)]
        return sorted(names, reverse=True)

    def saved_png(self, drawing_id):
        path = self._path(drawing_id)
        if not path or not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def load_saved(self, drawing_id):
        path = self._path(drawing_id)
        if not path or not os.path.exists(path):
            return "No saved drawing with that id"
        return self.set_all(self._read_png(path))

    def delete_saved(self, drawing_id):
        path = self._path(drawing_id)
        if not path or not os.path.exists(path):
            return "No saved drawing with that id"
        os.remove(path)
        return None

    # ---- helpers ----
    def _path(self, drawing_id):
        # Ids are only ever digits, so they can't point outside the folder
        if not isinstance(drawing_id, str) or not re.fullmatch(r"\d{10,16}", drawing_id):
            return None
        return os.path.join(self.folder, drawing_id + ".png")

    def _changed(self):
        self.version += 1
        self.dirty_since = time.monotonic()

    def _edited(self):
        if self.on_edit:
            self.on_edit()

    def _read_png(self, path):
        image = Image.open(path).convert("RGB")
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        return image.tobytes()

    def _write_png(self, path, data):
        out = io.BytesIO()
        Image.frombytes("RGB", (self.width, self.height), data).save(out, format="PNG")
        with open(path + ".tmp", "wb") as f:
            f.write(out.getvalue())
        os.replace(path + ".tmp", path)

    def _save_loop(self):
        # Write the current drawing a few seconds after editing stops, instead of on every stroke
        while True:
            time.sleep(1)
            if self.dirty_since is not None and time.monotonic() - self.dirty_since >= SAVE_DELAY:
                self.dirty_since = None
                try:
                    self._write_png(os.path.join(self.folder, "current.png"), self.get_bytes())
                except OSError as e:
                    print(f"Couldn't save the drawing: {e}")
