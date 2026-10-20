# 8-ball pool on the sign: the whole 64x32 screen is the table, with 2-pixel balls. Two players
# (or one, practicing against themselves) aim and shoot from their phones; the balls roll with
# simple physics here and the sign shows every frame.
#
# Rules, slightly simplified: the table is open after the break; the first player to legally pocket
# a ball after the break takes that group (solids 1-7 or stripes 9-15). Pocketing one of your own
# balls keeps your turn. Fouls (scratching, hitting nothing, or hitting the wrong group or the 8
# first) pass the turn with ball in hand. Sinking the 8 after clearing your group wins; sinking it
# early or scratching on it loses. An 8 sunk on the break is put back on its spot.
import math
import random
import threading
import time

from PIL import Image

import frame

R = 1.0                                   # ball radius, in pixels
X_MIN, X_MAX, Y_MIN, Y_MAX = 3.0, 61.0, 3.0, 29.0   # where ball centers can go
POCKETS = [(1.7, 1.7, 2.0), (32.0, 1.0, 2.1), (62.3, 1.7, 2.0),
           (1.7, 30.3, 2.0), (32.0, 31.0, 2.1), (62.3, 30.3, 2.0)]   # x, y, capture radius
HEAD_SPOT, FOOT_SPOT = (16.0, 16.0), (44.0, 16.0)
DT = 1 / 200                              # physics step, seconds
FRICTION = 16.0                           # px/s² of slowing down on the felt
CUSHION_BOUNCE = 0.8
BALL_BOUNCE = 0.95
MIN_SPEED, MAX_SPEED = 12.0, 150.0        # px/s for the weakest and hardest shot
SOLIDS, STRIPES = set(range(1, 8)), set(range(9, 16))
GROUP_NAMES = {"solids": "Solids", "stripes": "Stripes"}

FELT = (0, 95, 45)
CUSHION = (0, 60, 25)
RAIL = (110, 60, 25)
BALL_COLORS = {0: (255, 255, 255), 8: (15, 15, 15),
               1: (255, 210, 0), 2: (30, 80, 255), 3: (255, 30, 30), 4: (170, 40, 255),
               5: (255, 120, 0), 6: (120, 255, 120), 7: (200, 60, 90)}
for _n in range(9, 16):
    BALL_COLORS[_n] = BALL_COLORS[_n - 8]


def group_of(ball):
    return "solids" if ball in SOLIDS else "stripes" if ball in STRIPES else None


def make_table():
    # Rails, cushions, felt and pockets; drawn once
    image = Image.new("RGB", (64, 32), FELT)
    for x in range(64):
        for y in (0, 31):
            image.putpixel((x, y), RAIL)
        for y in (1, 30):
            image.putpixel((x, y), CUSHION)
    for y in range(32):
        for x in (0, 63):
            image.putpixel((x, y), RAIL)
        for x in (1, 62):
            if 0 < y < 31:
                image.putpixel((x, y), CUSHION)
    for x0, y0 in ((0, 0), (61, 0), (0, 29), (61, 29)):
        for dx in range(3):
            for dy in range(3):
                image.putpixel((x0 + dx, y0 + dy), (0, 0, 0))
    for x in (31, 32):
        for y in (0, 1, 30, 31):
            image.putpixel((x, y), (0, 0, 0))
    # Head and foot spots, barely visible
    image.putpixel((16, 16), (0, 120, 60))
    image.putpixel((44, 16), (0, 120, 60))
    return image


class Pool:
    def __init__(self, players, fonts, on_play=None):
        self.players = players      # player profiles: names, colors, stats
        self.fonts = fonts          # {"tiny": 4x6 font}
        self.on_play = on_play
        self.lock = threading.RLock()
        self.rng = random.Random()
        self.table = make_table()
        self.seats = []             # two player ids; the same id twice is practice
        self.phase = "none"         # none -> aim <-> rolling -> over
        self._rack()

    # ---- setting up ----
    def _rack(self):
        self.balls = {0: [HEAD_SPOT[0], HEAD_SPOT[1], 0.0, 0.0]}
        slots = []
        gap = 2 * R * 1.02
        for row in range(5):
            for i in range(row + 1):
                slots.append((FOOT_SPOT[0] + row * gap * math.sqrt(3) / 2, FOOT_SPOT[1] + (i - row / 2) * gap))
        # The 8 in the middle of the third row, a solid and a stripe in the back corners
        order = [None] * 15
        order[4] = 8
        solid, stripe = self.rng.choice(sorted(SOLIDS)), self.rng.choice(sorted(STRIPES))
        corners = [10, 14]
        self.rng.shuffle(corners)
        order[corners[0]], order[corners[1]] = solid, stripe
        rest = [b for b in range(1, 16) if b not in (8, solid, stripe)]
        self.rng.shuffle(rest)
        for i in range(15):
            if order[i] is None:
                order[i] = rest.pop()
        for ball, (x, y) in zip(order, slots):
            self.balls[ball] = [x + self.rng.uniform(-0.03, 0.03), y + self.rng.uniform(-0.03, 0.03), 0.0, 0.0]
        self.groups = [None, None]  # each seat's group once the table isn't open
        self.turn = 0
        self.break_shot = True
        self.ball_in_hand = False
        self.aim, self.power = 0.0, 0.7
        self.winner = None
        self.message, self.message_at = "", 0.0
        self.shot = None
        self.sim_time = 0.0

    # ---- actions from the control page; each returns an error message or None ----
    def act(self, action, data, player, now):
        with self.lock:
            self.tick(now)
            if not self.players.exists(player):
                return "Pick a player first"
            if action == "new":
                opponent = data.get("opponent")
                if opponent != player and not self.players.exists(opponent):
                    return "Pick who you're playing against"
                if self.phase in ("aim", "rolling") and player not in self.seats:
                    return "A game is going; only its players can start a new one"
                self._rack()
                self.seats = [player, opponent]
                self.phase = "aim"
                self.message = ""
            elif action in ("aim", "shoot", "place"):
                if self.phase != "aim":
                    return "Wait for the balls to stop" if self.phase == "rolling" else "Start a game first"
                if player != self.seats[self.turn]:
                    return f"It's {self.players.name(self.seats[self.turn])}'s turn"
                if action == "place":
                    return self._place_cue(data.get("x"), data.get("y"))
                angle, power = data.get("angle"), data.get("power")
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (angle, power)):
                    return "Aim needs an angle and a power"
                self.aim, self.power = angle % math.tau, min(1.0, max(0.0, float(power)))
                if action == "shoot":
                    self._shoot(now)
                else:
                    return None  # aiming is too frequent to put the game on the sign each time
            elif action == "forfeit":
                if self.phase not in ("aim", "rolling") or player not in self.seats:
                    return "You're not in a game"
                seat = self.seats.index(player)
                self._end(winner_seat=1 - seat, reason=f"{self.players.name(player).upper()} QUIT")
            else:
                return "Unknown action"
        if self.on_play:
            self.on_play()
        return None

    def _place_cue(self, x, y):
        if not self.ball_in_hand:
            return "You can only move the cue ball after a foul"
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)):
            return "Tap a spot on the table"
        x, y = min(X_MAX, max(X_MIN, float(x))), min(Y_MAX, max(Y_MIN, float(y)))
        if any(math.hypot(b[0] - x, b[1] - y) < 2 * R * 1.05 for n, b in self.balls.items() if n != 0):
            return "That spot is taken by another ball"
        self.balls[0][:2] = [x, y]
        return None

    def _shoot(self, now):
        speed = MIN_SPEED + (MAX_SPEED - MIN_SPEED) * self.power
        self.balls[0][2:] = [math.cos(self.aim) * speed, math.sin(self.aim) * speed]
        self.shot = {"first": None, "pocketed": [], "before": set(self.balls) - {0}}
        self.phase, self.ball_in_hand, self.sim_time = "rolling", False, now
        self.message = ""

    # ---- physics ----
    def tick(self, now):
        with self.lock:
            if self.phase != "rolling":
                return
            steps = min(int((now - self.sim_time) / DT), 6000)
            for _ in range(steps):
                if not self._step():
                    self._resolve(now)
                    return
            self.sim_time += steps * DT

    def _step(self):
        # One physics step; returns whether anything is still moving
        balls = self.balls
        for b in balls.values():
            b[0] += b[2] * DT
            b[1] += b[3] * DT
        # Cushions
        for b in balls.values():
            if b[0] < X_MIN:
                b[0], b[2] = 2 * X_MIN - b[0], -b[2] * CUSHION_BOUNCE
            elif b[0] > X_MAX:
                b[0], b[2] = 2 * X_MAX - b[0], -b[2] * CUSHION_BOUNCE
            if b[1] < Y_MIN:
                b[1], b[3] = 2 * Y_MIN - b[1], -b[3] * CUSHION_BOUNCE
            elif b[1] > Y_MAX:
                b[1], b[3] = 2 * Y_MAX - b[1], -b[3] * CUSHION_BOUNCE
        # Pockets
        for n in list(balls):
            x, y = balls[n][0], balls[n][1]
            if any((x - px) ** 2 + (y - py) ** 2 < pr * pr for px, py, pr in POCKETS):
                del balls[n]
                self.shot["pocketed"].append(n)
        # Collisions between balls (equal masses)
        numbers = list(balls)
        for i in range(len(numbers)):
            a = balls[numbers[i]]
            for j in range(i + 1, len(numbers)):
                b = balls[numbers[j]]
                dx, dy = b[0] - a[0], b[1] - a[1]
                d2 = dx * dx + dy * dy
                if d2 >= 4 * R * R or d2 == 0:
                    continue
                d = math.sqrt(d2)
                nx, ny = dx / d, dy / d
                push = (2 * R - d) / 2
                a[0] -= nx * push
                a[1] -= ny * push
                b[0] += nx * push
                b[1] += ny * push
                closing = (a[2] - b[2]) * nx + (a[3] - b[3]) * ny
                if closing > 0:
                    impulse = closing * (1 + BALL_BOUNCE) / 2
                    a[2] -= impulse * nx
                    a[3] -= impulse * ny
                    b[2] += impulse * nx
                    b[3] += impulse * ny
                if self.shot["first"] is None and 0 in (numbers[i], numbers[j]):
                    self.shot["first"] = numbers[j] if numbers[i] == 0 else numbers[i]
        # Friction
        moving = False
        for b in balls.values():
            speed = math.hypot(b[2], b[3])
            if speed < 0.8:
                b[2] = b[3] = 0.0
            else:
                scale = max(0.0, speed - FRICTION * DT) / speed
                b[2] *= scale
                b[3] *= scale
                moving = True
        return moving

    # ---- rules ----
    def _remaining(self, seat, on_table):
        group = self.groups[seat]
        wanted = SOLIDS if group == "solids" else STRIPES if group == "stripes" else SOLIDS | STRIPES
        return [b for b in on_table if b in wanted]

    def _resolve(self, now):
        shot = self.shot
        seat, other = self.turn, 1 - self.turn
        pocketed = [b for b in shot["pocketed"] if b != 0]
        scratch = 0 in shot["pocketed"]
        first = shot["first"]
        mine = self.groups[seat]
        on_eight = mine is not None and not self._remaining(seat, shot["before"] - {8})
        if first is None:
            foul = "NO HIT"
        elif scratch:
            foul = "SCRATCH"
        elif mine is None and first == 8 and not self.break_shot:
            foul = "FOUL"
        elif mine is not None and not (group_of(first) == mine or (first == 8 and on_eight)):
            foul = "FOUL"
        else:
            foul = None
        if 8 in pocketed:
            if self.break_shot:
                pocketed.remove(8)
                self.balls[8] = [*self._free_spot(FOOT_SPOT), 0.0, 0.0]
            else:
                won = foul is None and on_eight
                self._end(winner_seat=seat if won else other,
                          reason="" if won else ("SCRATCH ON 8" if scratch else "EARLY 8"), now=now)
                return
        if scratch or 0 not in self.balls:
            self.balls[0] = [*self._free_spot(HEAD_SPOT), 0.0, 0.0]
        # The first legal pocket after the break claims a group
        if mine is None and not self.break_shot and foul is None and pocketed:
            claimed = group_of(pocketed[0])
            self.groups[seat] = claimed
            self.groups[other] = "stripes" if claimed == "solids" else "solids"
            mine = claimed
            self._say(f"{GROUP_NAMES[claimed].upper()}!", now)
        keep_turn = foul is None and any(mine is None or group_of(b) == mine for b in pocketed)
        if foul:
            self._say(foul, now)
            self.ball_in_hand = True
        if not keep_turn:
            self.turn = other
        self.break_shot = False
        self.shot = None
        self.phase = "aim"
        # Aim the next shot at the nearest ball the shooter can hit
        self.aim = self._suggested_aim()

    def _end(self, winner_seat, reason, now=None):
        self.winner = self.seats[winner_seat]
        loser = self.seats[1 - winner_seat]
        self.phase = "over"
        self.shot = None
        self.message = reason
        if self.winner != loser:
            self.players.record(self.winner, pool_wins=1)
            self.players.record(loser, pool_losses=1)

    def _say(self, text, now):
        self.message, self.message_at = text, now

    def _free_spot(self, target):
        # The target, or the nearest free spot around it
        for radius in [0.0] + [r * 0.5 for r in range(1, 40)]:
            for k in range(max(1, int(radius * 8))):
                angle = k * math.tau / max(1, int(radius * 8))
                x = min(X_MAX, max(X_MIN, target[0] + math.cos(angle) * radius))
                y = min(Y_MAX, max(Y_MIN, target[1] + math.sin(angle) * radius))
                if all(math.hypot(b[0] - x, b[1] - y) >= 2 * R * 1.05 for n, b in self.balls.items() if n != 0):
                    return x, y
        return target

    def _suggested_aim(self):
        cue = self.balls.get(0)
        targets = self._remaining(self.turn, set(self.balls) - {0, 8}) or [8]
        targets = [t for t in targets if t in self.balls]
        if cue is None or not targets:
            return self.aim
        nearest = min(targets, key=lambda t: math.hypot(self.balls[t][0] - cue[0], self.balls[t][1] - cue[1]))
        return math.atan2(self.balls[nearest][1] - cue[1], self.balls[nearest][0] - cue[0]) % math.tau

    def aim_line(self):
        # Where the cue ball would go along the aim: (end x, end y, ghost ball or None)
        cue = self.balls[0]
        dx, dy = math.cos(self.aim), math.sin(self.aim)
        x, y = cue[0], cue[1]
        for _ in range(400):
            x += dx * 0.25
            y += dy * 0.25
            for n, b in self.balls.items():
                if n != 0 and (b[0] - x) ** 2 + (b[1] - y) ** 2 < 4 * R * R:
                    return x, y, (x, y)
            if not (X_MIN <= x <= X_MAX and Y_MIN <= y <= Y_MAX):
                return min(X_MAX, max(X_MIN, x)), min(Y_MAX, max(Y_MIN, y)), None
        return x, y, None

    # ---- what the control page shows ----
    def state(self, now, viewer=None):
        with self.lock:
            self.tick(now)
            seats = []
            for i, pid in enumerate(self.seats):
                seats.append({"id": pid, "name": self.players.name(pid), "color": list(self.players.color(pid)),
                              "group": self.groups[i],
                              "left": len(self._remaining(i, set(self.balls) - {0, 8})) if self.groups[i] else None})
            line = self.aim_line() if self.phase == "aim" and 0 in self.balls else None
            shooter = self.seats[self.turn] if self.seats else None
            return {
                "phase": self.phase,
                "balls": [[n, round(b[0], 2), round(b[1], 2)] for n, b in sorted(self.balls.items())],
                "seats": seats,
                "turn": self.turn,
                "my_turn": self.phase == "aim" and viewer is not None and viewer == shooter,
                "in_game": viewer in self.seats,
                "practice": len(self.seats) == 2 and self.seats[0] == self.seats[1],
                "ball_in_hand": self.ball_in_hand,
                "break_shot": self.break_shot,
                "aim": self.aim,
                "power": self.power,
                "aim_line": [round(v, 2) for v in line[:2]] if line else None,
                "ghost": [round(v, 2) for v in line[2]] if line and line[2] else None,
                "message": self.message,
                "winner": self.players.name(self.winner) if self.phase == "over" and self.winner else "",
                "bounds": [X_MIN, X_MAX, Y_MIN, Y_MAX],
            }

    # ---- drawing on the sign ----
    def draw(self, canvas, t):
        with self.lock:
            self.tick(t)
            canvas.SetImage(self.table, 0, 0)
            # The top rail shows whose turn it is
            if self.seats and self.phase in ("aim", "rolling"):
                color = self.players.color(self.seats[self.turn])
                for x in range(4, 60):
                    if x not in (30, 31, 32, 33):
                        canvas.SetPixel(x, 0, *color)
            if self.phase == "aim" and 0 in self.balls:
                self._draw_aim(canvas)
            for n, b in self.balls.items():
                left, top = int(math.floor(b[0] - R + 0.5)), int(math.floor(b[1] - R + 0.5))
                color = BALL_COLORS[n]
                for dx in range(2):
                    for dy in range(2):
                        canvas.SetPixel(left + dx, top + dy, *color)
                if n in STRIPES:
                    canvas.SetPixel(left, top, 255, 255, 255)
            if self.phase == "none":
                self._banner(canvas, ["8-BALL", "GAMES TAB"])
            elif self.phase == "over":
                lines = [f"{self.players.name(self.winner).upper()[:10]} WINS"]
                if self.message:
                    lines.append(self.message)
                self._banner(canvas, lines)
            elif self.message and t - self.message_at < 2.0:
                self._banner(canvas, [self.message])

    def _draw_aim(self, canvas):
        cue = self.balls[0]
        end_x, end_y, ghost = self.aim_line()
        steps = int(math.hypot(end_x - cue[0], end_y - cue[1]) / 0.5)
        for i in range(3, steps, 3):
            x = cue[0] + (end_x - cue[0]) * i / steps
            y = cue[1] + (end_y - cue[1]) * i / steps
            canvas.SetPixel(int(x), int(y), 150, 170, 150)
        if ghost:
            canvas.SetPixel(int(ghost[0]), int(ghost[1]), 210, 230, 210)
        # The cue stick behind the ball, pulled back further for a harder shot
        dx, dy = math.cos(self.aim), math.sin(self.aim)
        back = 2.5 + self.power * 4
        for i in range(9):
            x = cue[0] - dx * (back + i)
            y = cue[1] - dy * (back + i)
            if 0 <= x < 64 and 0 <= y < 32:
                canvas.SetPixel(int(x), int(y), *((230, 230, 230) if i == 0 else (190, 140, 70)))

    def _banner(self, canvas, lines):
        tiny = self.fonts["tiny"]
        widths = [sum(tiny.CharacterWidth(ord(c)) for c in line) for line in lines]
        box_w = max(widths) + 4
        box_h = 7 * len(lines) + 2
        left, top = 32 - box_w // 2, 16 - box_h // 2
        for x in range(left, left + box_w):
            for y in range(top, top + box_h):
                canvas.SetPixel(x, y, 0, 0, 0)
        for i, (line, width) in enumerate(zip(lines, widths)):
            frame.DrawText(canvas, tiny, 32 - width // 2, top + 7 + i * 7, frame.Color(255, 220, 80), line)
