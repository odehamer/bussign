# Ambient mode scenes: a crackling fireplace and rain running down a window at night (the aquarium
# lives in aquarium.py). Each scene draws onto any canvas with SetPixel/SetImage, like Aquarium.
import math
import random

from PIL import Image

import aquarium

WIDTH, HEIGHT = 64, 32


def blend(a, b, amount):
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


# ---------------- fireplace ----------------
FIRE_LEFT, FIRE_RIGHT = 8, 56   # firebox opening, columns [left, right)
FIRE_TOP, FIRE_BOTTOM = 7, 29   # rows [top, bottom)
FIRE_STEP = 0.05                # seconds between fire updates
FIRE_MAX_HEAT = 36
FIRE_COLORS = [(0, 0, 0), (50, 6, 0), (120, 16, 0), (190, 40, 0), (240, 90, 5), (255, 150, 20),
               (255, 200, 60), (255, 235, 140)]


def fire_palette():
    # Heat 0..FIRE_MAX_HEAT to a color, black through red and orange to pale yellow
    palette = []
    for heat in range(FIRE_MAX_HEAT + 1):
        pos = heat / FIRE_MAX_HEAT * (len(FIRE_COLORS) - 1)
        i = min(int(pos), len(FIRE_COLORS) - 2)
        palette.append(bytes(blend(FIRE_COLORS[i], FIRE_COLORS[i + 1], pos - i)))
    return palette


class Fireplace:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.width = FIRE_RIGHT - FIRE_LEFT
        self.height = FIRE_BOTTOM - FIRE_TOP
        self.heat = [[0] * self.width for _ in range(self.height + 1)]  # last row is the fuel bed
        self.palette = fire_palette()
        # Random numbers come from a table, which is much quicker on the Pi than calling random per pixel
        # Each entry is (cooling, sideways drift); drifting left and right equally keeps the flames upright
        self.table = [(self.rng.choice((0, 1, 2, 2, 3)), self.rng.choice((-1, 0, 0, 1))) for _ in range(4093)]
        self.table_pos = 0
        self.background = self._make_background()
        self.logs = self._make_logs()
        self.sparks = []  # [x, y, speed, life]
        self.next_step = 0.0
        self.flicker = 1.0
        self.last_t = None

    def _make_background(self):
        # Brick wall with a mantel, a black firebox and a stone hearth; drawn once
        image = Image.new("RGB", (WIDTH, HEIGHT))
        rng = random.Random(3)
        for y in range(HEIGHT):
            for x in range(WIDTH):
                course = y // 3
                offset = 0 if course % 2 else 3
                if y % 3 == 2 or (x + offset) % 7 == 0:
                    color = (28, 20, 18)  # mortar
                else:
                    shade = rng.randint(-10, 10)
                    color = (95 + shade, 36 + shade // 2, 26 + shade // 3)
                image.putpixel((x, y), color)
        for y in range(0, 3):  # mantel shelf
            for x in range(WIDTH):
                image.putpixel((x, y), (70, 42, 22) if y < 2 else (45, 26, 14))
        for y in range(FIRE_TOP - 1, FIRE_BOTTOM):  # firebox, with a darker lintel row on top
            for x in range(FIRE_LEFT - 1, FIRE_RIGHT + 1):
                inside = FIRE_LEFT <= x < FIRE_RIGHT and y >= FIRE_TOP
                image.putpixel((x, y), (6, 4, 4) if inside else (40, 34, 32))
        for y in range(FIRE_BOTTOM, HEIGHT):  # hearth
            for x in range(WIDTH):
                shade = rng.randint(-8, 8)
                image.putpixel((x, y), (80 + shade, 76 + shade, 72 + shade))
        return image

    def _make_logs(self):
        # Two logs in front of the flames: (x, y, color) pixels, plus ember spots that glow
        pixels, embers = [], []
        for x0, x1, y in ((13, 35, 26), (29, 51, 25)):
            for x in range(x0, x1):
                for dy in range(3):
                    bark = dy == 0 or (x * 7 + dy * 3) % 5 == 0
                    pixels.append((x, y + dy, (30, 16, 8) if bark else (70, 40, 18)))
            for dy in range(3):  # cut end
                pixels.append((x0, y + dy, (150, 110, 70) if dy == 1 else (110, 80, 50)))
            embers += [(x, y + 2) for x in range(x0 + 3, x1 - 1, 5)]
        return pixels, embers

    def _step(self):
        # Classic "doom fire": each cell takes the heat from below, cooled a little and nudged sideways
        width, height, heat = self.width, self.height, self.heat
        # The fuel bed: hottest in the middle, flickering
        self.flicker = 0.8 + 0.2 * self.rng.random()
        bed = heat[height]
        for x in range(width):
            edge = min(x, width - 1 - x) / (width / 2)
            strength = min(1.0, edge * 2.2) * self.flicker
            bed[x] = int(FIRE_MAX_HEAT * strength) if self.rng.random() > 0.1 else FIRE_MAX_HEAT // 2
        table, size = self.table, len(self.table)
        pos = self.table_pos
        for y in range(height):
            row, below = heat[y], heat[y + 1]
            for x in range(width):
                pos = pos + 1 if pos + 1 < size else 0
                cooling, drift = table[pos]
                value = below[x] - cooling
                dest = x + drift
                if 0 <= dest < width:
                    row[dest] = value if value > 0 else 0
        self.table_pos = pos
        # Now and then a spark floats up out of the fire
        if self.rng.random() < 0.25:
            self.sparks.append([self.rng.uniform(FIRE_LEFT + 12, FIRE_RIGHT - 12), FIRE_BOTTOM - 6,
                                self.rng.uniform(6, 12), self.rng.uniform(0.6, 1.4)])

    def draw(self, canvas, t):
        dt = 0.0 if self.last_t is None else min(0.2, t - self.last_t)
        self.last_t = t
        if t >= self.next_step:
            self._step()
            self.next_step = t + FIRE_STEP
        canvas.SetImage(self.background, 0, 0)
        # The flames, as one image over the firebox
        palette, data = self.palette, bytearray()
        for row in self.heat[:self.height]:
            for value in row:
                data += palette[value]
        flames = Image.frombytes("RGB", (self.width, self.height), bytes(data))
        canvas.SetImage(flames, FIRE_LEFT, FIRE_TOP)
        logs, embers = self.logs
        for x, y, color in logs:
            canvas.SetPixel(x, y, *color)
        for i, (x, y) in enumerate(embers):
            glow = 0.5 + 0.5 * math.sin(t * 2.3 + i * 1.7)
            canvas.SetPixel(x, y, int(140 + 115 * glow), int(30 + 50 * glow), 0)
        # Firelight on the hearth
        light = int(40 * self.flicker)
        for x in range(FIRE_LEFT + 4, FIRE_RIGHT - 4, 2):
            canvas.SetPixel(x, FIRE_BOTTOM, 80 + light, 60 + light // 2, 40)
        for spark in self.sparks:
            spark[1] -= spark[2] * dt
            spark[0] += math.sin(t * 5 + spark[2]) * dt * 4
            spark[3] -= dt
            if spark[3] > 0 and spark[1] >= FIRE_TOP:
                fade = min(1.0, spark[3])
                canvas.SetPixel(round(spark[0]), round(spark[1]), 255, int(160 * fade), int(40 * fade))
        self.sparks = [s for s in self.sparks if s[3] > 0 and s[1] >= FIRE_TOP]


# ---------------- rain on the window ----------------
MULLION_X = 31          # the window's middle bars
TRANSOM_Y = 15
MAX_BEADS = 60
LIGHTNING_EVERY = (45, 150)  # seconds between lightning flashes


class RainyWindow:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.background, self.flash_background, self.frame = self._make_backgrounds()
        self.beads = {}     # (x, y) -> brightness, still droplets on the glass
        self.runners = []   # [x, y, speed, pause, wander]
        self.trails = {}    # (x, y) -> seconds left, the wet line a runner leaves behind
        self.streaks = [[self.rng.uniform(0, WIDTH + 16), self.rng.uniform(0, HEIGHT), self.rng.uniform(40, 60)]
                        for _ in range(22)]
        self.next_runner = 0.0
        self.next_lightning = self.rng.uniform(*LIGHTNING_EVERY)
        self.lightning_at = None
        self.last_t = None

    def _make_backgrounds(self):
        # A city at night through the glass, with the window frame on top; drawn once
        rng = random.Random(11)
        image = Image.new("RGB", (WIDTH, HEIGHT))
        for y in range(HEIGHT):
            sky = blend((4, 6, 18), (22, 20, 38), y / (HEIGHT - 1))
            for x in range(WIDTH):
                image.putpixel((x, y), sky)
        x = 0
        while x < WIDTH:
            width, top = rng.randint(5, 11), rng.randint(9, 22)
            body = (rng.randint(10, 16), rng.randint(10, 14), rng.randint(18, 24))
            for bx in range(x, min(WIDTH, x + width)):
                for by in range(top, HEIGHT):
                    image.putpixel((bx, by), body)
            for wy in range(top + 2, HEIGHT - 1, 3):
                for wx in range(x + 1, min(WIDTH, x + width) - 1, 2):
                    if rng.random() < 0.35:
                        image.putpixel((wx, wy), rng.choice([(110, 80, 30), (90, 70, 40), (70, 80, 95), (120, 95, 45)]))
            x += width + rng.randint(0, 2)
        flash = Image.eval(image, lambda v: min(255, int(v * 2.2) + 40))
        frame = set()
        for x in range(WIDTH):
            frame |= {(x, 0), (x, HEIGHT - 1), (x, TRANSOM_Y)}
        for y in range(HEIGHT):
            frame |= {(0, y), (WIDTH - 1, y), (MULLION_X, y), (MULLION_X + 1, y)}
        for picture in (image, flash):
            for x, y in frame:
                picture.putpixel((x, y), (34, 24, 16) if x == MULLION_X + 1 else (48, 34, 24))
        return image, flash, frame

    def update(self, t):
        dt = 0.0 if self.last_t is None else min(0.2, t - self.last_t)
        self.last_t = t
        rng = self.rng
        # New beads land on the glass, more while it's emptier
        for _ in range(3):
            if len(self.beads) < MAX_BEADS and rng.random() < dt * 8:
                spot = (rng.randrange(1, WIDTH - 1), rng.randrange(1, HEIGHT - 1))
                if spot not in self.frame:
                    self.beads[spot] = rng.uniform(0.5, 1.0)
        # Every so often a big drop runs down, stopping and starting, sweeping up beads on the way
        if t >= self.next_runner:
            self.runners.append([rng.randrange(2, WIDTH - 2), rng.uniform(1, 12), 0.0, 0.0, 0.0])
            self.next_runner = t + rng.uniform(0.8, 2.5)
        for runner in self.runners:
            if runner[3] > 0:
                runner[3] -= dt
                continue
            runner[2] = min(26.0, runner[2] + dt * 30)
            old = (round(runner[0]), round(runner[1]))
            runner[1] += runner[2] * dt
            runner[4] += rng.uniform(-1, 1) * dt * 3
            runner[0] += max(-0.6, min(0.6, runner[4])) * dt * 4
            if rng.random() < dt * 0.8:
                runner[2], runner[3] = 0.0, rng.uniform(0.2, 0.9)  # snag on the glass for a moment
            x, y = round(runner[0]), round(runner[1])
            for ty in range(old[1], y + 1):
                self.trails[(x, ty)] = 1.6
            for spot in [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (0, 1)]:
                self.beads.pop(spot, None)
        self.runners = [r for r in self.runners if r[1] < HEIGHT]
        for spot in list(self.trails):
            self.trails[spot] -= dt
            if self.trails[spot] <= 0:
                del self.trails[spot]
        # Rain falling outside, behind the glass
        for streak in self.streaks:
            streak[1] += streak[2] * dt
            streak[0] -= streak[2] * dt * 0.25
            if streak[1] > HEIGHT + 3:
                streak[0], streak[1] = rng.uniform(0, WIDTH + 16), rng.uniform(-6, 0)
        # Lightning: two quick flashes now and then
        if t >= self.next_lightning:
            self.lightning_at = t
            self.next_lightning = t + rng.uniform(*LIGHTNING_EVERY)

    def draw(self, canvas, t):
        self.update(t)
        since = None if self.lightning_at is None else t - self.lightning_at
        flashing = since is not None and (since < 0.08 or 0.18 < since < 0.3)
        canvas.SetImage(self.flash_background if flashing else self.background, 0, 0)
        glass = lambda x, y: 0 <= x < WIDTH and 0 <= y < HEIGHT and (x, y) not in self.frame
        for x, y, speed in self.streaks:
            for i in range(3):
                px, py = round(x + i * 0.25), round(y - i)
                if glass(px, py):
                    canvas.SetPixel(px, py, 34 + i * 6, 42 + i * 6, 60 + i * 8)
        for (x, y), left in self.trails.items():
            if glass(x, y):
                level = min(1.0, left)
                canvas.SetPixel(x, y, int(30 + 30 * level), int(38 + 34 * level), int(55 + 40 * level))
        for (x, y), bright in self.beads.items():
            canvas.SetPixel(x, y, int(70 * bright), int(85 * bright), int(110 * bright))
        for x, y, *_ in self.runners:
            px, py = round(x), round(y)
            if glass(px, py):
                canvas.SetPixel(px, py, 170, 190, 220)
            if glass(px, py + 1):
                canvas.SetPixel(px, py + 1, 110, 125, 150)


# The choices for ambient mode, in the order the control page shows them
SCENES = {
    "aquarium": aquarium.Aquarium,
    "fireplace": Fireplace,
    "rain": RainyWindow,
}
