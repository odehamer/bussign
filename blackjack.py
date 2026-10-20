# Blackjack mode: one player against the dealer, played from the control page and drawn on the sign.
# Six-deck shoe, dealer stands on all 17s, blackjack pays 3:2, double down on the first two cards.
# Chips and the last bet are kept in a small JSON file so they survive restarts; the slot machine
# plays with the same chips through spend(), pay() and rebuy().
import json
import os
import random
import threading
import time

import frame

START_CHIPS = 1000
BETS = [10, 25, 50, 100, 250]
DECKS = 6
RESHUFFLE_AT = 52            # cards left in the shoe before a fresh shuffle
DEAL_STAGGER = 0.25          # seconds between cards in the opening deal
DEALER_PAUSE = 0.8           # seconds between dealer cards
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["hearts", "diamonds", "clubs", "spades"]


def card_value(rank):
    if rank == "A":
        return 11
    return 10 if rank in ("10", "J", "Q", "K") else int(rank)


def hand_total(cards):
    # Best total, and whether an ace is still counting as 11
    total = sum(card_value(rank) for rank, _ in cards)
    aces = sum(1 for rank, _ in cards if rank == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_blackjack(cards):
    return len(cards) == 2 and hand_total(cards)[0] == 21


class Blackjack:
    def __init__(self, path, fonts, on_play=None):
        self.path = path
        self.fonts = fonts          # {"text": 5x7 font, "tiny": 4x6 font}
        self.on_play = on_play      # called after any action, to put the game on the sign
        self.lock = threading.RLock()
        self.rng = random.Random()
        self.chips, self.bet = START_CHIPS, 50
        self._load()
        self.shoe = []
        self.player, self.dealer = [], []   # [(rank, suit, time it appears)]
        self.phase = "bet"                  # bet -> player -> dealer -> done
        self.hand_bet = 0
        self.result, self.payout = "", 0    # e.g. "WIN", +50
        self.hole_shown = False
        self.next_dealer_step = 0.0
        self.result_at = 0.0

    # ---- actions from the control page; each returns an error message or None ----
    def act(self, action, amount=None):
        with self.lock:
            now = time.monotonic()
            self.tick(now)
            if action == "bet":
                if self.phase not in ("bet", "done"):
                    return "Finish this hand first"
                if type(amount) is not int or amount not in BETS:
                    return "Pick one of the bet sizes"
                self.bet = amount
                self._save()
            elif action == "deal":
                error = self._deal(now)
                if error:
                    return error
            elif action == "hit":
                if self.phase != "player":
                    return "You can't hit right now"
                self.player.append(self._draw(now))
                total = hand_total(self._cards(self.player))[0]
                if total > 21:
                    self._finish(now, reveal=True)
                elif total == 21:
                    self._stand(now)
            elif action == "stand":
                if self.phase != "player":
                    return "You can't stand right now"
                self._stand(now)
            elif action == "double":
                if self.phase != "player" or len(self.player) != 2:
                    return "You can only double on your first two cards"
                if self.chips < self.hand_bet:
                    return "Not enough chips to double"
                self.chips -= self.hand_bet
                self.hand_bet *= 2
                self.player.append(self._draw(now))
                if hand_total(self._cards(self.player))[0] > 21:
                    self._finish(now, reveal=True)
                else:
                    self._stand(now)
            elif action == "rebuy":
                if self.phase == "player" or self.phase == "dealer":
                    return "Finish this hand first"
                error = self.rebuy()
                if error:
                    return error
                self.phase, self.player, self.dealer, self.result = "bet", [], [], ""
            else:
                return "Unknown action"
        if self.on_play:
            self.on_play()
        return None

    def state(self):
        # Everything the control page shows
        with self.lock:
            now = time.monotonic()
            self.tick(now)
            player = self._cards(self.player)
            dealer = self._cards(self.dealer)
            shown_dealer = dealer if self.hole_shown else dealer[:1]
            return {
                "phase": self.phase,
                "chips": self.chips,
                "bet": self.bet,
                "bets": BETS,
                "hand_bet": self.hand_bet,
                "player": [f"{r}{s[0].upper()}" for r, s in player],
                "dealer": [f"{r}{s[0].upper()}" for r, s in shown_dealer] + ([] if self.hole_shown or not dealer else ["?"]),
                "player_total": hand_total(player)[0] if player else None,
                "dealer_total": hand_total(shown_dealer)[0] if shown_dealer else None,
                "result": self.result,
                "payout": self.payout,
                "can_double": self.phase == "player" and len(self.player) == 2 and self.chips >= self.hand_bet,
                "can_rebuy": self.phase in ("bet", "done") and self.chips < BETS[0],
            }

    # ---- the chips, shared with the slot machine ----
    def spend(self, amount):
        # Takes the chips if there are enough; returns whether it did
        with self.lock:
            if self.chips < amount:
                return False
            self.chips -= amount
            self._save()
            return True

    def pay(self, amount):
        with self.lock:
            self.chips += amount
            self._save()

    def rebuy(self):
        with self.lock:
            if self.chips >= BETS[0]:
                return "You still have chips"
            self.chips = START_CHIPS
            self._save()
            return None

    # ---- game flow ----
    def _deal(self, now):
        if self.phase in ("player", "dealer"):
            return "Finish this hand first"
        if self.chips < self.bet:
            return "Not enough chips for that bet" if self.chips >= BETS[0] else "Out of chips. Tap Rebuy"
        if len(self.shoe) < RESHUFFLE_AT:
            self.shoe = [(rank, suit) for _ in range(DECKS) for suit in SUITS for rank in RANKS]
            self.rng.shuffle(self.shoe)
        self.chips -= self.bet
        self.hand_bet = self.bet
        self.result, self.payout, self.hole_shown = "", 0, False
        self.player, self.dealer = [], []
        for i in range(4):
            (self.player if i % 2 == 0 else self.dealer).append(self._draw(now + i * DEAL_STAGGER))
        self.phase = "player"
        # A blackjack on either side ends the hand right away (the dealer checks for one)
        if is_blackjack(self._cards(self.player)) or is_blackjack(self._cards(self.dealer)):
            self._finish(now + 4 * DEAL_STAGGER, reveal=True)
        self._save()
        return None

    def _stand(self, now):
        self.phase = "dealer"
        self.next_dealer_step = max(now, self._last_card_time()) + DEALER_PAUSE

    def tick(self, now):
        # The dealer flips the hole card, then draws to 17, one card at a time
        with self.lock:
            while self.phase == "dealer" and now >= self.next_dealer_step:
                step_time = self.next_dealer_step
                if not self.hole_shown:
                    self.hole_shown = True
                elif hand_total(self._cards(self.dealer))[0] < 17:
                    self.dealer.append(self._draw(step_time))
                else:
                    self._finish(step_time, reveal=True)
                    break
                self.next_dealer_step = step_time + DEALER_PAUSE

    def _finish(self, now, reveal):
        self.hole_shown = self.hole_shown or reveal
        player, dealer = self._cards(self.player), self._cards(self.dealer)
        mine, theirs = hand_total(player)[0], hand_total(dealer)[0]
        bet = self.hand_bet
        if mine > 21:
            self.result, back = "BUST", 0
        elif is_blackjack(player) and not is_blackjack(dealer):
            self.result, back = "BLACKJACK", bet + bet * 3 // 2
        elif is_blackjack(dealer) and not is_blackjack(player):
            self.result, back = "LOSE", 0
        elif theirs > 21 or mine > theirs:
            self.result, back = "WIN", bet * 2
        elif mine == theirs:
            self.result, back = "PUSH", bet
        else:
            self.result, back = "LOSE", 0
        self.chips += back
        self.payout = back - bet
        self.phase = "done"
        self.result_at = now
        self._save()

    def _draw(self, at):
        rank, suit = self.shoe.pop()
        return (rank, suit, at)

    def _cards(self, hand):
        return [(rank, suit) for rank, suit, _ in hand]

    def _last_card_time(self):
        return max([at for *_, at in self.player + self.dealer] or [0.0])

    # ---- saved chips ----
    def _load(self):
        try:
            with open(self.path) as f:
                saved = json.load(f)
            chips, bet = saved.get("chips"), saved.get("bet")
            if isinstance(chips, int) and chips >= 0:
                self.chips = chips
            if bet in BETS:
                self.bet = bet
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Couldn't read {self.path}, starting fresh: {e}")

    def _save(self):
        # Chips already in play count as spent, so quitting mid-hand loses that bet (like a real table)
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump({"chips": self.chips, "bet": self.bet}, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            print(f"Couldn't save blackjack chips: {e}")

    # ---- drawing on the sign ----
    def draw(self, canvas, t):
        with self.lock:
            self.tick(t)
            canvas.Clear()
            if self.phase == "bet" and not self.player:
                self._draw_table(canvas)
                return
            dealer = [c for c in self.dealer if c[2] <= t]
            player = [c for c in self.player if c[2] <= t]
            draw_hand(canvas, dealer, 1, hide_second=not self.hole_shown)
            draw_hand(canvas, player, 18)
            # Right side: dealer total, result or bet, player total
            text, tiny = self.fonts["text"], self.fonts["tiny"]
            if dealer:
                shown = self._cards(dealer if self.hole_shown else dealer[:1])
                draw_centered(canvas, text, 55, 8, WHITE, str(hand_total(shown)[0]))
            if player:
                draw_centered(canvas, text, 55, 30, WHITE, str(hand_total(self._cards(player))[0]))
            if self.phase == "done" and t >= self.result_at:
                word = {"BLACKJACK": "BJ!"}.get(self.result, self.result)
                color = RESULT_COLORS.get(self.result, WHITE)
                draw_centered(canvas, tiny, 55, 18, color, word)
            else:
                draw_centered(canvas, tiny, 55, 18, GOLD, f"${self.hand_bet}")

    def _draw_table(self, canvas):
        text, tiny = self.fonts["text"], self.fonts["tiny"]
        draw_card(canvas, 2, 9, "A", "spades")
        draw_card(canvas, 53, 9, "K", "hearts")
        draw_centered(canvas, text, 32, 8, GOLD, "BLACKJACK")
        draw_centered(canvas, text, 32, 19, WHITE, f"${self.chips}")
        draw_centered(canvas, tiny, 32, 28, CYAN, f"BET ${self.bet}")


# ---------------- card pixel art ----------------
WHITE = frame.Color(255, 255, 255)
GOLD = frame.Color(255, 190, 0)
CYAN = frame.Color(43, 224, 227)
RESULT_COLORS = {"WIN": frame.Color(60, 220, 90), "BLACKJACK": GOLD, "PUSH": frame.Color(230, 230, 120),
                 "LOSE": frame.Color(255, 70, 70), "BUST": frame.Color(255, 70, 70)}
CARD_W, CARD_H = 9, 13
HAND_WIDTH = 44              # columns for a hand; the right side holds the totals
CARD_FACE = (235, 235, 225)
CARD_EDGE = (120, 120, 120)
SUIT_COLORS = {"hearts": (215, 25, 35), "diamonds": (215, 25, 35), "clubs": (15, 15, 20), "spades": (15, 15, 20)}
RANK_GLYPHS = {
    "A": [".#.", "#.#", "###", "#.#", "#.#"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    "10": [".#.###", "##.#.#", ".#.#.#", ".#.#.#", ".#.###"],
    "J": ["..#", "..#", "..#", "#.#", ".#."],
    "Q": [".#.", "#.#", "#.#", "##.", ".##"],
    "K": ["#.#", "##.", "#..", "##.", "#.#"],
}
SUIT_GLYPHS = {
    "hearts": [".#.#.", "#####", "#####", ".###.", "..#.."],
    "diamonds": ["..#..", ".###.", "#####", ".###.", "..#.."],
    "clubs": ["..#..", ".###.", "#.#.#", "#####", "..#.."],
    "spades": ["..#..", ".###.", "#####", "#####", ".#.#."],
}


def draw_glyph(canvas, x, y, rows, color):
    for dy, row in enumerate(rows):
        for dx, key in enumerate(row):
            if key == "#":
                canvas.SetPixel(x + dx, y + dy, *color)


def draw_card(canvas, x, y, rank, suit, face_down=False):
    # 9x13 card with a gray edge and clipped corners
    for dy in range(CARD_H):
        for dx in range(CARD_W):
            corner = dx in (0, CARD_W - 1) and dy in (0, CARD_H - 1)
            if corner:
                continue
            edge = dx in (0, CARD_W - 1) or dy in (0, CARD_H - 1)
            if face_down:
                color = (90, 110, 200) if edge else ((40, 70, 190) if (dx + dy) % 2 else (25, 45, 140))
            else:
                color = CARD_EDGE if edge else CARD_FACE
            canvas.SetPixel(x + dx, y + dy, *color)
    if not face_down:
        # "10" is twice as wide, so it starts a column further left
        draw_glyph(canvas, x + (1 if rank == "10" else 2), y + 2, RANK_GLYPHS[rank], SUIT_COLORS[suit])
        draw_glyph(canvas, x + 2, y + 7, SUIT_GLYPHS[suit], SUIT_COLORS[suit])


def draw_hand(canvas, hand, y, hide_second=False):
    # Cards overlap more as the hand grows so it always fits in HAND_WIDTH
    if not hand:
        return
    step = 7 if len(hand) < 2 else min(7, (HAND_WIDTH - CARD_W) // (len(hand) - 1))
    for i, (rank, suit, _) in enumerate(hand):
        draw_card(canvas, 1 + i * step, y, rank, suit, face_down=hide_second and i == 1)


def draw_centered(canvas, font, center_x, y, color, text):
    width = sum(font.CharacterWidth(ord(c)) for c in text)
    frame.DrawText(canvas, font, center_x - width // 2, y, color, text)
