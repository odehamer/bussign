# Aquarium mode: fish swimming over a sandy bottom, with swaying seaweed and rising bubbles.
# Draws onto any canvas with SetPixel/SetImage (frame.Canvas on the sign).
import math
import random

from PIL import Image

WIDTH, HEIGHT = 64, 32
SAND_TOP = 28  # first row of sand

# Fish drawn facing right; "." is see-through. Each letter is a color from the fish's palette.
FISH_KINDS = {
    "clownfish": {
        "rows": ["....bb...",
                 "t.bbwbbw.",
                 "ttbbwbbwe",
                 "t.bbwbbw.",
                 "....bb..."],
        "colors": {"b": (255, 110, 0), "w": (255, 255, 255), "t": (230, 80, 0), "e": (10, 10, 20)},
    },
    "tang": {
        "rows": ["...bbb..",
                 "y.bkkbb.",
                 "yybbbbeb",
                 "y.bbkkb.",
                 "...bbb.."],
        "colors": {"b": (20, 90, 255), "k": (0, 20, 90), "y": (255, 220, 0), "e": (10, 10, 20)},
    },
    "tetra": {
        "rows": [".bbbb.",
                 "tcccce",
                 ".rrrr."],
        "colors": {"b": (150, 160, 170), "c": (0, 230, 255), "r": (255, 30, 40), "t": (120, 130, 140),
                   "e": (10, 10, 20)},
    },
}


def sprite(kind, facing_right):
    # Lit pixels as (dx, dy, color), mirrored for fish swimming left
    info = FISH_KINDS[kind]
    width = len(info["rows"][0])
    pixels = []
    for dy, row in enumerate(info["rows"]):
        for dx, key in enumerate(row):
            if key != ".":
                pixels.append((dx if facing_right else width - 1 - dx, dy, info["colors"][key]))
    return pixels, width


class Fish:
    def __init__(self, kind, rng):
        self.kind = kind
        self.rng = rng
        self.right = {d: sprite(kind, d) for d in (True, False)}
        self.width = self.right[True][1]
        self.height = len(FISH_KINDS[kind]["rows"])
        self.new_lap(start_inside=True)

    def new_lap(self, start_inside=False):
        # Pick a depth and speed, and enter from one side (or anywhere, at startup)
        rng = self.rng
        self.facing_right = rng.random() < 0.5
        self.speed = rng.uniform(4, 10) if self.kind != "tetra" else rng.uniform(8, 14)
        self.y = rng.uniform(2, SAND_TOP - self.height - 3)
        self.bob_phase = rng.uniform(0, math.tau)
        if start_inside:
            self.x = rng.uniform(0, WIDTH - self.width)
        else:
            self.x = -self.width - rng.uniform(0, 20) if self.facing_right else WIDTH + rng.uniform(0, 20)

    def update(self, dt):
        self.x += self.speed * dt * (1 if self.facing_right else -1)
        # Once it has swum off one side, come back later at a new depth
        if (self.facing_right and self.x > WIDTH + 2) or (not self.facing_right and self.x < -self.width - 2):
            self.new_lap()
        # Now and then a fish turns around mid-tank
        elif self.rng.random() < dt * 0.04:
            self.facing_right = not self.facing_right

    def draw(self, canvas, t):
        pixels, _ = self.right[self.facing_right]
        bob = round(math.sin(t * 1.6 + self.bob_phase) * 0.8)
        x0, y0 = round(self.x), round(self.y) + bob
        for dx, dy, color in pixels:
            canvas.SetPixel(x0 + dx, y0 + dy, *color)


class Aquarium:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.background = self._make_background()
        self.fish = [Fish(kind, self.rng) for kind in ("clownfish", "clownfish", "tang", "tetra", "tetra", "tetra")]
        # Seaweed: (x, height, sway phase, color)
        self.seaweed = [(4, 13, 0.0, (20, 170, 60)), (9, 9, 1.3, (10, 120, 40)),
                        (44, 15, 2.1, (20, 170, 60)), (49, 10, 0.7, (40, 190, 90)), (52, 7, 2.9, (10, 120, 40))]
        self.bubbler_x = 59
        self.bubbles = []  # [x, y, wobble phase]
        self.next_bubble = 0.0
        self.last_t = None

    def _make_background(self):
        # Water fading darker toward the bottom, then sand with a few pebbles; drawn once
        image = Image.new("RGB", (WIDTH, HEIGHT))
        for y in range(SAND_TOP):
            depth = y / (SAND_TOP - 1)
            color = (0, int(55 - 35 * depth), int(110 - 60 * depth))
            for x in range(WIDTH):
                image.putpixel((x, y), color)
        rng = random.Random(7)
        for y in range(SAND_TOP, HEIGHT):
            for x in range(WIDTH):
                shade = rng.randint(-18, 18)
                image.putpixel((x, y), (150 + shade, 120 + shade, 60 + shade // 2))
        for _ in range(9):
            x, y = rng.randint(0, WIDTH - 2), rng.randint(SAND_TOP + 1, HEIGHT - 1)
            image.putpixel((x, y), rng.choice([(110, 105, 100), (90, 80, 75), (170, 150, 130)]))
        return image

    def update(self, t):
        dt = 0.0 if self.last_t is None else min(0.2, t - self.last_t)
        self.last_t = t
        for fish in self.fish:
            fish.update(dt)
        # A bubble every half second or so, rising and wobbling
        if t >= self.next_bubble:
            self.bubbles.append([self.bubbler_x + self.rng.uniform(-0.5, 0.5), SAND_TOP - 1, self.rng.uniform(0, math.tau)])
            self.next_bubble = t + self.rng.uniform(0.35, 0.8)
        for bubble in self.bubbles:
            bubble[1] -= 7 * dt
        self.bubbles = [b for b in self.bubbles if b[1] > -1]

    def draw(self, canvas, t):
        self.update(t)
        canvas.SetImage(self.background, 0, 0)
        # Seaweed sways more toward the tip
        for x, height, phase, color in self.seaweed:
            for i in range(height):
                sway = round(math.sin(t * 1.3 + phase + i * 0.45) * (i / height) * 2)
                canvas.SetPixel(x + sway, SAND_TOP - 1 - i, *color)
        for fish in self.fish:
            fish.draw(canvas, t)
        # The bubbler, then its bubbles
        canvas.SetPixel(self.bubbler_x, SAND_TOP - 1, 120, 120, 130)
        canvas.SetPixel(self.bubbler_x + 1, SAND_TOP - 1, 120, 120, 130)
        for x, y, phase in self.bubbles:
            wobble = round(math.sin(t * 6 + phase) * 0.6)
            canvas.SetPixel(round(x) + wobble, round(y), 170, 220, 255)
