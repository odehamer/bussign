# Slot machine mode: three reels of pixel fruit, spun from the control page. Each spin is paid from
# and into the chips of the player who spun (players.py), the same chips blackjack uses.
import random
import threading

from PIL import Image

import frame

BETS = [10, 25, 50, 100]
SPIN_SECONDS = (1.2, 1.7, 2.2)   # when each reel stops, after the spin starts
SPIN_SYMBOLS = (18, 24, 30)      # how many symbols each reel rolls past before it stops

# How often each symbol is on a reel, and what three of a kind pays (times the bet). Tuned so the
# machine pays back about 95% of what goes in, and something wins a bit over a third of the time.
WEIGHTS = {"cherry": 3, "lemon": 6, "bell": 5, "star": 4, "bar": 3, "seven": 2}
THREE_OF_A_KIND = {"seven": 50, "bar": 25, "star": 16, "bell": 12, "lemon": 8, "cherry": 20}
CHERRY_PAYS = {2: 4, 1: 1}        # cherries anywhere on the line, when it isn't three of a kind
NAMES = {"cherry": "Cherry", "lemon": "Lemon", "bell": "Bell", "star": "Star", "bar": "BAR", "seven": "7"}

SPRITES = {
    "seven": ([".rrrrrrrrrr.",
               ".rrrrrrrrrr.",
               "........rrr.",
               ".......rrr..",
               "......rrr...",
               ".....rrr....",
               ".....rrr....",
               "....rrr.....",
               "....rrr.....",
               "...rrr......",
               "...rrr......",
               "...rrr......"], {"r": (240, 30, 40)}),
    "cherry": (["........gg..",
                ".......g.g..",
                "......g..g..",
                ".....g...g..",
                "....g....g..",
                "...g.....g..",
                ".rrr....rrr.",
                "rrwrr..rrwrr",
                "rrrrr..rrrrr",
                "rrrrr..rrrrr",
                ".rrr....rrr.",
                "............"], {"r": (220, 20, 40), "g": (40, 170, 40), "w": (255, 190, 190)}),
    "lemon": (["............",
               "............",
               "...yyyyyy...",
               "..yyywwyyy..",
               ".yyyyyyyyyy.",
               "yyyyyyyyyyyy",
               "yyyyyyyyyyyy",
               ".yyyyyyyyyy.",
               "..yyyyyyyy..",
               "...yyyyyy...",
               "............",
               "............"], {"y": (250, 230, 30), "w": (255, 255, 190)}),
    "bell": ([".....bb.....",
              "....bbbb....",
              "...bbbbbb...",
              "...bbwbbb...",
              "...bwbbbb...",
              "..bbbbbbbb..",
              "..bbbbbbbb..",
              "..bbbbbbbb..",
              ".bbbbbbbbbb.",
              "bbbbbbbbbbbb",
              ".....dd.....",
              ".....dd....."], {"b": (255, 170, 0), "w": (255, 235, 150), "d": (150, 90, 0)}),
    "star": ([".....ss.....",
              ".....ss.....",
              "....ssss....",
              "ssssssssssss",
              ".ssssssssss.",
              "..ssssssss..",
              "...ssssss...",
              "...ssssss...",
              "..sssssssss.",
              "..sss..sss..",
              ".sss....sss.",
              ".ss......ss."], {"s": (40, 210, 255)}),
    "bar": (["..............",
             "..............",
             "..............",
             "pppppppppppppp",
             "pkkpppkppkkppp",
             "pkpkpkpkpkpkpp",
             "pkkppkkkpkkppp",
             "pkpkpkpkpkpkpp",
             "pkkppkpkpkpkpp",
             "pppppppppppppp",
             "..............",
             ".............."], {"p": (225, 225, 235), "k": (20, 20, 40)}),
}
SPRITE_PIXELS = {name: [(x, y, colors[key]) for y, row in enumerate(rows) for x, key in enumerate(row) if key != "."]
                 for name, (rows, colors) in SPRITES.items()}
SPRITE_WIDTHS = {name: len(rows[0]) for name, (rows, _) in SPRITES.items()}

# Layout: three windows with a frame, the payline marked at both sides, chips and bet underneath
WINDOW_X = (3, 23, 43)
WINDOW_W, WINDOW_TOP, WINDOW_BOTTOM = 18, 1, 21   # window interior, rows [top, bottom]
CENTER_Y = 11
PITCH = 15                                        # rows from one symbol to the next on a reel
FRAME = (170, 120, 30)
GOLD = frame.Color(255, 190, 0)
GRAY = frame.Color(140, 140, 140)
GREEN = frame.Color(60, 230, 90)


def make_strip(seed):
    strip = [name for name, weight in WEIGHTS.items() for _ in range(weight)]
    random.Random(seed).shuffle(strip)
    return strip


def payout_multiplier(line):
    if line[0] == line[1] == line[2]:
        return THREE_OF_A_KIND[line[0]]
    return CHERRY_PAYS.get(line.count("cherry"), 0)


def make_background():
    image = Image.new("RGB", (64, 32))
    for x0 in WINDOW_X:
        for y in range(WINDOW_TOP - 1, WINDOW_BOTTOM + 2):
            for x in range(x0 - 1, x0 + WINDOW_W + 1):
                inside = x0 <= x < x0 + WINDOW_W and WINDOW_TOP <= y <= WINDOW_BOTTOM
                # Glass slightly lighter in the middle, like a lit window
                shade = 26 - abs(y - CENTER_Y) * 2
                image.putpixel((x, y), (shade, shade, shade + 10) if inside else FRAME)
    return image


class SlotMachine:
    def __init__(self, players, fonts, on_play=None):
        self.players = players      # player profiles: chips, stats, names
        self.fonts = fonts          # {"tiny": 4x6 font}
        self.on_play = on_play      # called after any action, to put the machine on the sign
        self.lock = threading.Lock()
        self.rng = random.Random()
        self.strips = [make_strip(seed) for seed in (1, 2, 3)]
        self.stops = [self.rng.randrange(len(s)) for s in self.strips]
        self.started = None         # time.monotonic() of the current spin
        self.phase = "idle"         # idle -> spinning -> done
        self.bet = 25
        self.spin_bet = 0
        self.win = 0
        self.spinner = None         # id of the player whose spin is on the reels
        self.background = make_background()

    # ---- actions from the control page; each returns an error message or None ----
    def act(self, action, amount, now, player=None):
        with self.lock:
            self.tick(now)
            if not self.players.exists(player):
                return "Pick a player first"
            if action == "bet":
                if self.phase == "spinning":
                    return "Wait for the reels to stop"
                if type(amount) is not int or amount not in BETS:
                    return "Pick one of the bet sizes"
                self.bet = amount
            elif action == "spin":
                if self.phase == "spinning":
                    return "The reels are still spinning"
                if not self.players.spend(player, self.bet):
                    return "Not enough chips for that bet" if self.players.chips(player) >= BETS[0] else "Out of chips. Tap Rebuy"
                self.stops = [self.rng.randrange(len(s)) for s in self.strips]
                self.started, self.phase, self.spin_bet, self.win = now, "spinning", self.bet, 0
                self.spinner = player
            elif action == "rebuy":
                if self.phase == "spinning" and player == self.spinner:
                    return "Wait for the reels to stop"
                error = self.players.rebuy(player)
                if error:
                    return error
            else:
                return "Unknown action"
        if self.on_play:
            self.on_play()
        return None

    def tick(self, now):
        # Pay out once the last reel has stopped
        if self.phase == "spinning" and now - self.started >= SPIN_SECONDS[-1]:
            line = self.line()
            self.win = self.spin_bet * payout_multiplier(line)
            if self.win:
                self.players.pay(self.spinner, self.win)
            self.players.record(self.spinner, win=self.win - self.spin_bet, slot_spins=1,
                                slot_jackpots=1 if line == ["seven"] * 3 else 0)
            self.phase = "done"

    def line(self):
        return [strip[stop] for strip, stop in zip(self.strips, self.stops)]

    def state(self, now, viewer=None):
        with self.lock:
            self.tick(now)
            spinning = self.phase == "spinning"
            chips = self.players.chips(viewer)
            return {
                "phase": self.phase,
                "chips": chips,
                "spinner": self.players.name(self.spinner) if self.spinner else "",
                "mine": viewer is not None and viewer == self.spinner,
                "bet": self.bet,
                "bets": BETS,
                "spin_bet": self.spin_bet,
                "line": None if spinning else [NAMES[name] for name in self.line()],
                "win": 0 if spinning else self.win,
                "can_rebuy": self.players.exists(viewer) and chips < BETS[0]
                             and not (spinning and viewer == self.spinner),
                "paytable": [[f"{NAMES[n]} {NAMES[n]} {NAMES[n]}", THREE_OF_A_KIND[n]] for n in THREE_OF_A_KIND]
                            + [["Two cherries", CHERRY_PAYS[2]], ["One cherry", CHERRY_PAYS[1]]],
            }

    # ---- drawing on the sign ----
    def draw(self, canvas, t):
        with self.lock:
            self.tick(t)
            canvas.SetImage(self.background, 0, 0)
            for reel, x0 in enumerate(WINDOW_X):
                self._draw_reel(canvas, reel, x0, t)
            won = self.phase == "done" and self.win > 0
            blink = won and int(t * 4) % 2 == 0
            # Payline markers either side of the middle row, lit up on a win
            marker = (60, 230, 90) if blink else (230, 40, 40)
            for dy in (-1, 0, 1):
                canvas.SetPixel(0, CENTER_Y + dy, *marker)
                canvas.SetPixel(63, CENTER_Y + dy, *marker)
            canvas.SetPixel(1, CENTER_Y, *marker)
            canvas.SetPixel(62, CENTER_Y, *marker)
            tiny = self.fonts["tiny"]
            if won:
                text = f"WIN ${self.win}"
                width = sum(tiny.CharacterWidth(ord(c)) for c in text)
                frame.DrawText(canvas, tiny, 32 - width // 2, 30, GREEN if blink else GOLD, text)
            elif self.spinner:
                # Whose spin it is, and their chips
                name = self.players.name(self.spinner).upper()[:7]
                frame.DrawText(canvas, tiny, 2, 30, frame.Color(*self.players.color(self.spinner)), name)
                chips = f"${self.players.chips(self.spinner)}"
                frame.DrawText(canvas, tiny, 62 - sum(tiny.CharacterWidth(ord(c)) for c in chips), 30, GOLD, chips)
            else:
                frame.DrawText(canvas, tiny, 2, 30, GRAY, "SPIN FROM GAMES")

    def _reel_position(self, reel, t):
        # Fractional index on the strip under the payline; the reel slows down as it stops
        stop = self.stops[reel]
        if self.phase != "spinning":
            return float(stop)
        progress = min(1.0, (t - self.started) / SPIN_SECONDS[reel])
        eased = 1 - (1 - progress) ** 3
        return stop - SPIN_SYMBOLS[reel] * (1 - eased)

    def _draw_reel(self, canvas, reel, x0, t):
        strip = self.strips[reel]
        position = self._reel_position(reel, t)
        base = int(position // 1)
        offset = position - base
        for k in (-1, 0, 1):
            name = strip[(base + k) % len(strip)]
            # Later symbols on the strip sit higher, so the reel rolls downward as the position grows
            top = round(CENTER_Y - 6 - (k - offset) * PITCH)
            left = x0 + (WINDOW_W - SPRITE_WIDTHS[name]) // 2
            for dx, dy, color in SPRITE_PIXELS[name]:
                y = top + dy
                if WINDOW_TOP <= y <= WINDOW_BOTTOM:
                    canvas.SetPixel(left + dx, y, *color)
