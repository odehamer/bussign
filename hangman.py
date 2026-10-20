# Hangman mode: one person types a secret word on their phone (or picks a random one), the other
# guesses letters from theirs. The secret never leaves the sign until the game is over. Solving
# is a win for everyone who guessed and a loss for the word-setter, and the other way round.
import random
import re
import threading

import frame

MAX_MISSES = 6
MIN_LETTERS, MAX_LETTERS = 3, 10
RANDOM_WORDS = """
ATLANTA FALCONS TOUCHDOWN FIELDGOAL KICKOFF HUDDLE BLITZ SAFETY PUNTER HELMET
QUARTERBACK RECEIVER TACKLE FUMBLE ENDZONE SIDELINE PLAYOFFS STADIUM REFEREE
PENALTY INTERCEPT GRIDIRON SCRIMMAGE TAILGATE OVERTIME HALFTIME BUS TICKET
SUBWAY TROLLEY STATION SCHEDULE WEATHER THUNDER RAINBOW SUNSHINE BLIZZARD
TORNADO DRIZZLE HURRICANE AQUARIUM FIREPLACE PENGUIN GIRAFFE ELEPHANT DOLPHIN
OCTOPUS KANGAROO PELICAN TORTOISE CHEETAH GORILLA HAMSTER LOBSTER JELLYFISH
PIZZA BURRITO PANCAKE WAFFLE PRETZEL AVOCADO NOODLES SANDWICH POPCORN CUPCAKE
BROCCOLI PINEAPPLE BLUEBERRY CINNAMON PICKLE MUSHROOM SPAGHETTI DUMPLING
GUITAR TRUMPET PIANO DRUMMER VIOLIN SAXOPHONE CONCERT PLAYLIST SPEAKER VINYL
KEYBOARD LAPTOP PRINTER ROUTER BATTERY CHARGER MONITOR JOYSTICK PIXEL ROBOT
ROCKET PLANET GALAXY COMET ASTEROID METEOR SATELLITE TELESCOPE GRAVITY ORBIT
CASTLE DRAGON WIZARD KNIGHT PIRATE TREASURE COMPASS LANTERN VOLCANO GLACIER
JUNGLE DESERT CANYON MEADOW ISLAND LIGHTHOUSE WATERFALL MOUNTAIN VALLEY
BICYCLE SKATEBOARD SCOOTER HELICOPTER SUBMARINE TRACTOR AIRPLANE SAILBOAT
BLACKJACK HANGMAN PUZZLE CHECKERS DOMINO RIDDLE MYSTERY SECRET BIRTHDAY
""".split()


class Hangman:
    def __init__(self, players, fonts, on_play=None):
        self.players = players      # player profiles, for the win/loss record
        self.fonts = fonts          # {"text": 5x7 font, "tiny": 4x6 font}
        self.on_play = on_play      # called after any action, to put the game on the sign
        self.lock = threading.Lock()
        self.rng = random.Random()
        self.word = ""
        self.guessed = []           # letters in the order they were guessed
        self.phase = "setup"        # setup -> playing -> won / lost
        self.setter = None          # who typed the word (None for a random word)
        self.guessers = set()       # who has guessed a letter this game

    # ---- actions from the control page; each returns an error message or None ----
    def act(self, action, value=None, player=None):
        with self.lock:
            if not self.players.exists(player):
                return "Pick a player first"
            if action == "start":
                word = value.strip().upper() if isinstance(value, str) else ""
                if not re.fullmatch(r"[A-Z]{%d,%d}" % (MIN_LETTERS, MAX_LETTERS), word):
                    return f"Use one word of {MIN_LETTERS} to {MAX_LETTERS} letters, A to Z only"
                self._start(word, setter=player)
            elif action == "random":
                self._start(self.rng.choice([w for w in RANDOM_WORDS if MIN_LETTERS <= len(w) <= MAX_LETTERS]))
            elif action == "guess":
                if self.phase != "playing":
                    return "Start a game first"
                letter = value.upper() if isinstance(value, str) else ""
                if not re.fullmatch(r"[A-Z]", letter):
                    return "Guess one letter, A to Z"
                if letter in self.guessed:
                    return f"{letter} was already guessed"
                self.guessed.append(letter)
                if player != self.setter:
                    self.guessers.add(player)
                if all(c in self.guessed for c in self.word):
                    self.phase = "won"
                elif len(self.misses()) >= MAX_MISSES:
                    self.phase = "lost"
                if self.phase in ("won", "lost"):
                    self._record(self.phase == "won")
            elif action == "reset":
                self.word, self.guessed, self.phase = "", [], "setup"
            else:
                return "Unknown action"
        if self.on_play:
            self.on_play()
        return None

    def _start(self, word, setter=None):
        self.word, self.guessed, self.phase = word, [], "playing"
        self.setter, self.guessers = setter, set()

    def _record(self, solved):
        for guesser in self.guessers:
            self.players.record(guesser, hm_wins=int(solved), hm_losses=int(not solved))
        if self.setter and self.guessers:
            self.players.record(self.setter, hm_wins=int(not solved), hm_losses=int(solved))

    def misses(self):
        return [c for c in self.guessed if c not in self.word]

    def state(self):
        # The word comes back with unguessed letters as null, and in full only once the game is over
        with self.lock:
            over = self.phase in ("won", "lost")
            return {
                "phase": self.phase,
                "letters": [c if over or c in self.guessed else None for c in self.word],
                "guessed": list(self.guessed),
                "misses": self.misses(),
                "max_misses": MAX_MISSES,
                "word": self.word if over else None,
                "setter": self.players.name(self.setter) if self.setter else "",
                "min_letters": MIN_LETTERS,
                "max_letters": MAX_LETTERS,
            }

    # ---- drawing on the sign ----
    def draw(self, canvas, t):
        with self.lock:
            canvas.Clear()
            text, tiny = self.fonts["text"], self.fonts["tiny"]
            if self.phase == "setup":
                draw_gallows(canvas, 0, WOOD)
                draw_centered(canvas, text, 42, 10, GOLD, "HANGMAN")
                draw_centered(canvas, tiny, 42, 21, GRAY, "WAITING")
                draw_centered(canvas, tiny, 42, 28, GRAY, "FOR A WORD")
                return
            misses = self.misses()
            figure = {"won": GREEN, "lost": RED}.get(self.phase, WHITE)
            draw_gallows(canvas, len(misses), figure)
            # Top: wrong letters in red
            draw_centered(canvas, tiny, 42, 6, RED_TEXT, "".join(misses))
            # Middle: the word, with blanks for letters not found yet (missed ones show in red at the end)
            font, slot = (text, 5) if len(self.word) <= 8 else (tiny, 4)
            left = 42 - len(self.word) * slot // 2
            for i, c in enumerate(self.word):
                x = left + i * slot
                if c in self.guessed:
                    frame.DrawText(canvas, font, x, 19, GREEN_TEXT if self.phase == "won" else WHITE_TEXT, c)
                elif self.phase == "lost":
                    frame.DrawText(canvas, font, x, 19, RED_TEXT, c)
                for dx in range(slot - 1):
                    canvas.SetPixel(x + dx, 21, 120, 120, 120)
            # Bottom: guesses left, or the result
            if self.phase == "won":
                draw_centered(canvas, tiny, 42, 30, GREEN_TEXT, "SOLVED!")
            elif self.phase == "lost":
                draw_centered(canvas, tiny, 42, 30, RED_TEXT, "GAME OVER")
            else:
                left_count = MAX_MISSES - len(misses)
                draw_centered(canvas, tiny, 42, 30, GRAY, f"{left_count} LEFT")


WOOD = (150, 95, 45)
WHITE = (235, 235, 235)
GREEN = (60, 220, 90)
RED = (255, 70, 70)
WHITE_TEXT = frame.Color(*WHITE)
GREEN_TEXT = frame.Color(*GREEN)
RED_TEXT = frame.Color(*RED)
GOLD = frame.Color(255, 190, 0)
GRAY = frame.Color(130, 130, 130)
HEAD = [(1, 0), (2, 0), (3, 0), (0, 1), (4, 1), (0, 2), (4, 2), (0, 3), (4, 3), (1, 4), (2, 4), (3, 4)]


def line(canvas, x0, y0, x1, y1, color):
    steps = max(abs(x1 - x0), abs(y1 - y0))
    for i in range(steps + 1):
        canvas.SetPixel(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps), *color)


def draw_gallows(canvas, misses, figure_color):
    # The gallows on the left, then one body part per miss: head, body, arms, legs
    line(canvas, 0, 31, 10, 31, WOOD)
    line(canvas, 2, 1, 2, 30, WOOD)
    line(canvas, 2, 1, 13, 1, WOOD)
    line(canvas, 3, 5, 6, 2, WOOD)  # brace
    line(canvas, 13, 2, 13, 4, (170, 150, 110))  # rope
    if misses >= 1:
        for dx, dy in HEAD:
            canvas.SetPixel(11 + dx, 5 + dy, *figure_color)
    parts = [(13, 10, 13, 17), (13, 12, 10, 14), (13, 12, 16, 14), (13, 17, 10, 22), (13, 17, 16, 22)]
    for x0, y0, x1, y1 in parts[:max(0, misses - 1)]:
        line(canvas, x0, y0, x1, y1, figure_color)


def draw_centered(canvas, font, center_x, y, color, text):
    width = sum(font.CharacterWidth(ord(c)) for c in text)
    frame.DrawText(canvas, font, center_x - width // 2, y, color, text)
