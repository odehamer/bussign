# NFL betting with the game chips: pick a winner of this week's or next week's games at the
# moneyline odds ESPN shows (DraftKings), locked in when the bet is placed. Bets close at kickoff,
# settle on their own once the game is final, and a tie gives the stake back. Bets are saved in a
# small JSON file; chips and stats live in the player profiles (players.py).
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
AMOUNTS = [10, 25, 50, 100, 250]
POLL_SECONDS = 600           # schedule and odds refresh
LIVE_POLL_SECONDS = 120      # while any game is on, so scores and settling keep up
KEEP_SETTLED = 300           # settled bets kept for the history


def parse_odds(text):
    # "-155" -> -155, "+154" -> 154, "EVEN" -> 100; "OFF" or missing -> None
    text = (text or "").strip().upper()
    if text in ("EVEN", "EV", "PK"):
        return 100
    try:
        value = int(text)
    except ValueError:
        return None
    return value if abs(value) >= 100 else None


def profit(amount, odds):
    # What a winning bet earns on top of the stake, at American odds
    return amount * odds // 100 if odds > 0 else amount * 100 // -odds


def parse_games(data, week_label):
    games = {}
    for event in data.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in competition.get("competitors", [])}
        if "home" not in sides or "away" not in sides or not event.get("id"):
            continue
        status = event.get("status", {}).get("type", {})
        moneyline = ((competition.get("odds") or [{}])[0] or {}).get("moneyline") or {}
        game = {"id": str(event["id"]), "week": week_label, "kickoff": event.get("date") or "",
                "state": status.get("state"), "status": status.get("shortDetail") or "",
                "completed": bool(status.get("completed"))}
        for side in ("away", "home"):
            competitor = sides[side]
            line = moneyline.get(side) or {}
            odds = parse_odds((line.get("close") or {}).get("odds")) or parse_odds((line.get("open") or {}).get("odds"))
            game[side] = {"abbr": (competitor.get("team") or {}).get("abbreviation", "?"),
                          "name": (competitor.get("team") or {}).get("shortDisplayName", ""),
                          "score": competitor.get("score", ""), "winner": bool(competitor.get("winner")),
                          "odds": odds}
        game["label"] = f"{game['away']['abbr']} @ {game['home']['abbr']}"
        games[game["id"]] = game
    return games


class Sportsbook:
    def __init__(self, path, players):
        self.path = path
        self.players = players
        self.lock = threading.RLock()
        self.games = {}             # game id -> game, this week and next
        self.week_order = []        # week labels in order
        self.fetched_at = 0.0
        self.bets = []
        self.next_id = 1
        self._load()
        threading.Thread(target=self._loop, name="sportsbook", daemon=True).start()

    # ---- actions from the control page; each returns an error message or None ----
    def act(self, action, data, player):
        with self.lock:
            if not self.players.exists(player):
                return "Pick a player first"
            if action == "place":
                return self._place(player, str(data.get("game", "")), data.get("team"), data.get("amount"))
            if action == "cancel":
                return self._cancel(player, data.get("bet"))
            return "Unknown action"

    def _place(self, player, game_id, team, amount):
        game = self.games.get(game_id)
        if game is None:
            return "That game isn't on the board"
        if not self._open_for_betting(game):
            return "Betting on that game has closed"
        side = next((s for s in ("away", "home") if game[s]["abbr"] == team), None)
        if side is None:
            return "Pick one of the two teams"
        if type(amount) is not int or amount not in AMOUNTS:
            return "Pick one of the bet sizes"
        if any(b["player"] == player and b["game"] == game_id and b["status"] == "open" for b in self.bets):
            return "You already have a bet on this game; cancel it to change it"
        if not self.players.spend(player, amount):
            return "Not enough chips for that bet" if self.players.chips(player) >= AMOUNTS[0] else "Out of chips"
        odds = game[side]["odds"] or 100  # even money when ESPN has no line
        other = "home" if side == "away" else "away"
        self.bets.append({"id": self.next_id, "player": player, "game": game_id, "team": team,
                          "opponent": game[other]["abbr"], "label": game["label"], "kickoff": game["kickoff"],
                          "amount": amount, "odds": odds, "status": "open", "payout": 0, "placed": time.time()})
        self.next_id += 1
        self._save()
        return None

    def _cancel(self, player, bet_id):
        bet = next((b for b in self.bets if b["id"] == bet_id and b["player"] == player), None)
        if bet is None or bet["status"] != "open":
            return "No open bet with that id"
        game = self.games.get(bet["game"])
        if game is None or not self._open_for_betting(game):
            return "That game has started; the bet stands"
        self.players.pay(player, bet["amount"])
        bet["status"] = "cancelled"
        self.bets.remove(bet)
        self._save()
        return None

    def _open_for_betting(self, game):
        if game["state"] != "pre" or not game["kickoff"]:
            return False
        kickoff = datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < kickoff

    # ---- what the control page shows ----
    def state(self, viewer):
        with self.lock:
            mine = [b for b in self.bets if b["player"] == viewer]
            open_by_game = {b["game"]: b for b in mine if b["status"] == "open"}
            weeks = []
            for label in self.week_order:
                games = sorted((g for g in self.games.values() if g["week"] == label), key=lambda g: g["kickoff"])
                weeks.append({"label": label, "games": [
                    {**g, "open": self._open_for_betting(g), "my_bet": self._public_bet(open_by_game.get(g["id"]))}
                    for g in games]})
            history = sorted((b for b in mine if b["status"] != "open"), key=lambda b: -b.get("settled", 0))[:15]
            return {"weeks": weeks, "amounts": AMOUNTS, "chips": self.players.chips(viewer),
                    "open_bets": [self._public_bet(b) for b in mine if b["status"] == "open"],
                    "history": [self._public_bet(b) for b in history],
                    "updated": round(time.time() - self.fetched_at) if self.fetched_at else None}

    def _public_bet(self, bet):
        if bet is None:
            return None
        return {k: bet[k] for k in ("id", "team", "opponent", "label", "kickoff", "amount", "odds", "status", "payout")} | {
            "to_win": profit(bet["amount"], bet["odds"])}

    # ---- schedule, odds and settling ----
    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                print(f"Sportsbook: couldn't update the games: {e}")
            with self.lock:
                live = any(g["state"] == "in" for g in self.games.values())
            time.sleep(LIVE_POLL_SECONDS if live else POLL_SECONDS)

    def refresh(self):
        # This week (ESPN's default scoreboard) and the next one, so bets can go in early
        current = requests.get(SCOREBOARD_URL, timeout=10)
        current.raise_for_status()
        data = current.json()
        week = (data.get("week") or {}).get("number")
        season_type = (data.get("season") or {}).get("type")
        label = f"Week {week}" if week else "This week"
        games = parse_games(data, label)
        order = [label]
        if week and season_type:
            upcoming = requests.get(SCOREBOARD_URL, params={"week": week + 1, "seasontype": season_type}, timeout=10)
            if upcoming.ok:
                next_games = parse_games(upcoming.json(), f"Week {week + 1}")
                if next_games:
                    games.update({gid: g for gid, g in next_games.items() if gid not in games})
                    order.append(f"Week {week + 1}")
        with self.lock:
            self.games, self.week_order, self.fetched_at = games, order, time.time()
        self._settle_missing()
        self.settle()

    def _settle_missing(self):
        # Open bets on games that have dropped off the board (the week rolled over): look them up by date
        with self.lock:
            missing = {b["game"]: b["kickoff"] for b in self.bets if b["status"] == "open" and b["game"] not in self.games}
        for game_id, kickoff in missing.items():
            try:
                start = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            except ValueError:
                continue
            for day in {start.strftime("%Y%m%d"), (start - timedelta(hours=8)).strftime("%Y%m%d")}:
                response = requests.get(SCOREBOARD_URL, params={"dates": day}, timeout=10)
                if response.ok:
                    found = parse_games(response.json(), "Earlier")
                    if game_id in found:
                        with self.lock:
                            self.games.setdefault(game_id, found[game_id])
                        break

    def settle(self):
        # Pay out bets on finished games: stake plus winnings, the stake back on a tie
        with self.lock:
            changed = False
            for bet in self.bets:
                game = self.games.get(bet["game"])
                if bet["status"] != "open" or game is None or game["state"] != "post" or not game["completed"]:
                    continue
                side = "away" if game["away"]["abbr"] == bet["team"] else "home"
                other = "home" if side == "away" else "away"
                if game[side]["winner"]:
                    winnings = profit(bet["amount"], bet["odds"])
                    bet["status"], bet["payout"] = "won", bet["amount"] + winnings
                    self.players.pay(bet["player"], bet["payout"])
                    self.players.record(bet["player"], win=winnings, bets_won=1)
                elif game[other]["winner"]:
                    bet["status"], bet["payout"] = "lost", 0
                    self.players.record(bet["player"], bets_lost=1)
                else:
                    bet["status"], bet["payout"] = "push", bet["amount"]
                    self.players.pay(bet["player"], bet["amount"])
                bet["settled"] = time.time()
                print(f"Bet {bet['id']} on {bet['team']} ({bet['label']}): {bet['status']}")
                changed = True
            if changed:
                settled = [b for b in self.bets if b["status"] != "open"]
                if len(settled) > KEEP_SETTLED:
                    drop = {id(b) for b in sorted(settled, key=lambda b: b.get("settled", 0))[:-KEEP_SETTLED]}
                    self.bets = [b for b in self.bets if id(b) not in drop]
                self._save()

    # ---- saving ----
    def _load(self):
        try:
            with open(self.path) as f:
                saved = json.load(f)
            self.bets = [b for b in saved.get("bets", []) if isinstance(b, dict) and "id" in b]
            self.next_id = max([saved.get("next_id", 1)] + [b["id"] + 1 for b in self.bets])
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Couldn't read {self.path}, starting with no bets: {e}")

    def _save(self):
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump({"bets": self.bets, "next_id": self.next_id}, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            print(f"Couldn't save bets: {e}")
