# sudo /home/pi/bustracker/.venv/bin/python main.py 
import requests
from datetime import datetime, timedelta, timezone
import time
import math
import io
import re
from collections import deque
from google.protobuf.message import Message
from google.transit import gtfs_realtime_pb2
import sys
import os
import signal
import threading
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from PIL import Image, ImageOps

# NECESSARY!!!!! VVVVV
sys.path.append(os.path.abspath('/home/pi/rpi-rgb-led-matrix/bindings/python'))
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

# Fix SSL certificate issue
os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'
load_dotenv()


MTS_API_KEY = os.getenv("MTS_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")
TRIP_UPDATES_URL = "https://realtime.sdmts.com/api/api/gtfs_realtime/trip-updates-for-agency/MTS.pb?key="

STOP_ID = os.getenv("STOP_ID", "")  # MTS stop to show arrivals for
# Weather location
WEATHER_LAT = os.getenv("WEATHER_LAT", "")
WEATHER_LON = os.getenv("WEATHER_LON", "")

BUS_DURATION_MULTIPLIER = 1

# Bus cycle: wait times while "BUS TRACKER" scrolls, then arrival times
LABEL_PAUSE = 0.5          # seconds the label holds still before scrolling
FRAME_INTERVAL = 0.115625  # seconds per scroll pixel
ARRIVAL_PAUSE = 0.25
ARRIVAL_SECONDS = 6

# While Spotify is playing, album art takes the right half and the left half
# alternates: clock for SPOTIFY_CLOCK_SECONDS, then one bus cycle
SPOTIFY_CLOCK_SECONDS = 8
SPOTIFY_POLL_SECONDS = 10

# Brightness (1-100): daytime runs from 30 min before sunrise to 30 min after sunset
DAY_BRIGHTNESS = 75
NIGHT_BRIGHTNESS = 20
# Screen is blank from SCREEN_OFF_START_HOUR to SCREEN_OFF_END_HOUR (24h clock) unless Spotify is playing
SCREEN_OFF_START_HOUR = 0
SCREEN_OFF_END_HOUR = 6

# Red dot in the corner of the bus area when bus times haven't updated for this long
BUS_STALE_SECONDS = 120

# Flash an umbrella over the weather icon if rain is forecast within this many hours
RAIN_LOOKAHEAD_HOURS = 6
RAIN_CHANCE_THRESHOLD = 0.5  # forecast probability of precipitation (0-1)

# NFL scores ticker: scrolls across the whole screen, only on days with games, showing that day's
# games that have started or finished. Repeats every NFL_TICKER_LIVE_INTERVAL seconds while any
# game is in progress, otherwise every NFL_TICKER_INTERVAL seconds.
NFL_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NFL_TICKER_LIVE_INTERVAL = 60
NFL_TICKER_INTERVAL = 300
NFL_TICKER_FIRST_DELAY = 30  # seconds after startup before the first ticker
NFL_TICKER_SPEED = 40        # pixels per second
NFL_TICKER_PAUSE = 1.0       # seconds to hold on each game once it's centered
NFL_TICKER_FRAME = 0.03      # seconds per frame while the ticker runs
NFL_POLL_SECONDS = 600       # how often to refresh scores when nothing is happening
NFL_LIVE_POLL_SECONDS = 60   # refresh rate while a game is in progress or about to kick off
NFL_KICKOFF_WINDOW = 600     # start the fast refresh this many seconds before a kickoff
NFL_LOGO_SIZE = 16           # team logos are scaled to this many pixels square
NFL_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
TD_FLASH_SECONDS = 2.4       # "TOUCHDOWN" flashes this long before the play details scroll
TD_ALERT_MAX_AGE = 600       # drop touchdown alerts that couldn't be shown within this many seconds
NFL_SEPARATOR_PADDING = 3    # blank pixels either side of the line between games
NFL_LOGO_GAP = 1             # pixels between a logo and its score
NFL_TEAM_GAP = 4             # pixels between the away score and the home logo

# Configure the LED matrix
options = RGBMatrixOptions()
options.rows = 32
options.cols = 64
options.chain_length = 1
options.parallel = 1
options.hardware_mapping = 'regular'
options.gpio_slowdown = 2
options.brightness = DAY_BRIGHTNESS
## NECESSARY!!!!! VVVVV
options.hardware_mapping = 'adafruit-hat'
# Stay root after init: /home/pi isn't readable by the 'daemon' user the
# library would drop to, which breaks lazy imports (e.g. urllib3's HTTP/1 backend)
options.drop_privileges = False

arrival_times = []  # sorted arrival datetimes
last_bus_update = None  # time.monotonic() of the last successful bus fetch
rain_expected = False
nfl_games = []  # live/finished games, see parse_nfl_games
nfl_has_live = False
nfl_games_day = ""  # YYYYMMDD that nfl_games belong to
nfl_next_kickoff = None  # UTC datetime of today's next game that hasn't started
# Per game: total points when first seen (older plays are never alerted), total covered by the
# last summary fetch, and scoring play ids already handled
nfl_score_tracking = {}
nfl_td_alerts = deque()  # touchdowns waiting to be shown
nfl_logos = {}  # team abbreviation -> logo image (RGB, NFL_LOGO_SIZE square)
current_temperature = 0
weather_code = 0
sunrise_time = None
sunset_time = None
spotify_access_token = ""
spotify_token_expires_at = 0
spotify_album_art = None  # already resized to 32x32
spotify_album_art_url = ""
spotify_is_playing = False
spotify_progress_ms = 0
spotify_duration_ms = 0
spotify_progress_at = 0.0  # time.monotonic() when progress was read
spotify_track_name = ""
spotify_artist_name = ""

wait_time_font = graphics.Font()
wait_time_font.LoadFont("/home/pi/bustracker/fonts/7x13.bdf")
arrival_time_font = graphics.Font()
arrival_time_font.LoadFont("/home/pi/bustracker/fonts/6x10.bdf")
bus_number_font = graphics.Font()
bus_number_font.LoadFont("/home/pi/bustracker/fonts/4x6.bdf")
nfl_status_font = graphics.Font()
nfl_status_font.LoadFont("/home/pi/bustracker/fonts/5x7.bdf")
nfl_score_font = graphics.Font()
nfl_score_font.LoadFont("/home/pi/bustracker/fonts/6x13B.bdf")
date_font = graphics.Font()
date_font.LoadFont("/home/pi/bustracker/fonts/5x8.bdf")
default_color = graphics.Color(255, 255, 0)
bus_text_color = graphics.Color(214, 11, 180)
nfl_label_color = graphics.Color(213, 10, 10)
nfl_score_color = graphics.Color(255, 255, 255)
nfl_loser_score_color = graphics.Color(120, 120, 120)
nfl_status_color = graphics.Color(43, 224, 227)
nfl_separator_color = (70, 70, 70)
date_color = graphics.Color(43, 224, 227)

sun_color = graphics.Color(255, 225, 0)

# Get bus arrival times from MTS API
def get_bus_times():
    global arrival_times
    global last_bus_update
    try:
        response = requests.get(TRIP_UPDATES_URL + MTS_API_KEY, timeout=5)
        response.raise_for_status()

        # Parse the protobuf directly from response bytes
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)

        new_arrivals = []
        for entity in feed.entity:
            if entity.HasField('trip_update'):
                for update in entity.trip_update.stop_time_update:
                    if update.stop_id == STOP_ID:
                        timestamp = update.arrival.time or update.departure.time
                        if not timestamp:
                            continue
                        arrival_time = datetime.fromtimestamp(timestamp)
                        # skip buses that have already left
                        if arrival_time < datetime.now() - timedelta(minutes=1):
                            continue
                        new_arrivals.append(arrival_time)
        # Swap in the whole list at once so the draw loop never sees a half-built list
        arrival_times = sorted(new_arrivals)
        last_bus_update = time.monotonic()
    except Exception as e:
        print(f"Bus API error: {e}")
        # Keep existing arrival_times on error


def schedule_bus_update():
    get_bus_times()
    timer = threading.Timer(30.0, schedule_bus_update)
    timer.daemon = True
    timer.start()


def get_wait_color(minutes):
    # Green for a bus that's about to come, shading to red as the wait gets longer
    return graphics.Color(int(min(255, 5.67 * minutes)), int(max(0, 255 - (5.67 * minutes))), 0)


def get_bus_screen(count=3):
    # Snapshot of the next buses: wait strings, arrival strings, colors and the scrolling label
    now = datetime.now()
    waits, arrivals, colors = [], [], []
    for t in arrival_times[:count]:
        minutes = (t - now).total_seconds() / 60
        if minutes >= 1:
            waits.append(f"{math.ceil(minutes)}m")
            colors.append(get_wait_color(minutes))
        else:
            waits.append("Now")
            colors.append(graphics.Color(255, 255, 255))
        arrivals.append(t.strftime("%I:%M"))
    while len(waits) < count:
        waits.append("")
        arrivals.append("")
        colors.append(default_color)

    # Scrolling stops with the last 3 letters of the label showing
    label = "BUS TRACKER"
    scroll_pixels = 10 * (len(label) - 3)
    scroll_end = LABEL_PAUSE + scroll_pixels * FRAME_INTERVAL
    arrivals_start = scroll_end + ARRIVAL_PAUSE
    return {
        "waits": waits,
        "arrivals": arrivals,
        "colors": colors,
        "label": label,
        "scroll_pixels": scroll_pixels,
        "scroll_end": scroll_end,
        "arrivals_start": arrivals_start,
        "cycle_seconds": arrivals_start + ARRIVAL_SECONDS,
    }


def draw_bus_waits(canvas, screen, y_pos, x):
    waits, colors = screen["waits"], screen["colors"]
    graphics.DrawText(canvas, wait_time_font, x + 2, 10, colors[0], waits[0])
    graphics.DrawText(canvas, wait_time_font, x + 2, 20, colors[1], waits[1])
    graphics.DrawText(canvas, wait_time_font, x + 2, 30, colors[2], waits[2])
    for i, letter in enumerate(screen["label"]):
        y = 10 * (i + 1) + y_pos
        if letter == " " or y < 0 or y > 45:
            continue
        graphics.DrawText(canvas, wait_time_font, x + 24, y, bus_text_color, letter)


def draw_bus_arrivals(canvas, arrivals, colors, x):
    graphics.DrawText(canvas, arrival_time_font, x + 2, 9, colors[0], arrivals[0])
    graphics.DrawText(canvas, arrival_time_font, x + 2, 19, colors[1], arrivals[1])
    graphics.DrawText(canvas, arrival_time_font, x + 2, 29, colors[2], arrivals[2])


def draw_bus(canvas, screen, elapsed, x):
    # elapsed: seconds into the current bus cycle; x: left edge of the half to draw in
    if elapsed < screen["scroll_end"]:
        y_pos = -min(screen["scroll_pixels"], max(0, int((elapsed - LABEL_PAUSE) / FRAME_INTERVAL)))
        draw_bus_waits(canvas, screen, y_pos, x)
    elif elapsed >= screen["arrivals_start"]:
        draw_bus_arrivals(canvas, screen["arrivals"], screen["colors"], x)


def bus_data_is_stale():
    reference = last_bus_update if last_bus_update is not None else STARTED_AT
    return time.monotonic() - reference > BUS_STALE_SECONDS


def draw_stale_dot(canvas, x):
    # 2x2 red dot in the top-right corner of the bus half
    for dx in (30, 31):
        for dy in (0, 1):
            canvas.SetPixel(x + dx, dy, 255, 0, 0)


def get_openmeteo_weather():
    global current_temperature
    global weather_code
    global sunrise_time
    global sunset_time
    try:
        # Use raw requests to get JSON response instead of using the SDK
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {
            "lat": WEATHER_LAT,
            "lon": WEATHER_LON,
            "appid": OPENWEATHER_API_KEY,
            "units": "imperial",
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        print(data)

        current_temperature = float(data['main']['temp'])
        weather_code = data['weather'][0]['id']
        
        sunrise_str = data['sys']['sunrise']
        sunset_str = data['sys']['sunset']

        sunrise_time = datetime.fromtimestamp(sunrise_str)
        sunset_time = datetime.fromtimestamp(sunset_str)

        print(f"Updated weather - Temp: {current_temperature}F, Code: {weather_code}")
        print(f"Sunrise: {sunrise_time}, Sunset: {sunset_time}")
        
    except Exception as e:
        print(f"Weather API error: {e}")
        import traceback
        traceback.print_exc()
        # Keep the last known weather on error


def get_rain_forecast():
    # 3-hourly forecast: is rain (or storms/drizzle/snow) likely in the next RAIN_LOOKAHEAD_HOURS?
    global rain_expected
    try:
        response = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={
                "lat": WEATHER_LAT,
                "lon": WEATHER_LON,
                "appid": OPENWEATHER_API_KEY,
                "units": "imperial",
                "cnt": math.ceil(RAIN_LOOKAHEAD_HOURS / 3) + 1,
            },
            timeout=10,
        )
        response.raise_for_status()
        cutoff = time.time() + RAIN_LOOKAHEAD_HOURS * 3600
        expected = False
        for slot in response.json().get("list", []):
            if slot.get("dt", 0) > cutoff:
                continue
            ids = [w.get("id", 800) for w in slot.get("weather", [])]
            if slot.get("pop", 0) >= RAIN_CHANCE_THRESHOLD or any(i < 700 for i in ids):
                expected = True
        rain_expected = expected
        print(f"Rain expected in next {RAIN_LOOKAHEAD_HOURS}h: {rain_expected}")
    except Exception as e:
        print(f"Forecast API error: {e}")
        # Keep the last known forecast on error

def schedule_weather_update():
    get_openmeteo_weather()
    get_rain_forecast()
    timer = threading.Timer(600.0, schedule_weather_update)  # 600 seconds = 10 minutes
    timer.daemon = True
    timer.start()


def get_team_color(team):
    # ESPN gives a primary and alternate hex color; many primaries are too dark for LEDs
    def parse(hex_color):
        hex_color = (hex_color or "").lstrip("#")
        if len(hex_color) != 6:
            return None
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

    rgb = parse(team.get("color")) or (255, 255, 255)
    alternate = parse(team.get("alternateColor"))
    if max(rgb) < 100 and alternate and max(alternate) > max(rgb):
        rgb = alternate
    peak = max(rgb)
    if peak == 0:
        return (255, 255, 255)
    if peak < 180:
        rgb = tuple(min(255, int(c * 180 / peak)) for c in rgb)
    return rgb


def parse_nfl_games(data):
    games = []
    for event in data.get("events", []):
        status = event.get("status", {}).get("type", {})
        state = status.get("state")
        # pre = not started yet; only show games in progress or finished
        if state not in ("in", "post"):
            continue
        competition = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in competition.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue
        # Shown in small text above the scores: "FINAL", "FINAL/OT", "8:42 3RD", "HALFTIME"
        status_text = (status.get("shortDetail") or "").upper().replace(" - ", " ")
        game = {"id": event.get("id"), "state": state, "status": status_text}
        for side in ("away", "home"):
            competitor = sides[side]
            team = competitor.get("team", {})
            game[side] = {
                "abbr": team.get("abbreviation", "?"),
                "score": competitor.get("score", "0"),
                "winner": bool(competitor.get("winner")),
                "color": get_team_color(team),
                # logoDark is ESPN's version for dark backgrounds
                "logo_url": team.get("logoDark") or team.get("logo") or "",
            }
        games.append(game)
    return games


def load_nfl_logo(url):
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    logo = Image.open(io.BytesIO(response.content)).convert("RGBA")
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    logo.thumbnail((NFL_LOGO_SIZE, NFL_LOGO_SIZE), resample)
    # Flatten transparency onto black and center it in a square tile
    tile = Image.new("RGB", (NFL_LOGO_SIZE, NFL_LOGO_SIZE), (0, 0, 0))
    position = ((NFL_LOGO_SIZE - logo.width) // 2, (NFL_LOGO_SIZE - logo.height) // 2)
    tile.paste(logo, position, logo)
    return tile


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def clean_play_text(text):
    # "Bucky Irving 5 Yd pass from Jalon Daniels (Chase McLaughlin Kick)" -> scorer and description
    description = re.sub(r"\s*\(.*?\)", "", text or "").strip()
    match = re.match(r"^(.*?)\s+\d+\s+Yd", description)
    scorer = match.group(1) if match else ""
    return scorer.upper(), description.upper()


def check_for_touchdowns(games):
    # The scoreboard only has totals; when a game's total goes up, fetch that game's scoring plays
    for game in games:
        if not game["id"]:
            continue
        total = to_int(game["away"]["score"]) + to_int(game["home"]["score"])
        track = nfl_score_tracking.get(game["id"])
        if track is None:
            nfl_score_tracking[game["id"]] = {"baseline": total, "checked_total": total, "seen": set()}
            continue
        if total <= track["checked_total"]:
            continue
        try:
            response = requests.get(NFL_SUMMARY_URL, params={"event": game["id"]}, timeout=10)
            response.raise_for_status()
            plays = response.json().get("scoringPlays", [])
        except Exception as e:
            print(f"NFL scoring plays error: {e}")
            continue  # try again on the next refresh

        teams = {game[side]["abbr"]: game[side] for side in ("away", "home")}
        latest_total = track["checked_total"]
        for play in plays:
            play_total = to_int(play.get("awayScore")) + to_int(play.get("homeScore"))
            # The summary can lag the scoreboard; only count what it actually includes
            latest_total = max(latest_total, play_total)
            if play.get("id") in track["seen"]:
                continue
            track["seen"].add(play.get("id"))
            if play_total <= track["baseline"]:
                continue  # scored before we started watching this game
            if (play.get("scoringType") or {}).get("name") != "touchdown":
                continue
            abbr = (play.get("team") or {}).get("abbreviation", "")
            team = teams.get(abbr)
            opponent = next((t for a, t in teams.items() if a != abbr), None)
            away_score, home_score = to_int(play.get("awayScore")), to_int(play.get("homeScore"))
            team_score, opponent_score = (away_score, home_score) if team is game["away"] else (home_score, away_score)
            scorer, description = clean_play_text(play.get("text"))
            nfl_td_alerts.append({
                "abbr": abbr,
                "color": team["color"] if team else (255, 255, 255),
                "opponent": opponent["abbr"] if opponent else "",
                "score": f"{team_score}-{opponent_score}",
                "scorer": scorer,
                "description": description,
                "queued_at": time.monotonic(),
            })
            print(f"Touchdown {abbr}: {description} ({team_score}-{opponent_score})")
        track["checked_total"] = latest_total


def load_missing_nfl_logos(games):
    # Logos don't change, so each team is downloaded once per run
    for game in games:
        for side in ("away", "home"):
            team = game[side]
            if team["abbr"] in nfl_logos or not team["logo_url"]:
                continue
            try:
                nfl_logos[team["abbr"]] = load_nfl_logo(team["logo_url"])
            except Exception as e:
                print(f"NFL logo error for {team['abbr']}: {e}")


def get_nfl_scores():
    global nfl_games
    global nfl_has_live
    global nfl_games_day
    global nfl_next_kickoff
    try:
        # ESPN groups games by US day, so the Sunday night game counts as Sunday
        day = datetime.now().strftime("%Y%m%d")
        response = requests.get(NFL_SCOREBOARD_URL, params={"dates": day}, timeout=10)
        response.raise_for_status()
        data = response.json()
        games = parse_nfl_games(data)

        kickoffs = []
        for event in data.get("events", []):
            if event.get("status", {}).get("type", {}).get("state") == "pre" and event.get("date"):
                kickoffs.append(datetime.fromisoformat(event["date"].replace("Z", "+00:00")))

        load_missing_nfl_logos(games)
        check_for_touchdowns(games)
        nfl_games = games
        nfl_games_day = day
        nfl_has_live = any(g["state"] == "in" for g in games)
        nfl_next_kickoff = min(kickoffs) if kickoffs else None
        print(f"NFL scores updated for {day}: {len(games)} started/finished, live: {nfl_has_live}, "
              f"next kickoff: {nfl_next_kickoff}")
    except Exception as e:
        print(f"NFL scores error: {e}")
        # Keep the last known scores on error


def get_nfl_poll_seconds():
    # Refresh every minute while a game is on or one is about to start (kickoffs often run late)
    if nfl_has_live:
        return NFL_LIVE_POLL_SECONDS
    if nfl_next_kickoff is not None:
        until_kickoff = (nfl_next_kickoff - datetime.now(timezone.utc)).total_seconds()
        if until_kickoff <= NFL_KICKOFF_WINDOW:
            return NFL_LIVE_POLL_SECONDS
        return min(NFL_POLL_SECONDS, until_kickoff - NFL_KICKOFF_WINDOW)
    return NFL_POLL_SECONDS


def get_todays_nfl_games():
    # Ignore scores left over from a previous day until the next refresh replaces them
    if nfl_games_day != datetime.now().strftime("%Y%m%d"):
        return []
    return nfl_games


def schedule_nfl_update():
    get_nfl_scores()
    timer = threading.Timer(get_nfl_poll_seconds(), schedule_nfl_update)
    timer.daemon = True
    timer.start()


# Ticker rows: small status text on top, then logos and scores below it
NFL_STATUS_BASELINE = 7
NFL_LOGO_TOP = 10
NFL_SCORE_BASELINE = 23


def build_nfl_ticker(games):
    # Lay the ticker out once as segments (text, logo or separator line) at fixed x offsets
    segments = []
    x = 0

    def text_width(text, font):
        return sum(font.CharacterWidth(ord(c)) for c in text)

    def add_text(text, color=None, font=nfl_score_font):
        nonlocal x
        width = text_width(text, font)
        if color is not None:
            segments.append({"x": x, "width": width, "kind": "text", "text": text, "color": color,
                             "font": font, "y": NFL_SCORE_BASELINE})
        x += width

    def add_team(team):
        # Logo only; fall back to the abbreviation if the logo couldn't be downloaded
        nonlocal x
        logo = nfl_logos.get(team["abbr"])
        if logo is None:
            add_text(team["abbr"], graphics.Color(*team["color"]))
            add_text(" ")
            return
        segments.append({"x": x, "width": NFL_LOGO_SIZE, "kind": "image", "image": logo})
        x += NFL_LOGO_SIZE + NFL_LOGO_GAP

    def add_separator():
        nonlocal x
        x += NFL_SEPARATOR_PADDING
        segments.append({"x": x, "width": 1, "kind": "line"})
        x += 1 + NFL_SEPARATOR_PADDING

    add_text("NFL", nfl_label_color, wait_time_font)
    stops = []
    for game in games:
        add_separator()
        game_start = x
        for i, side in enumerate(("away", "home")):
            team = game[side]
            add_team(team)
            losing = game["state"] == "post" and not team["winner"]
            add_text(team["score"], nfl_loser_score_color if losing else nfl_score_color)
            if i == 0:
                x += NFL_TEAM_GAP
        # Status ("F", "8:42 3RD", "HALFTIME") in small text centered above the game
        status_width = text_width(game["status"], nfl_status_font)
        segments.append({"x": game_start + (x - game_start - status_width) // 2, "width": status_width,
                         "kind": "text", "text": game["status"], "color": nfl_status_color,
                         "font": nfl_status_font, "y": NFL_STATUS_BASELINE})
        # Scroll distance at which this game sits centered on screen
        stops.append(options.cols + game_start - (options.cols - (x - game_start)) // 2)
    return {"segments": segments, "width": x, "stops": stops}


def get_nfl_ticker_scroll(ticker, elapsed):
    # Pixels scrolled after `elapsed` seconds: move at NFL_TICKER_SPEED, holding NFL_TICKER_PAUSE at each stop
    scrolled = 0.0
    for stop in ticker["stops"]:
        travel = (stop - scrolled) / NFL_TICKER_SPEED
        if elapsed < travel:
            return scrolled + elapsed * NFL_TICKER_SPEED
        elapsed -= travel
        scrolled = stop
        if elapsed < NFL_TICKER_PAUSE:
            return scrolled
        elapsed -= NFL_TICKER_PAUSE
    return scrolled + elapsed * NFL_TICKER_SPEED


def draw_nfl_ticker(canvas, ticker, elapsed):
    # Returns False once everything has scrolled fully off the left edge
    offset = options.cols - int(get_nfl_ticker_scroll(ticker, elapsed))
    if offset + ticker["width"] < 0:
        return False
    for segment in ticker["segments"]:
        left = offset + segment["x"]
        # Only draw segments that are at least partly on screen
        if left >= options.cols or left + segment["width"] < 0:
            continue
        if segment["kind"] == "text":
            graphics.DrawText(canvas, segment["font"], left, segment["y"], segment["color"], segment["text"])
        elif segment["kind"] == "image":
            canvas.SetImage(segment["image"], left, NFL_LOGO_TOP)
        elif segment["kind"] == "line" and left >= 0:
            for y in range(3, options.rows - 3):
                canvas.SetPixel(left, y, *nfl_separator_color)
    return True


def next_td_alert():
    # Oldest touchdown that's still fresh, or None
    while nfl_td_alerts:
        alert = nfl_td_alerts.popleft()
        if time.monotonic() - alert["queued_at"] <= TD_ALERT_MAX_AGE:
            return alert
    return None


def build_td_alert(alert):
    # Scrolling line: scorer's name in team color, the rest of the play in white
    scorer = alert["scorer"]
    rest = alert["description"][len(scorer):] if scorer and alert["description"].startswith(scorer) else " " + alert["description"]
    color = graphics.Color(*alert["color"])
    width = lambda text: sum(arrival_time_font.CharacterWidth(ord(c)) for c in text)
    parts = [(0, scorer, color), (width(scorer), rest, nfl_score_color)]
    return {**alert, "team_color": color, "parts": parts, "text_width": width(scorer) + width(rest)}


def draw_td_alert(canvas, alert, elapsed):
    # Returns False once the alert has finished
    if elapsed < TD_FLASH_SECONDS:
        # Flash "TOUCHDOWN" between the team color and white
        flash_color = alert["team_color"] if int(elapsed / 0.3) % 2 == 0 else nfl_score_color
        graphics.DrawText(canvas, wait_time_font, 1, 21, flash_color, "TOUCHDOWN")
        return True

    scrolled = int((elapsed - TD_FLASH_SECONDS) * NFL_TICKER_SPEED)
    if options.cols - scrolled + alert["text_width"] < 0:
        return False

    # Top: team logo and the score after the play (scoring team first)
    logo = nfl_logos.get(alert["abbr"])
    if logo is not None:
        canvas.SetImage(logo, 1, 0)
        score_x = NFL_LOGO_SIZE + 4
    else:
        graphics.DrawText(canvas, nfl_score_font, 1, 13, alert["team_color"], alert["abbr"])
        score_x = 1 + len(alert["abbr"]) * 6 + 3
    graphics.DrawText(canvas, nfl_score_font, score_x, 13, nfl_score_color, alert["score"])

    # Bottom: the play, scrolling once
    for x, text, color in alert["parts"]:
        graphics.DrawText(canvas, arrival_time_font, options.cols - scrolled + x, 29, color, text)
    return True


def get_spotify_access_token():
    global spotify_access_token
    global spotify_token_expires_at

    if spotify_access_token and time.time() < spotify_token_expires_at - 30:
        return spotify_access_token

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET or not SPOTIFY_REFRESH_TOKEN:
        return None

    try:
        response = requests.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": SPOTIFY_REFRESH_TOKEN,
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        spotify_access_token = data.get("access_token", "")
        spotify_token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return spotify_access_token
    except Exception as e:
        print(f"Spotify token error: {e}")
        spotify_access_token = ""
        spotify_token_expires_at = 0
        return None


def clear_spotify_now_playing():
    global spotify_album_art, spotify_album_art_url, spotify_is_playing
    global spotify_track_name, spotify_artist_name
    spotify_is_playing = False
    spotify_album_art = None
    spotify_album_art_url = ""
    spotify_track_name = ""
    spotify_artist_name = ""


def fetch_spotify_now_playing():
    global spotify_album_art
    global spotify_album_art_url
    global spotify_is_playing
    global spotify_progress_ms
    global spotify_duration_ms
    global spotify_progress_at
    global spotify_track_name
    global spotify_artist_name

    token = get_spotify_access_token()
    if not token:
        clear_spotify_now_playing()
        return

    try:
        response = requests.get(
            "https://api.spotify.com/v1/me/player/currently-playing",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        # 204 = nothing playing
        if response.status_code == 204:
            clear_spotify_now_playing()
            return

        response.raise_for_status()
        data = response.json()
        item = data.get("item") or {}
        spotify_track_name = item.get("name", "")
        spotify_artist_name = ", ".join(
            artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")
        )

        images = item.get("album", {}).get("images", [])
        image_url = images[0].get("url") if images else ""
        if not image_url:
            spotify_album_art = None
            spotify_album_art_url = ""
        elif image_url != spotify_album_art_url:
            # Only download and resize when the album changes
            image_response = requests.get(image_url, timeout=10)
            image_response.raise_for_status()
            image = Image.open(io.BytesIO(image_response.content)).convert("RGB")
            resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
            spotify_album_art = ImageOps.fit(image, (32, 32), method=resample)
            spotify_album_art_url = image_url

        spotify_progress_ms = int(data.get("progress_ms") or 0)
        spotify_duration_ms = int(item.get("duration_ms") or 0)
        spotify_progress_at = time.monotonic()
        spotify_is_playing = bool(data.get("is_playing"))
    except Exception as e:
        print(f"Spotify playback error: {e}")


def schedule_spotify_update():
    fetch_spotify_now_playing()
    timer = threading.Timer(SPOTIFY_POLL_SECONDS, schedule_spotify_update)
    timer.daemon = True
    timer.start()

def draw_sun(canvas):
    canvas.SetPixel(25, 19, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(21, 20, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 20, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(29, 20, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(22, 21, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(28, 21, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(24, 22, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 22, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(26, 22, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(23, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(24, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(26, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(27, 23, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(20, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(21, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(23, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(24, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(26, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(27, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(29, 24, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(30, 24, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(23, 25, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(24, 25, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 25, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(26, 25, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(27, 25, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(24, 26, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 26, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(26, 26, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(22, 27, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(28, 27, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(21, 28, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 28, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(29, 28, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(25, 29, sun_color.red, sun_color.green, sun_color.blue)

def draw_cloud(canvas):
    
    canvas.SetPixel(23, 22, 220, 220, 220)
    canvas.SetPixel(24, 22, 220, 220, 220)

    canvas.SetPixel(22, 23, 220, 220, 220)
    canvas.SetPixel(23, 23, 220, 220, 220)
    canvas.SetPixel(24, 23, 220, 220, 220)
    canvas.SetPixel(25, 23, 220, 220, 220)
    canvas.SetPixel(27, 23, 220, 220, 220)
    canvas.SetPixel(28, 23, 220, 220, 220)

    for x in range(21, 29):
        canvas.SetPixel(x, 24, 220, 220, 220)
        canvas.SetPixel(x, 27, 220, 220, 220)

    for x in range(20, 30):
        canvas.SetPixel(x, 25, 220, 220, 220)
        canvas.SetPixel(x, 26, 220, 220, 220)

def draw_rain(canvas, is_animated):
    draw_cloud(canvas)
    if is_animated:
        canvas.SetPixel(22, 29, 0, 0, 255)
        canvas.SetPixel(26, 29, 0, 0, 255)

        canvas.SetPixel(22, 30, 0, 0, 255)
        canvas.SetPixel(24, 30, 0, 0, 255)
        canvas.SetPixel(26, 30, 0, 0, 255)
        canvas.SetPixel(28, 30, 0, 0, 255)

        canvas.SetPixel(24, 31, 0, 0, 255)
        canvas.SetPixel(28, 31, 0, 0, 255)
    else:
        canvas.SetPixel(22, 28, 0, 0, 255)
        canvas.SetPixel(26, 28, 0, 0, 255)

        canvas.SetPixel(22, 29, 0, 0, 255)
        canvas.SetPixel(24, 29, 0, 0, 255)
        canvas.SetPixel(26, 29, 0, 0, 255)
        canvas.SetPixel(28, 29, 0, 0, 255)

        canvas.SetPixel(24, 30, 0, 0, 255)
        canvas.SetPixel(28, 30, 0, 0, 255)

def draw_partly_cloudy(canvas):
    draw_cloud(canvas)

    canvas.SetPixel(28, 18, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(24, 19, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(28, 19, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(31, 19, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(25, 20, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(30, 20, sun_color.red, sun_color.green, sun_color.blue)

    for x in range(27, 29):
        canvas.SetPixel(x, 21, sun_color.red, sun_color.green, sun_color.blue)

    for x in range(26, 30):
        canvas.SetPixel(x, 22, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(26, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(28, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(29, 23, sun_color.red, sun_color.green, sun_color.blue)
    canvas.SetPixel(31, 23, sun_color.red, sun_color.green, sun_color.blue)

    canvas.SetPixel(29, 24, sun_color.red, sun_color.green, sun_color.blue)

def draw_thunderstorm(canvas, is_animated):
    draw_cloud(canvas)
    if is_animated:
        canvas.SetPixel(27, 29, 0, 0, 255)

        canvas.SetPixel(22, 30, 0, 0, 255)
        canvas.SetPixel(27, 30, 0, 0, 255)

        canvas.SetPixel(22, 31, 0, 0, 255)
    else:
        canvas.SetPixel(24, 28, 255, 255, 0)
        canvas.SetPixel(27, 28, 0, 0, 255)

        canvas.SetPixel(22, 29, 0, 0, 255)
        canvas.SetPixel(24, 29, 255, 255, 0)
        canvas.SetPixel(25, 29, 255, 255, 0)
        canvas.SetPixel(27, 29, 0, 0, 255)

        canvas.SetPixel(22, 30, 0, 0, 255)
        canvas.SetPixel(25, 30, 255, 255, 0)

        canvas.SetPixel(25, 31, 255, 255, 0)

def draw_moon(canvas, is_star):
    for x in range(25, 27):
        canvas.SetPixel(x, 19, 255, 255, 255)
    
    for x in range(26, 28):
        canvas.SetPixel(x, 20, 255, 255, 255)
    
    for x in range(26, 29):
        canvas.SetPixel(x, 21, 255, 255, 255)
    
    for x in range(27, 29):
        canvas.SetPixel(x, 22, 255, 255, 255)

    for x in range(27, 30):
        canvas.SetPixel(x, 23, 255, 255, 255)

    for x in range(27, 30):
        canvas.SetPixel(x, 24, 255, 255, 255)

    for x in range(27, 30):
        canvas.SetPixel(x, 25, 255, 255, 255)
    
    
    canvas.SetPixel(21, 26, 255, 255, 255)
    for x in range(26, 29):
        canvas.SetPixel(x, 26, 255, 255, 255)

    for x in range(20, 24):
        canvas.SetPixel(x, 27, 255, 255, 255)
    for x in range(25, 29):
        canvas.SetPixel(x, 27, 255, 255, 255)
    
    for x in range(21, 28):
        canvas.SetPixel(x, 28, 255, 255, 255)

    for x in range(23, 26):
        canvas.SetPixel(x, 29, 255, 255, 255)

    if (is_star):
        canvas.SetPixel(21, 20, 255, 255, 0)
        
        canvas.SetPixel(20, 21, 255, 255, 0)
        canvas.SetPixel(21, 21, 255, 255, 0)
        canvas.SetPixel(22, 21, 255, 255, 0)

        canvas.SetPixel(21, 22, 255, 255, 0)


def get_temp_color(temp):
    try:
        if temp >= 70:
            r = 255
            g = int(min(255, max(0, 255 - (8.5 * (temp - 70)))))
            b = 0
            return graphics.Color(r, g, b)

        r = int(min(255, max(0, 140 + (3.83 * (temp - 40)))))
        g = int(min(255, max(0, 219 - (1.2 * (temp - 40)))))
        b = int(min(255, max(0, 255 - (8.5 * (temp - 40)))))
        return graphics.Color(r, g, b)
    except:
        if temp <= 40:
            return graphics.Color(140, 219, 255)
        elif temp >= 100:
            return graphics.Color(255, 0, 0)
        return default_color


def is_daytime(now):
    # Day runs from 30 min before sunrise to 30 min after sunset; assume day until weather has loaded
    if not (sunrise_time and sunset_time):
        return True
    return not ((now > sunset_time + timedelta(minutes=30)) or (now < sunrise_time - timedelta(minutes=30)))


def in_screen_off_hours(now):
    if SCREEN_OFF_START_HOUR <= SCREEN_OFF_END_HOUR:
        return SCREEN_OFF_START_HOUR <= now.hour < SCREEN_OFF_END_HOUR
    # Window wraps past midnight, e.g. 23 -> 6
    return now.hour >= SCREEN_OFF_START_HOUR or now.hour < SCREEN_OFF_END_HOUR


def get_brightness(spotify_playing):
    # 0 means the screen should be off
    now = datetime.now()
    if in_screen_off_hours(now):
        return NIGHT_BRIGHTNESS if spotify_playing else 0
    return DAY_BRIGHTNESS if is_daytime(now) else NIGHT_BRIGHTNESS


def draw_umbrella(canvas):
    canopy = (80, 160, 255)
    handle = (200, 200, 200)
    canvas.SetPixel(25, 18, *handle)
    for y, (x0, x1) in {19: (24, 26), 20: (22, 28), 21: (21, 29), 22: (20, 30)}.items():
        for x in range(x0, x1 + 1):
            canvas.SetPixel(x, y, *canopy)
    # scalloped bottom edge of the canopy
    for x in (20, 21, 24, 25, 26, 29, 30):
        canvas.SetPixel(x, 23, *canopy)
    for y in range(24, 30):
        canvas.SetPixel(25, y, *handle)
    for x, y in ((24, 30), (23, 30), (22, 29)):
        canvas.SetPixel(x, y, *handle)


def draw_clock_left(canvas, blink_on):
    temp = current_temperature
    now = datetime.now()
    is_day = is_daytime(now)

    temp_color = get_temp_color(temp)

    time_str = now.strftime("%I:%M" if blink_on else "%I %M")
    month_str = now.strftime("%b")
    day_str = now.strftime("%d")
    canvas.Clear()
    graphics.DrawText(canvas, arrival_time_font, 2, 10, default_color, time_str)
    graphics.DrawText(canvas, date_font, 2, 17, date_color, month_str)
    graphics.DrawText(canvas, date_font, 15, 17, date_color, ",")
    graphics.DrawText(canvas, date_font, 22, 17, date_color, day_str)
    if (temp < 100):
        graphics.DrawText(canvas, wait_time_font, 2, 30, temp_color, f"{temp:.0f}")
        graphics.DrawText(canvas, arrival_time_font, 15, 27, temp_color, "°")
        # If rain is on the way but it isn't raining yet, alternate the icon with an umbrella every 2s
        raining_now = weather_code < 700 and weather_code != 0
        if rain_expected and not raining_now and (now.second // 2) % 2 == 1:
            draw_umbrella(canvas)
        else:
            draw_weather(canvas, is_day, not blink_on)
    else:
        graphics.DrawText(canvas, wait_time_font, 5, 30, temp_color, f"{temp:.0f}")
        graphics.DrawText(canvas, arrival_time_font, 24, 27, temp_color, "°")
    return canvas


def draw_blinking_clock(canvas):
    return draw_clock_left(canvas, False)


def draw_spotify_art(canvas, art):
    canvas.SetImage(art, 32, 0)

    # 1px progress bar along the bottom of the cover, estimated between polls
    if spotify_duration_ms <= 0:
        return
    progress_ms = spotify_progress_ms + (time.monotonic() - spotify_progress_at) * 1000
    filled = int(32 * min(1.0, max(0.0, progress_ms / spotify_duration_ms)))
    for i in range(32):
        if i < filled:
            canvas.SetPixel(32 + i, 31, 255, 255, 255)
        else:
            canvas.SetPixel(32 + i, 31, 40, 40, 40)


def draw_weather(canvas, is_day, is_animated):
    if weather_code in [300, 301, 302, 310, 311, 312, 313, 314, 321, 500, 501, 502, 503, 504, 511, 520, 521, 522, 531]:
        draw_rain(canvas, is_animated)
    elif weather_code in [200, 201, 202, 210, 211, 212, 221, 230, 231, 232]:
        draw_thunderstorm(canvas, is_animated)
    elif not is_day:
        draw_moon(canvas, is_animated)
    elif weather_code == 800:
        draw_sun(canvas)
    elif weather_code in [801, 802]:
        draw_partly_cloudy(canvas)
    else:
        draw_cloud(canvas)

# systemctl stop sends SIGTERM; exit cleanly so the matrix gets cleared
signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

# Initialize matrix
matrix = RGBMatrix(options=options)
offscreen_canvas = matrix.CreateFrameCanvas()  # Create ONCE and reuse

# Start weather update scheduler
schedule_weather_update()
schedule_spotify_update()
schedule_bus_update()

STARTED_AT = time.monotonic()
schedule_nfl_update()
nfl_ticker = None
nfl_ticker_start = 0.0
td_alert = None
td_alert_start = 0.0
next_nfl_ticker_at = STARTED_AT + NFL_TICKER_FIRST_DELAY
cycle_start = time.monotonic()
bus_screen = get_bus_screen()
has_buses = bool(arrival_times)
showing_spotify = False

try:
    while True:
        art = spotify_album_art
        spotify_now = spotify_is_playing and art is not None

        brightness = get_brightness(spotify_now)
        if brightness == 0:
            offscreen_canvas.Clear()
            offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
            time.sleep(1)
            continue

        if nfl_ticker is not None:
            # NFL ticker takes over the whole screen until it has scrolled past
            offscreen_canvas.brightness = brightness
            offscreen_canvas.Clear()
            if draw_nfl_ticker(offscreen_canvas, nfl_ticker, time.monotonic() - nfl_ticker_start):
                offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
                time.sleep(NFL_TICKER_FRAME)
                continue
            nfl_ticker = None
            next_nfl_ticker_at = time.monotonic() + (NFL_TICKER_LIVE_INTERVAL if nfl_has_live else NFL_TICKER_INTERVAL)
            cycle_start = 0.0  # force a fresh cycle below

        # Touchdowns interrupt the clock/bus screens right away (but wait for a running ticker)
        if td_alert is None and nfl_td_alerts:
            alert = next_td_alert()
            if alert is not None:
                td_alert = build_td_alert(alert)
                td_alert_start = time.monotonic()
        if td_alert is not None:
            offscreen_canvas.brightness = brightness
            offscreen_canvas.Clear()
            if draw_td_alert(offscreen_canvas, td_alert, time.monotonic() - td_alert_start):
                offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
                time.sleep(NFL_TICKER_FRAME)
                continue
            td_alert = None
            cycle_start = 0.0  # force a fresh cycle below

        cycle_seconds = bus_screen["cycle_seconds"] + (SPOTIFY_CLOCK_SECONDS if spotify_now else 0)
        elapsed = time.monotonic() - cycle_start
        if spotify_now != showing_spotify or elapsed >= cycle_seconds:
            # Restart the cycle on a fresh snapshot (also when Spotify starts or stops)
            showing_spotify = spotify_now
            cycle_start = time.monotonic()
            bus_screen = get_bus_screen()
            has_buses = bool(arrival_times)
            elapsed = 0

            # Run the NFL ticker between cycles so it never cuts off the bus screens
            games = get_todays_nfl_games()
            if games and time.monotonic() >= next_nfl_ticker_at:
                nfl_ticker = build_nfl_ticker(games)
                nfl_ticker_start = time.monotonic()
                continue

        stale = bus_data_is_stale()
        offscreen_canvas.brightness = brightness
        offscreen_canvas.Clear()
        if showing_spotify:
            # Left half: clock, then the bus cycle; right half: album art
            if elapsed < SPOTIFY_CLOCK_SECONDS or not has_buses:
                draw_clock_left(offscreen_canvas, datetime.now().second % 2 == 0)
            else:
                draw_bus(offscreen_canvas, bus_screen, elapsed - SPOTIFY_CLOCK_SECONDS, 0)
                if stale:
                    draw_stale_dot(offscreen_canvas, 0)
            draw_spotify_art(offscreen_canvas, art)
        else:
            draw_clock_left(offscreen_canvas, datetime.now().second % 2 == 0)
            if has_buses:
                draw_bus(offscreen_canvas, bus_screen, elapsed, 32)
            if stale:
                draw_stale_dot(offscreen_canvas, 32)
        offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
        time.sleep(FRAME_INTERVAL)

except KeyboardInterrupt:
    print("Exiting...")
finally:
    matrix.Clear()
    del matrix
