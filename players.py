# Player profiles for the games: each has a name, a color, chips and stats, saved in a small JSON
# file. Blackjack and the slot machine spend and pay chips through here; the leaderboard ranks
# everyone by net worth (chips minus what they've rebought).
import json
import os
import re
import secrets
import threading

import frame

START_CHIPS = 1000
MIN_BET = 10                 # below this a player can rebuy
MAX_PLAYERS = 12
MAX_NAME = 12
COLORS = [(43, 224, 227), (255, 90, 200), (120, 230, 60), (255, 150, 30), (255, 230, 60), (255, 70, 70),
          (90, 140, 255), (255, 160, 190), (40, 200, 150), (180, 110, 255), (235, 235, 235), (200, 160, 80)]
STATS = ["rebuys", "biggest_win", "bj_hands", "bj_wins", "slot_spins", "slot_jackpots", "hm_wins", "hm_losses",
         "bets_won", "bets_lost", "pool_wins", "pool_losses"]


class Players:
    def __init__(self, path, legacy_chips_path=None):
        self.path = path
        self.lock = threading.RLock()
        self.players = {}           # id -> {"name", "color", "chips", stats...}
        self.order = []             # ids in the order they were made
        self.carry_over = None      # chips from before profiles, for the first player made
        self._load(legacy_chips_path)

    # ---- profiles ----
    def create(self, name):
        # Returns (id, None) or (None, error message)
        name = " ".join(name.split()) if isinstance(name, str) else ""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 '\-]{0,%d}" % (MAX_NAME - 1), name):
            return None, f"Names are 1 to {MAX_NAME} letters, numbers, spaces, ' or -"
        with self.lock:
            if len(self.players) >= MAX_PLAYERS:
                return None, f"There's room for {MAX_PLAYERS} players; remove one first"
            if any(p["name"].lower() == name.lower() for p in self.players.values()):
                return None, f"There's already a player called {name}"
            player_id = secrets.token_hex(4)
            used = {tuple(p["color"]) for p in self.players.values()}
            color = next((c for c in COLORS if c not in used), COLORS[len(self.players) % len(COLORS)])
            chips = START_CHIPS
            if self.carry_over is not None:
                # The first profile keeps the chips that were there before profiles existed
                chips, self.carry_over = self.carry_over, None
            self.players[player_id] = {"name": name, "color": list(color), "chips": chips, **{s: 0 for s in STATS}}
            self.order.append(player_id)
            self._save()
            return player_id, None

    def delete(self, player_id):
        with self.lock:
            if player_id not in self.players:
                return "No player with that id"
            del self.players[player_id]
            self.order.remove(player_id)
            self._save()
            return None

    def exists(self, player_id):
        return isinstance(player_id, str) and player_id in self.players

    def name(self, player_id):
        player = self.players.get(player_id)
        return player["name"] if player else ""

    def color(self, player_id):
        player = self.players.get(player_id)
        return tuple(player["color"]) if player else (235, 235, 235)

    def public(self):
        # Everyone with their stats, best net worth first
        with self.lock:
            rows = [{"id": pid, **self.players[pid], "net": self._net(pid)} for pid in self.order]
        return sorted(rows, key=lambda r: -r["net"])

    # ---- chips ----
    def chips(self, player_id):
        player = self.players.get(player_id)
        return player["chips"] if player else 0

    def spend(self, player_id, amount):
        # Takes the chips if the player has enough; returns whether it did
        with self.lock:
            player = self.players.get(player_id)
            if player is None or player["chips"] < amount:
                return False
            player["chips"] -= amount
            self._save()
            return True

    def pay(self, player_id, amount):
        with self.lock:
            if player_id in self.players:
                self.players[player_id]["chips"] += amount
                self._save()

    def rebuy(self, player_id):
        with self.lock:
            player = self.players.get(player_id)
            if player is None:
                return "Pick a player first"
            if player["chips"] >= MIN_BET:
                return "You still have chips"
            player["chips"] = START_CHIPS
            player["rebuys"] += 1
            self._save()
            return None

    def record(self, player_id, win=0, **counts):
        # Add to a player's stats, e.g. record(pid, win=150, bj_hands=1, bj_wins=1)
        with self.lock:
            player = self.players.get(player_id)
            if player is None:
                return
            for key, amount in counts.items():
                player[key] += amount
            player["biggest_win"] = max(player["biggest_win"], win)
            self._save()

    def _net(self, player_id):
        player = self.players[player_id]
        return player["chips"] - player["rebuys"] * START_CHIPS

    # ---- saving ----
    def _load(self, legacy_chips_path):
        try:
            with open(self.path) as f:
                saved = json.load(f)
            for player_id, player in saved.get("players", {}).items():
                if re.fullmatch(r"[0-9a-f]{8}", player_id) and isinstance(player, dict):
                    clean = {"name": str(player.get("name", "?"))[:MAX_NAME],
                             "color": list(player.get("color", COLORS[0]))[:3],
                             "chips": max(0, int(player.get("chips", START_CHIPS)))}
                    clean.update({s: max(0, int(player.get(s, 0))) for s in STATS})
                    self.players[player_id] = clean
                    self.order.append(player_id)
            carry = saved.get("carry_over")
            self.carry_over = carry if isinstance(carry, int) and carry >= 0 else None
            return
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Couldn't read {self.path}, starting with no players: {e}")
            return
        # First run with profiles: hold on to the chips from the old single blackjack wallet
        if legacy_chips_path:
            try:
                with open(legacy_chips_path) as f:
                    chips = json.load(f).get("chips")
                if isinstance(chips, int) and chips >= 0:
                    self.carry_over = chips
            except Exception:
                pass

    def _save(self):
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump({"players": {pid: self.players[pid] for pid in self.order},
                           "carry_over": self.carry_over}, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            print(f"Couldn't save players: {e}")

    # ---- the leaderboard on the sign ----
    def draw_leaderboard(self, canvas, fonts):
        text, tiny = fonts["text"], fonts["tiny"]
        canvas.Clear()
        title = "LEADERBOARD"
        frame.DrawText(canvas, tiny, 32 - sum(tiny.CharacterWidth(ord(c)) for c in title) // 2, 6,
                       frame.Color(255, 190, 0), title)
        rows = self.public()[:3]
        if not rows:
            message = "NO PLAYERS"
            frame.DrawText(canvas, text, 32 - sum(text.CharacterWidth(ord(c)) for c in message) // 2, 20,
                           frame.Color(130, 130, 130), message)
            return
        medals = [(255, 200, 0), (200, 205, 215), (205, 125, 60)]
        for i, row in enumerate(rows):
            baseline = 15 + i * 8
            # Medal, name in the player's color, net worth on the right
            for dx in range(3):
                for dy in range(3):
                    canvas.SetPixel(1 + dx, baseline - 4 + dy, *medals[i])
            net = f"{row['net']}"
            width = sum(text.CharacterWidth(ord(c)) for c in net)
            room = (64 - width - 6 - 2) // text.CharacterWidth(ord("M"))  # name fits between medal and number
            frame.DrawText(canvas, text, 6, baseline, frame.Color(*row["color"]), row["name"].upper()[:room])
            frame.DrawText(canvas, text, 64 - width, baseline, frame.Color(235, 235, 235), net)
