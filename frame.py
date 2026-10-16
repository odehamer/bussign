# In-memory frame drawing, so the control page can show exactly what's on the sign.
#
# Stands in for the parts of rgbmatrix that main.py draws with: a canvas (SetPixel, SetImage,
# Clear, brightness), graphics (Color, Font, DrawText) and the matrix (CreateFrameCanvas,
# SwapOnVSync, Clear). Each finished frame goes to the LEDs in one SetImage call and is kept for
# the web page. Text follows rgbmatrix's bdf-font.cc so it lands on the same pixels.
import io
import threading
import time

from PIL import Image

REPLACEMENT_CHARACTER = 0xFFFD
TIMING_LOG_SECONDS = 60     # log drawing time once a minute...
TIMING_LOG_DURATION = 300   # ...for the first five minutes after startup


class Color:
    __slots__ = ("red", "green", "blue")

    def __init__(self, red=0, green=0, blue=0):
        self.red, self.green, self.blue = red, green, blue


class Font:
    # BDF font reader matching rgbmatrix's Font::LoadFont / DrawGlyph
    def __init__(self):
        self.glyphs = {}  # codepoint -> (advance width, height, y offset, [(x, y), ...] lit pixels)
        self.height = -1
        self.baseline = 0

    def LoadFont(self, path):
        codepoint = None
        device_width = 0
        glyph = None  # [width, height, x offset, y offset, rows]
        reading_bitmap = False
        with open(path, encoding="latin-1") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                key = parts[0]
                if key == "FONTBOUNDINGBOX" and len(parts) >= 5:
                    self.height = int(parts[2])
                    self.baseline = int(parts[4]) + self.height
                elif key == "ENCODING" and len(parts) >= 2:
                    codepoint = int(parts[1])
                elif key == "DWIDTH" and len(parts) >= 2:
                    device_width = min(int(parts[1]), 196)
                elif key == "BBX" and len(parts) >= 5:
                    glyph = [int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), []]
                    reading_bitmap = False
                elif key == "BITMAP":
                    reading_bitmap = glyph is not None
                elif key == "ENDCHAR":
                    if glyph is not None and len(glyph[4]) == glyph[1] and codepoint is not None and codepoint >= 0:
                        self.glyphs[codepoint] = self._make_glyph(device_width, glyph)
                    glyph = None
                    reading_bitmap = False
                elif reading_bitmap and glyph is not None and len(glyph[4]) < glyph[1]:
                    glyph[4].append(line.strip())

    @staticmethod
    def _make_glyph(device_width, glyph):
        _, height, x_offset, y_offset, rows = glyph
        pixels = []
        for y, hex_row in enumerate(rows):
            # Bits are read left-aligned, then shifted right by the x offset (a negative offset
            # shifts everything out in rgbmatrix, so the row is blank)
            if x_offset < 0:
                continue
            column = x_offset
            for char in hex_row:
                try:
                    nibble = int(char, 16)
                except ValueError:
                    break
                for bit in (8, 4, 2, 1):
                    if nibble & bit and column < device_width:
                        pixels.append((column, y))
                    column += 1
        return (device_width, height, y_offset, pixels)

    def CharacterWidth(self, codepoint):
        glyph = self.glyphs.get(codepoint)
        return glyph[0] if glyph else -1


def DrawText(canvas, font, x, y, color, text):
    start_x = x
    width, height = canvas.width, canvas.height
    buffer = canvas.buffer
    r, g, b = color.red, color.green, color.blue
    for char in text:
        glyph = font.glyphs.get(ord(char)) or font.glyphs.get(REPLACEMENT_CHARACTER)
        if glyph is None:
            continue
        advance, glyph_height, y_offset, pixels = glyph
        top = y - glyph_height - y_offset
        if not (x + advance < 0 or x > width or top + glyph_height < 0 or top > height):
            for dx, dy in pixels:
                px, py = x + dx, top + dy
                if 0 <= px < width and 0 <= py < height:
                    i = (py * width + px) * 3
                    buffer[i] = r
                    buffer[i + 1] = g
                    buffer[i + 2] = b
        x += advance
    return x - start_x


class Canvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.buffer = bytearray(width * height * 3)
        self.brightness = 100
        self.cleared_at = time.perf_counter()

    def Clear(self):
        self.buffer[:] = bytes(len(self.buffer))
        self.cleared_at = time.perf_counter()

    def SetPixel(self, x, y, r, g, b):
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 3
            self.buffer[i] = r
            self.buffer[i + 1] = g
            self.buffer[i + 2] = b

    def SetImage(self, image, x=0, y=0):
        if image.mode != "RGB":
            image = image.convert("RGB")
        data = image.tobytes()
        image_width, image_height = image.size
        # Copy whole rows at a time, clipped to the canvas
        left, right = max(0, x), min(self.width, x + image_width)
        if left >= right:
            return
        for row in range(max(0, -y), min(image_height, self.height - y)):
            src = (row * image_width + (left - x)) * 3
            dst = ((y + row) * self.width + left) * 3
            self.buffer[dst:dst + (right - left) * 3] = data[src:src + (right - left) * 3]


class Display:
    # Wraps the real RGBMatrix: frames are drawn into a Canvas, then pushed to the LEDs
    def __init__(self, matrix):
        self.matrix = matrix
        self.hardware_canvas = matrix.CreateFrameCanvas()
        self.width, self.height = self.hardware_canvas.width, self.hardware_canvas.height
        self.lock = threading.Lock()
        self.latest = bytes(self.width * self.height * 3)
        self.latest_number = 0
        self.png_cache = (-1, b"")
        self.started = time.monotonic()
        self.timing = {"frames": 0, "draw": 0.0, "draw_max": 0.0, "push": 0.0, "since": time.monotonic()}

    def CreateFrameCanvas(self):
        return Canvas(self.width, self.height)

    def SwapOnVSync(self, canvas):
        drawn = time.perf_counter()
        frame = bytes(canvas.buffer)
        self.hardware_canvas.brightness = canvas.brightness
        self.hardware_canvas.SetImage(Image.frombytes("RGB", (self.width, self.height), frame))
        self.hardware_canvas = self.matrix.SwapOnVSync(self.hardware_canvas)
        with self.lock:
            self.latest = frame
            self.latest_number += 1
        self._record_timing(drawn - canvas.cleared_at, time.perf_counter() - drawn)
        return canvas

    def Clear(self):
        self.matrix.Clear()

    def screen_png(self):
        # Latest frame as a PNG (re-encoded only when the frame changed)
        with self.lock:
            number, frame = self.latest_number, self.latest
        if self.png_cache[0] != number:
            out = io.BytesIO()
            Image.frombytes("RGB", (self.width, self.height), frame).save(out, format="PNG")
            self.png_cache = (number, out.getvalue())
        return self.png_cache[1]

    def _record_timing(self, draw_seconds, push_seconds):
        if time.monotonic() - self.started > TIMING_LOG_DURATION + TIMING_LOG_SECONDS:
            return
        t = self.timing
        t["frames"] += 1
        t["draw"] += draw_seconds
        t["draw_max"] = max(t["draw_max"], draw_seconds)
        t["push"] += push_seconds
        if time.monotonic() - t["since"] >= TIMING_LOG_SECONDS:
            n = t["frames"]
            print(f"Frame timing: {n} frames, draw avg {t['draw'] / n * 1000:.1f} ms "
                  f"(max {t['draw_max'] * 1000:.1f} ms), push avg {t['push'] / n * 1000:.1f} ms")
            self.timing = {"frames": 0, "draw": 0.0, "draw_max": 0.0, "push": 0.0, "since": time.monotonic()}
