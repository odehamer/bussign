# sudo /home/pi/bustracker/.venv/bin/python main.py 
import requests
from datetime import datetime, timedelta, timezone
import time
import math
import io
import json
import re
import unicodedata
from collections import deque
from google.protobuf.message import Message
from google.transit import gtfs_realtime_pb2
import sys
import os
import signal
import socket
import threading
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from PIL import Image, ImageOps
import web
import ambient
import drawing

# NECESSARY!!!!! VVVVV
sys.path.append(os.path.abspath('/home/pi/rpi-rgb-led-matrix/bindings/python'))
from rgbmatrix import RGBMatrix, RGBMatrixOptions
# Frames are drawn in memory (so the control page can show them), then pushed to the LEDs
import frame
graphics = frame

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

# Spotify is only used in music mode (and auto mode, to notice a song starting)
SPOTIFY_POLL_SECONDS = 10
# Auto mode: music while Spotify plays (kept this long after it stops, so it doesn't flicker
# between songs), sports while an NFL game is live, otherwise normal
AUTO_MUSIC_LINGER = 60

# Music mode: album art on the right, song title and artist on the left. Text too wide for the
# left half holds for MUSIC_SCROLL_PAUSE seconds, then scrolls in a loop.
MUSIC_SCROLL_SPEED = 16  # pixels per second
MUSIC_SCROLL_PAUSE = 2.0
MUSIC_SCROLL_GAP = 12    # blank pixels between the end of the text and its repeat

# Brightness (1-100): daytime runs from 30 min before sunrise to 30 min after sunset
# These are defaults: settings changed on the control page are saved to settings.json and win.
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

# NFL scores (sports mode only). Today's games are watched for touchdowns; the sports ticker
# scrolls through the whole week's games.
NFL_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NFL_TICKER_SPEED = 40        # pixels per second
# Sports mode: loops through all of this week's games (finished, live and upcoming)
SPORTS_GAME_SECONDS = 4.0    # hold on each game (the favorite team's game holds 1.5x as long)
SPORTS_CHECK_SECONDS = 15    # how often the week's scores are checked for being out of date
# Sleeper fantasy matchups, shown after the NFL games in sports mode. The username is a setting
# on the control page; this is the default.
SLEEPER_USERNAME = "odehamer"
SLEEPER_API = "https://api.sleeper.app/v1/"
FANTASY_SECONDS = 5.0           # hold on each matchup in the sports ticker
FANTASY_PICTURE_SIZE = 14       # team pictures are scaled to this many pixels square
FANTASY_LEAGUE_CACHE = 6 * 3600 # re-read league names, rosters and team names this often
NFL_UPCOMING_GAP = 12        # space either side of the "@" so upcoming games are as wide as played ones
NFL_TICKER_FRAME = 0.03      # seconds per frame while the ticker runs
NFL_POLL_SECONDS = 600       # how often to refresh scores when nothing is happening
NFL_LIVE_POLL_SECONDS = 60   # refresh rate while a game is in progress or about to kick off
NFL_KICKOFF_WINDOW = 600     # start the fast refresh this many seconds before a kickoff
NFL_LOGO_SIZE = 16           # team logos are scaled to this many pixels square
NFL_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
TD_FLASH_SECONDS = 2.4       # "TOUCHDOWN" flashes this long before the play details scroll
TD_ALERT_MAX_AGE = 600       # drop touchdown alerts that couldn't be shown within this many seconds
TD_TRY_WAIT_SECONDS = 120    # wait up to this long for the extra point / two-point try before alerting

# Favorite team (ESPN abbreviation): its game leads the ticker, holds longer and is underlined,
# and its touchdowns get a longer alert with a flashing border
FAVORITE_TEAM = "ATL"
FAVORITE_TD_FLASH_SECONDS = 4.0  # "TOUCHDOWN" flash length for the favorite team
TD_ALERTS_FAVORITE_ONLY = False  # True = only alert for the favorite team's touchdowns

# Phone control page: http://<pi>.local:WEB_PORT on the home network. Set WEB_PIN in .env to require a PIN.
WEB_PORT = 8080
WEB_PIN = os.getenv("WEB_PIN", "")
NFL_GAME_GAP = 7             # blank pixels between games
NFL_LOGO_GAP = 1             # pixels between a logo and its score
NFL_TEAM_GAP = 4             # pixels between the away score and the home logo

# Live settings: defaults from the constants above, overridden by settings.json (written by the
# control page). The display loop reads `settings` every frame, so changes apply immediately.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
NFL_TEAMS = [
    ("ARI", "Arizona Cardinals"), ("ATL", "Atlanta Falcons"), ("BAL", "Baltimore Ravens"),
    ("BUF", "Buffalo Bills"), ("CAR", "Carolina Panthers"), ("CHI", "Chicago Bears"),
    ("CIN", "Cincinnati Bengals"), ("CLE", "Cleveland Browns"), ("DAL", "Dallas Cowboys"),
    ("DEN", "Denver Broncos"), ("DET", "Detroit Lions"), ("GB", "Green Bay Packers"),
    ("HOU", "Houston Texans"), ("IND", "Indianapolis Colts"), ("JAX", "Jacksonville Jaguars"),
    ("KC", "Kansas City Chiefs"), ("LV", "Las Vegas Raiders"), ("LAC", "Los Angeles Chargers"),
    ("LAR", "Los Angeles Rams"), ("MIA", "Miami Dolphins"), ("MIN", "Minnesota Vikings"),
    ("NE", "New England Patriots"), ("NO", "New Orleans Saints"), ("NYG", "New York Giants"),
    ("NYJ", "New York Jets"), ("PHI", "Philadelphia Eagles"), ("PIT", "Pittsburgh Steelers"),
    ("SF", "San Francisco 49ers"), ("SEA", "Seattle Seahawks"), ("TB", "Tampa Bay Buccaneers"),
    ("TEN", "Tennessee Titans"), ("WSH", "Washington Commanders"),
]
SETTINGS_FIELDS = [
    {"key": "display_mode", "section": "Mode", "label": "Display mode", "type": "choice",
     "options": [["normal", "Clock, weather & buses"], ["music", "Music only"], ["sports", "Football scores"],
                 ["ambient", "Ambient"], ["drawing", "Drawing"], ["auto", "Auto"]]},
    {"key": "ambient_scene", "section": "Mode", "label": "Ambient scene", "type": "choice",
     "options": [["aquarium", "Aquarium"], ["fireplace", "Fireplace"], ["rain", "Rain on the window"]]},
    {"key": "day_brightness", "section": "Screen", "label": "Daytime brightness", "type": "range", "min": 1, "max": 100},
    {"key": "night_brightness", "section": "Screen", "label": "Night brightness", "type": "range", "min": 1, "max": 100},
    {"key": "screen_off_enabled", "section": "Screen", "label": "Turn the screen off overnight", "type": "toggle"},
    {"key": "screen_off_start", "section": "Screen", "label": "Screen off at", "type": "hour"},
    {"key": "screen_off_end", "section": "Screen", "label": "Screen back on at", "type": "hour"},
    {"key": "auto_sports", "section": "Football", "label": "Auto mode shows sports for", "type": "choice",
     "options": [["any", "Any live NFL game"], ["favorite", "Favorite team's games"], ["never", "Never"]]},
    {"key": "fantasy_enabled", "section": "Football", "label": "Fantasy matchups in sports mode", "type": "toggle"},
    {"key": "sleeper_username", "section": "Football", "label": "Sleeper username", "type": "text",
     "max_length": 40, "placeholder": "Sleeper username"},
    {"key": "favorite_team", "section": "Football", "label": "Favorite team", "type": "choice",
     "options": [["", "None"]] + [list(team) for team in NFL_TEAMS]},
    {"key": "touchdown_alerts", "section": "Football", "label": "Touchdown alerts (sports mode)", "type": "choice",
     "options": [["all", "All teams"], ["favorite", "Favorite team only"], ["off", "Off"]]},
]
DEFAULT_SETTINGS = {
    "display_mode": "normal",
    "ambient_scene": "aquarium",
    "day_brightness": DAY_BRIGHTNESS,
    "night_brightness": NIGHT_BRIGHTNESS,
    "screen_off_enabled": SCREEN_OFF_START_HOUR != SCREEN_OFF_END_HOUR,
    "screen_off_start": SCREEN_OFF_START_HOUR,
    "screen_off_end": SCREEN_OFF_END_HOUR,
    "fantasy_enabled": True,
    "auto_sports": "any",
    "sleeper_username": SLEEPER_USERNAME,
    "favorite_team": FAVORITE_TEAM,
    "touchdown_alerts": "favorite" if TD_ALERTS_FAVORITE_ONLY else "all",
}
settings_lock = threading.Lock()
RETIRED_SETTINGS = {"spotify_enabled", "nfl_ticker_enabled"}  # older settings.json files may still have these; skipped quietly


def validate_setting(key, value):
    # Returns an error message, or None if the value is allowed
    field = next((f for f in SETTINGS_FIELDS if f["key"] == key), None)
    if field is None:
        return f"Unknown setting {key!r}"
    kind = field["type"]
    if kind in ("range", "hour"):
        low, high = (field["min"], field["max"]) if kind == "range" else (0, 23)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            return f"{field['label']} must be a whole number from {low} to {high}"
    elif kind == "toggle":
        if not isinstance(value, bool):
            return f"{field['label']} must be on or off"
    elif kind == "choice":
        if value not in [option[0] for option in field["options"]]:
            return f"{field['label']} isn't one of the choices"
    elif kind == "text":
        # Sleeper usernames are letters, numbers and _ . - only
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,%d}" % field["max_length"], value):
            return f"{field['label']} can only use letters, numbers, _ . and -"
    return None


def load_settings():
    # Defaults, overridden by any valid values saved in settings.json
    loaded = {}
    try:
        with open(SETTINGS_PATH) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in RETIRED_SETTINGS:
                    continue
                if key == "display_mode" and value == "aquarium":
                    # Aquarium mode became one of the ambient scenes
                    value = "ambient"
                    loaded.setdefault("ambient_scene", "aquarium")
                if validate_setting(key, value) is None:
                    loaded[key] = value
                else:
                    print(f"Ignoring invalid saved setting {key}={value!r}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Couldn't read {SETTINGS_PATH}, using defaults: {e}")
    return {**DEFAULT_SETTINGS, **loaded}


def get_settings():
    return dict(settings)


def update_settings(changes):
    # Called from the web server; returns (new settings, None) or (None, error message)
    global settings
    if not isinstance(changes, dict) or not changes:
        return None, "No settings to change"
    for key, value in changes.items():
        error = validate_setting(key, value)
        if error:
            return None, error
    with settings_lock:
        new_settings = {**settings, **changes}
        # Write to a temp file and swap it in, so a power cut never leaves a half-written file
        try:
            with open(SETTINGS_PATH + ".tmp", "w") as f:
                json.dump(new_settings, f, indent=2)
            os.replace(SETTINGS_PATH + ".tmp", SETTINGS_PATH)
        except OSError as e:
            print(f"Couldn't save settings (still applied until restart): {e}")
        settings = new_settings
    print("Settings changed: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    if "sleeper_username" in changes:
        reset_fantasy()
    if "display_mode" in changes or "fantasy_enabled" in changes or "sleeper_username" in changes:
        nfl_week_wakeup.set()
    if "display_mode" in changes or "auto_sports" in changes:
        spotify_wakeup.set()
    return dict(new_settings), None


settings = load_settings()


spotify_last_playing = float("-inf")  # time.monotonic() when Spotify last reported a song playing


def current_mode():
    # The mode on screen: the chosen one, or for auto, whichever fits right now
    mode = settings["display_mode"]
    if mode != "auto":
        return mode
    if spotify_album_art is not None and time.monotonic() - spotify_last_playing < AUTO_MUSIC_LINGER:
        return "music"
    sports_for = settings["auto_sports"]
    if sports_for == "any" and nfl_has_live:
        return "sports"
    if sports_for == "favorite" and any(g["state"] == "in" and is_favorite(g) for g in nfl_games):
        return "sports"
    return "normal"


def is_favorite(game):
    team = settings["favorite_team"]
    return bool(team) and team in (game["away"]["abbr"], game["home"]["abbr"])


# Configure the LED matrix
options = RGBMatrixOptions()
options.rows = 32
options.cols = 64
options.chain_length = 1
options.parallel = 1
options.hardware_mapping = 'regular'
options.gpio_slowdown = 2
options.brightness = settings["day_brightness"]
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
nfl_next_kickoff = None  # UTC datetime of today's next game that hasn't started
nfl_last_summary = ""  # last logged NFL status, so unchanged refreshes stay quiet
nfl_week_games = []  # this week's games for sports mode, including ones that haven't started
nfl_week_has_live = False
nfl_week_fetched_at = float("-inf")
nfl_week_summary = ""
fantasy_matchups = []  # this week's Sleeper matchups for the sleeper_username setting
fantasy_fetched_at = float("-inf")
fantasy_summary = ""
fantasy_user_id = None
fantasy_leagues = {"fetched_at": float("-inf"), "season": None, "leagues": []}
fantasy_pictures = {}  # picture URL -> image (RGB, FANTASY_PICTURE_SIZE square)
spotify_wakeup = threading.Event()  # set when the display mode changes, so music mode loads right away
nfl_week_wakeup = threading.Event()  # set when the display mode changes, so sports mode loads right away
# Per game: total points when first seen (older plays are never alerted), total covered by the
# last summary fetch, and scoring play ids already handled
nfl_score_tracking = {}
nfl_td_alerts = deque()  # touchdowns waiting to be shown
nfl_logos = {}  # team abbreviation -> logo image (RGB, NFL_LOGO_SIZE square)
current_temperature = 0
weather_loaded = False  # the clock leaves the temperature out until the first weather update
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
clock_font = graphics.Font()
clock_font.LoadFont("/home/pi/bustracker/fonts/10x20.bdf")
date_font = graphics.Font()
date_font.LoadFont("/home/pi/bustracker/fonts/5x8.bdf")
default_color = graphics.Color(255, 255, 0)
bus_text_color = graphics.Color(214, 11, 180)
nfl_label_color = graphics.Color(213, 10, 10)
nfl_score_color = graphics.Color(255, 255, 255)
nfl_loser_score_color = graphics.Color(120, 120, 120)
nfl_status_color = graphics.Color(43, 224, 227)
gold_color = graphics.Color(255, 190, 0)
date_color = graphics.Color(43, 224, 227)

sun_color = graphics.Color(255, 225, 0)

# Sources that are currently failing -> number of failed attempts so far
failing_sources = {}


def log_error(source, error):
    # Log the first failure in a run, then stay quiet while it keeps retrying
    if source not in failing_sources:
        print(f"{source} error: {error} (retrying quietly until it recovers)")
    failing_sources[source] = failing_sources.get(source, 0) + 1


def log_ok(source):
    # Log once when a failing source starts working again
    attempts = failing_sources.pop(source, None)
    if attempts is not None:
        print(f"{source} OK again after {attempts} failed attempt{'s' if attempts != 1 else ''}")


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
        log_ok("Bus data")
    except Exception as e:
        log_error("Bus data", e)
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
    global weather_loaded
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

        current_temperature = float(data['main']['temp'])
        weather_loaded = True
        weather_code = data['weather'][0]['id']
        
        sunrise_str = data['sys']['sunrise']
        sunset_str = data['sys']['sunset']

        sunrise_time = datetime.fromtimestamp(sunrise_str)
        sunset_time = datetime.fromtimestamp(sunset_str)

        print(f"Weather: {current_temperature:.0f}F, code {weather_code}, "
              f"sunrise {sunrise_time:%H:%M}, sunset {sunset_time:%H:%M}")
        log_ok("Weather")
        
    except Exception as e:
        log_error("Weather", e)
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
        if expected != rain_expected or "Rain forecast" in failing_sources:
            print(f"Rain forecast: {'rain' if expected else 'no rain'} expected in the next {RAIN_LOOKAHEAD_HOURS}h")
        rain_expected = expected
        log_ok("Rain forecast")
    except Exception as e:
        log_error("Rain forecast", e)
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


def parse_nfl_games(data, include_upcoming=False):
    games = []
    for event in data.get("events", []):
        status = event.get("status", {}).get("type", {})
        state = status.get("state")
        # pre = not started yet; the ticker only shows games in progress or finished
        if state not in ("in", "post") and not (include_upcoming and state == "pre"):
            continue
        competition = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in competition.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue
        # Shown in small text above the scores: "FINAL", "FINAL/OT", "8:42 3RD", "HALFTIME"
        status_text = (status.get("shortDetail") or "").upper().replace(" - ", " ")
        if state == "pre" and event.get("date"):
            # Upcoming: kickoff day and local time, e.g. "SUN 10:00"
            kickoff = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone()
            status_text = f"{kickoff:%a} {kickoff.hour % 12 or 12}:{kickoff:%M}".upper()
        game = {"id": event.get("id"), "state": state, "status": status_text}
        # Live games: which team has the ball (ESPN gives its team id) and whether it's in the red zone
        situation = competition.get("situation") or {}
        game["possession"] = next((side for side in ("away", "home")
                                   if str((sides[side].get("team") or {}).get("id")) == str(situation.get("possession"))), None)
        game["red_zone"] = bool(situation.get("isRedZone"))
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


def touchdown_try_done(play, points_added):
    # ESPN adds the try result in parentheses once it's played, e.g. "(Wil Lutz Kick)" or
    # "(Two-Point Conversion Failed)"; a 7 or 8 point jump also means the try is already counted
    return "(" in (play.get("text") or "") or points_added >= 7


def queue_touchdown_alert(game, play):
    # Touchdown alerts only show in sports mode; in other modes the play is just marked as seen
    if current_mode() != "sports":
        return
    abbr = (play.get("team") or {}).get("abbreviation", "")
    teams = {game[side]["abbr"]: game[side] for side in ("away", "home")}
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
        "favorite": bool(abbr) and abbr == settings["favorite_team"],
    })
    print(f"Touchdown {abbr}: {description} ({team_score}-{opponent_score})")


def check_for_touchdowns(games):
    # The scoreboard only has totals; when a game's total goes up (or a touchdown is waiting on its
    # extra point), fetch that game's scoring plays
    for game in games:
        if not game["id"]:
            continue
        total = to_int(game["away"]["score"]) + to_int(game["home"]["score"])
        track = nfl_score_tracking.get(game["id"])
        if track is None:
            nfl_score_tracking[game["id"]] = {"baseline": total, "checked_total": total, "seen": set(),
                                              "pending": {}}
            continue
        if total <= track["checked_total"] and not track["pending"]:
            continue
        try:
            response = requests.get(NFL_SUMMARY_URL, params={"event": game["id"]}, timeout=10)
            response.raise_for_status()
            plays = response.json().get("scoringPlays", [])
        except Exception as e:
            log_error("NFL scoring plays", e)
            continue  # try again on the next refresh
        log_ok("NFL scoring plays")

        now = time.monotonic()
        latest_total = track["checked_total"]
        previous = {"away": 0, "home": 0}
        for play in plays:
            scores = {"away": to_int(play.get("awayScore")), "home": to_int(play.get("homeScore"))}
            play_total = scores["away"] + scores["home"]
            points_added = max(scores["away"] - previous["away"], scores["home"] - previous["home"])
            previous = scores
            # The summary can lag the scoreboard; only count what it actually includes
            latest_total = max(latest_total, play_total)
            play_id = play.get("id")

            if play_id in track["pending"]:
                # Waiting on the try: alert once it's played, the game is over, or we've waited long enough
                if (touchdown_try_done(play, points_added) or game["state"] == "post"
                        or now - track["pending"][play_id] >= TD_TRY_WAIT_SECONDS):
                    del track["pending"][play_id]
                    queue_touchdown_alert(game, play)
                continue
            if play_id in track["seen"]:
                continue
            track["seen"].add(play_id)
            if play_total <= track["baseline"]:
                continue  # scored before we started watching this game
            if (play.get("scoringType") or {}).get("name") != "touchdown":
                continue
            abbr = (play.get("team") or {}).get("abbreviation", "")
            alerts = settings["touchdown_alerts"]
            if alerts == "off" or (alerts == "favorite" and abbr != settings["favorite_team"]):
                continue
            if touchdown_try_done(play, points_added) or game["state"] == "post":
                queue_touchdown_alert(game, play)
            else:
                track["pending"][play_id] = now
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
                log_ok(f"NFL logo {team['abbr']}")
            except Exception as e:
                log_error(f"NFL logo {team['abbr']}", e)


def get_nfl_scores():
    global nfl_games
    global nfl_has_live
    global nfl_next_kickoff
    global nfl_last_summary
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
        nfl_has_live = any(g["state"] == "in" for g in games)
        nfl_next_kickoff = min(kickoffs) if kickoffs else None
        log_ok("NFL scores")
        if not data.get("events"):
            summary = f"NFL: no games on {day}"
        else:
            live = sum(1 for g in games if g["state"] == "in")
            kickoff = f", next kickoff {nfl_next_kickoff.astimezone():%H:%M}" if nfl_next_kickoff else ""
            summary = f"NFL: {len(data['events'])} games on {day}, {live} live, {len(games) - live} finished{kickoff}"
        if summary != nfl_last_summary:
            print(summary)
            nfl_last_summary = summary
    except Exception as e:
        log_error("NFL scores", e)
        # Keep the last known scores on error


def get_nfl_week():
    # The scoreboard without a date is the current week: Thursday through Monday night
    global nfl_week_games, nfl_week_has_live, nfl_week_fetched_at, nfl_week_summary
    try:
        response = requests.get(NFL_SCOREBOARD_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        games = parse_nfl_games(data, include_upcoming=True)
        load_missing_nfl_logos(games)
        nfl_week_games = games
        nfl_week_has_live = any(g["state"] == "in" for g in games)
        nfl_week_fetched_at = time.monotonic()
        log_ok("NFL week")
        counts = {state: sum(1 for g in games if g["state"] == state) for state in ("post", "in", "pre")}
        summary = (f"NFL week {data.get('week', {}).get('number', '?')}: {counts['post']} final, "
                   f"{counts['in']} live, {counts['pre']} upcoming")
        if summary != nfl_week_summary:
            print(summary)
            nfl_week_summary = summary
    except Exception as e:
        log_error("NFL week", e)


def sleeper_get(path):
    response = requests.get(SLEEPER_API + path, timeout=10)
    response.raise_for_status()
    return response.json()


def load_fantasy_leagues(season):
    # League names, which roster is ours and every team's name and picture. Cached because
    # these barely change during a week.
    leagues = []
    for league in sleeper_get(f"user/{fantasy_user_id}/leagues/nfl/{season}") or []:
        league_id = league.get("league_id")
        rosters = sleeper_get(f"league/{league_id}/rosters") or []
        users = {u.get("user_id"): u for u in sleeper_get(f"league/{league_id}/users") or []}
        # Only teams we own; leagues where we're just a co-manager are skipped
        mine = next((r for r in rosters if r.get("owner_id") == fantasy_user_id), None)
        if mine is None:
            continue
        names, pictures = {}, {}
        for roster in rosters:
            owner = users.get(roster.get("owner_id")) or {}
            metadata = owner.get("metadata") or {}
            names[roster.get("roster_id")] = (metadata.get("team_name") or owner.get("display_name")
                                              or f"Team {roster.get('roster_id')}")
            # The team's own picture for this league, else the owner's Sleeper avatar
            pictures[roster.get("roster_id")] = metadata.get("avatar") or (
                f"https://sleepercdn.com/avatars/thumbs/{owner['avatar']}" if owner.get("avatar") else "")
        leagues.append({"id": league_id, "name": league.get("name") or "League",
                        "roster_id": mine.get("roster_id"), "names": names, "pictures": pictures})
    return leagues


def load_fantasy_picture(url):
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    picture = Image.open(io.BytesIO(response.content)).convert("RGBA")
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    picture = ImageOps.fit(picture, (FANTASY_PICTURE_SIZE, FANTASY_PICTURE_SIZE), method=resample)
    # Flatten any transparency onto black
    tile = Image.new("RGB", picture.size, (0, 0, 0))
    tile.paste(picture, (0, 0), picture)
    return tile


def load_missing_fantasy_pictures(matchups):
    # Pictures rarely change, so each one is downloaded once per run
    for matchup in matchups:
        for url in (matchup["my_picture"], matchup["opponent_picture"]):
            if not url or url in fantasy_pictures:
                continue
            try:
                fantasy_pictures[url] = load_fantasy_picture(url)
                log_ok("Fantasy picture")
            except Exception as e:
                log_error("Fantasy picture", e)


def reset_fantasy():
    # Forget the old user's leagues and matchups so a new username loads from scratch
    global fantasy_matchups, fantasy_fetched_at, fantasy_user_id
    fantasy_matchups = []
    fantasy_fetched_at = float("-inf")
    fantasy_user_id = None
    fantasy_leagues.update(fetched_at=float("-inf"), season=None, leagues=[])


def get_fantasy_matchups():
    global fantasy_matchups, fantasy_fetched_at, fantasy_summary, fantasy_user_id
    username = settings["sleeper_username"]
    try:
        if fantasy_user_id is None:
            user = sleeper_get(f"user/{username}")
            if not user:
                raise ValueError(f"no Sleeper user named {username!r}")
            fantasy_user_id = user["user_id"]
        state = sleeper_get("state/nfl")
        season, week = state.get("season"), state.get("display_week") or state.get("week")
        if (fantasy_leagues["season"] != season
                or time.monotonic() - fantasy_leagues["fetched_at"] > FANTASY_LEAGUE_CACHE):
            fantasy_leagues.update(leagues=load_fantasy_leagues(season), season=season,
                                   fetched_at=time.monotonic())
        matchups = []
        for league in fantasy_leagues["leagues"]:
            teams = sleeper_get(f"league/{league['id']}/matchups/{week}") or []
            me = next((t for t in teams if t.get("roster_id") == league["roster_id"]), None)
            if me is None:
                continue
            opponent = next((t for t in teams if t.get("matchup_id") is not None
                             and t.get("matchup_id") == me.get("matchup_id")
                             and t.get("roster_id") != me.get("roster_id")), None)
            matchups.append({
                "league": league["name"],
                "my_name": league["names"].get(me.get("roster_id"), username),
                "my_points": float(me.get("points") or 0),
                "opponent_name": league["names"].get(opponent.get("roster_id"), "?") if opponent else "BYE",
                "opponent_points": float(opponent.get("points") or 0) if opponent else None,
                "my_picture": league["pictures"].get(me.get("roster_id"), ""),
                "opponent_picture": league["pictures"].get(opponent.get("roster_id"), "") if opponent else "",
            })
        load_missing_fantasy_pictures(matchups)
        if username != settings["sleeper_username"]:
            return  # the username changed while loading; the next refresh loads the new one
        fantasy_matchups = matchups
        fantasy_fetched_at = time.monotonic()
        log_ok("Fantasy")
        summary = f"Fantasy week {week}: {len(matchups)} matchups for {username}"
        if summary != fantasy_summary:
            print(summary)
            fantasy_summary = summary
    except Exception as e:
        log_error("Fantasy", e)


def nfl_week_loop():
    # Loaded once at startup so switching to sports mode is instant, then refreshed while sports mode
    # is on: every minute during live games, otherwise every 10
    while True:
        sports = current_mode() == "sports"
        refresh_seconds = NFL_LIVE_POLL_SECONDS if nfl_week_has_live else NFL_POLL_SECONDS
        week_age = time.monotonic() - nfl_week_fetched_at
        if week_age == float("inf") or (sports and week_age >= refresh_seconds):
            get_nfl_week()
        # Fantasy points move with the real games, so they refresh on the same schedule
        fantasy_age = time.monotonic() - fantasy_fetched_at
        if settings["fantasy_enabled"] and (fantasy_age == float("inf") or (sports and fantasy_age >= refresh_seconds)):
            get_fantasy_matchups()
        nfl_week_wakeup.wait(SPORTS_CHECK_SECONDS)
        nfl_week_wakeup.clear()


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


def schedule_nfl_update():
    get_nfl_scores()
    timer = threading.Timer(get_nfl_poll_seconds(), schedule_nfl_update)
    timer.daemon = True
    timer.start()


# Ticker rows: small status text on top, then logos and scores below it
NFL_STATUS_BASELINE = 7
NFL_LOGO_TOP = 10
NFL_SCORE_BASELINE = 23


def build_nfl_ticker(games, hold_seconds, matchups=()):
    # Lay the ticker out once as segments (text or logo) at fixed x offsets
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

    def add_team(game, side):
        # Logo with a possession marker above it during live games;
        # falls back to the abbreviation if the logo couldn't be downloaded
        nonlocal x
        team = game[side]
        logo = nfl_logos.get(team["abbr"])
        if logo is None:
            add_text(team["abbr"], graphics.Color(*team["color"]))
            add_text(" ")
            return
        segments.append({"x": x, "width": NFL_LOGO_SIZE, "kind": "image", "image": logo})
        segments.append({"x": x, "width": NFL_LOGO_SIZE, "kind": "possession", "game_id": game["id"], "side": side})
        x += NFL_LOGO_SIZE + NFL_LOGO_GAP

    def add_gap():
        nonlocal x
        x += NFL_GAME_GAP

    stops = []
    if games:
        add_text("NFL", nfl_label_color, wait_time_font)
    # Favorite team's game first; the rest keep ESPN's order
    for game in sorted(games, key=lambda g: not is_favorite(g)):
        add_gap()
        game_start = x
        if game["state"] == "pre":
            # Not started: away logo @ home logo, kickoff time above
            add_team(game, "away")
            x += NFL_UPCOMING_GAP - NFL_LOGO_GAP
            segments.append({"x": x, "width": text_width("@", nfl_status_font), "kind": "text", "text": "@",
                             "color": nfl_loser_score_color, "font": nfl_status_font, "y": NFL_SCORE_BASELINE - 3})
            x += text_width("@", nfl_status_font) + NFL_UPCOMING_GAP
            add_team(game, "home")
            x -= NFL_LOGO_GAP
        else:
            for i, side in enumerate(("away", "home")):
                team = game[side]
                add_team(game, side)
                losing = game["state"] == "post" and not team["winner"]
                add_text(team["score"], nfl_loser_score_color if losing else nfl_score_color)
                if i == 0:
                    x += NFL_TEAM_GAP
        # Status ("F", "8:42 3RD", "HALFTIME") in small text centered above the game
        status_width = text_width(game["status"], nfl_status_font)
        segments.append({"x": game_start + (x - game_start - status_width) // 2, "width": status_width,
                         "kind": "text", "text": game["status"], "color": nfl_status_color,
                         "font": nfl_status_font, "y": NFL_STATUS_BASELINE})
        if is_favorite(game):
            # Underline the favorite's game in its team color
            favorite = game["away"] if game["away"]["abbr"] == settings["favorite_team"] else game["home"]
            segments.append({"x": game_start, "width": x - game_start, "kind": "underline",
                             "color": favorite["color"]})
        # Scroll distance at which this game sits centered on screen, and how long to hold there
        hold = hold_seconds * (1.5 if is_favorite(game) else 1)
        stops.append((options.cols + game_start - (options.cols - (x - game_start)) // 2, hold))
    if matchups:
        # Fantasy matchups: league name on top, each team's picture with its score underneath
        if games:
            add_gap()
        add_text("FANTASY", gold_color, wait_time_font)
        column = 31
        for matchup in matchups:
            add_gap()
            card_start = x
            league = to_display_text(matchup["league"]).strip(" -").upper() or "FANTASY"
            segments.append({"x": card_start, "width": 2 * column, "kind": "marquee", "text": league,
                             "color": gold_color, "font": nfl_status_font, "y": NFL_STATUS_BASELINE,
                             "stop": len(stops)})
            mine, theirs = matchup["my_points"], matchup["opponent_points"]
            sides = [(matchup["my_picture"], mine, theirs is None or mine >= theirs),
                     (matchup["opponent_picture"], theirs, theirs is not None and theirs >= mine)]
            for i, (url, points, leading) in enumerate(sides):
                column_start = card_start + i * column
                picture = fantasy_pictures.get(url)
                if picture is not None:
                    segments.append({"x": column_start + (column - FANTASY_PICTURE_SIZE) // 2,
                                     "width": FANTASY_PICTURE_SIZE, "kind": "image", "image": picture, "y": 9})
                score = "BYE" if points is None else f"{points:.1f}"
                score_width = text_width(score, arrival_time_font)
                segments.append({"x": column_start + (column - score_width) // 2, "width": score_width,
                                 "kind": "text", "text": score, "font": arrival_time_font, "y": 31,
                                 "color": nfl_score_color if leading else nfl_loser_score_color})
            x = card_start + 2 * column
            stops.append((options.cols + card_start - (options.cols - 2 * column) // 2,
                          FANTASY_SECONDS))
    # When each stop is reached, so a long league name starts scrolling once its card is centered
    arrivals, elapsed, scrolled = [], 0.0, 0
    for stop, hold in stops:
        elapsed += (stop - scrolled) / NFL_TICKER_SPEED
        arrivals.append(elapsed)
        elapsed += hold
        scrolled = stop
    return {"segments": segments, "width": x, "stops": stops, "arrivals": arrivals}


def get_nfl_ticker_scroll(ticker, elapsed):
    # Pixels scrolled after `elapsed` seconds: move at NFL_TICKER_SPEED, holding at each stop
    scrolled = 0.0
    for stop, pause in ticker["stops"]:
        travel = (stop - scrolled) / NFL_TICKER_SPEED
        if elapsed < travel:
            return scrolled + elapsed * NFL_TICKER_SPEED
        elapsed -= travel
        scrolled = stop
        if elapsed < pause:
            return scrolled
        elapsed -= pause
    return scrolled + elapsed * NFL_TICKER_SPEED


def draw_clipped_marquee(canvas, segment, left, elapsed):
    # Text centered in its area if it fits; otherwise it scrolls in a loop, drawing only the
    # characters that are fully inside the area so it never spills onto neighbors
    font, text = segment["font"], segment["text"]
    widths = [max(0, font.CharacterWidth(ord(c))) for c in text]
    width = sum(widths)
    if width <= segment["width"]:
        graphics.DrawText(canvas, font, left + (segment["width"] - width) // 2, segment["y"], segment["color"], text)
        return
    loop = width + MUSIC_SCROLL_GAP
    period = MUSIC_SCROLL_PAUSE + loop / MUSIC_SCROLL_SPEED
    shift = int(max(0.0, elapsed % period - MUSIC_SCROLL_PAUSE) * MUSIC_SCROLL_SPEED)
    right = left + segment["width"]
    for start in (left - shift, left - shift + loop):
        char_x = start
        for char, char_width in zip(text, widths):
            if char_x >= left and char_x + char_width <= right:
                graphics.DrawText(canvas, font, char_x, segment["y"], segment["color"], char)
            char_x += char_width


def draw_possession_marker(canvas, segment, left, live_games):
    # A short bar above the logo of the team with the ball: white, or red in the red zone. Read from
    # the latest scores every frame, so it doesn't wait for the ticker to loop around.
    game = live_games.get(segment["game_id"])
    if not game or game["possession"] != segment["side"]:
        return
    color = (220, 30, 50) if game["red_zone"] else (255, 255, 255)
    center = left + NFL_LOGO_SIZE // 2
    for x in range(center - 3, center + 3):
        for y in (8, 9):
            canvas.SetPixel(x, y, *color)


def draw_nfl_ticker(canvas, ticker, elapsed):
    # Returns False once everything has scrolled fully off the left edge
    offset = options.cols - int(get_nfl_ticker_scroll(ticker, elapsed))
    if offset + ticker["width"] < 0:
        return False
    live_games = {g["id"]: g for g in nfl_week_games if g["state"] == "in"}
    for segment in ticker["segments"]:
        left = offset + segment["x"]
        # Only draw segments that are at least partly on screen
        if left >= options.cols or left + segment["width"] < 0:
            continue
        if segment["kind"] == "text":
            graphics.DrawText(canvas, segment["font"], left, segment["y"], segment["color"], segment["text"])
        elif segment["kind"] == "image":
            canvas.SetImage(segment["image"], left, segment.get("y", NFL_LOGO_TOP))
        elif segment["kind"] == "marquee":
            draw_clipped_marquee(canvas, segment, left, max(0.0, elapsed - ticker["arrivals"][segment["stop"]]))
        elif segment["kind"] == "possession":
            draw_possession_marker(canvas, segment, left, live_games)
        elif segment["kind"] == "underline":
            for x in range(max(0, left), min(options.cols, left + segment["width"])):
                canvas.SetPixel(x, options.rows - 2, *segment["color"])
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
    flash_seconds = FAVORITE_TD_FLASH_SECONDS if alert.get("favorite") else TD_FLASH_SECONDS
    return {**alert, "team_color": color, "parts": parts, "text_width": width(scorer) + width(rest),
            "flash_seconds": flash_seconds}


def draw_td_alert(canvas, alert, elapsed):
    # Returns False once the alert has finished
    if elapsed < alert["flash_seconds"]:
        # Flash "TOUCHDOWN" between the team color and white
        team_phase = int(elapsed / 0.3) % 2 == 0
        flash_color = alert["team_color"] if team_phase else nfl_score_color
        if alert.get("favorite"):
            # Narrower bold font so the word fits inside the border
            graphics.DrawText(canvas, nfl_score_font, (options.cols - 9 * 6) // 2, 21, flash_color, "TOUCHDOWN")
        else:
            graphics.DrawText(canvas, wait_time_font, 1, 21, flash_color, "TOUCHDOWN")
        if alert.get("favorite"):
            # Favorite team: border flashing opposite the text
            border = nfl_score_color if team_phase else alert["team_color"]
            rgb = (border.red, border.green, border.blue)
            for x in range(options.cols):
                canvas.SetPixel(x, 0, *rgb)
                canvas.SetPixel(x, options.rows - 1, *rgb)
            for y in range(options.rows):
                canvas.SetPixel(0, y, *rgb)
                canvas.SetPixel(options.cols - 1, y, *rgb)
        return True

    scrolled = int((elapsed - alert["flash_seconds"]) * NFL_TICKER_SPEED)
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
        log_ok("Spotify token")
        return spotify_access_token
    except Exception as e:
        log_error("Spotify token", e)
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


def spotify_active():
    # Spotify is only checked in music mode, and in auto mode to notice when a song starts
    return settings["display_mode"] in ("music", "auto")


def fetch_spotify_now_playing():
    global spotify_last_playing
    global spotify_album_art
    global spotify_album_art_url
    global spotify_is_playing
    global spotify_progress_ms
    global spotify_duration_ms
    global spotify_progress_at
    global spotify_track_name
    global spotify_artist_name

    if not spotify_active():
        # Keep the last song, so switching back to music mode shows it right away while it refreshes
        return

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
            log_ok("Spotify playback")
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
        if spotify_is_playing:
            spotify_last_playing = time.monotonic()
        log_ok("Spotify playback")
    except Exception as e:
        log_error("Spotify playback", e)


def spotify_loop():
    # Checks Spotify every SPOTIFY_POLL_SECONDS, or right away when the display mode changes
    while True:
        fetch_spotify_now_playing()
        spotify_wakeup.wait(SPOTIFY_POLL_SECONDS)
        spotify_wakeup.clear()

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
    if not settings["screen_off_enabled"]:
        return False
    start, end = settings["screen_off_start"], settings["screen_off_end"]
    if start <= end:
        return start <= now.hour < end
    # Window wraps past midnight, e.g. 23 -> 6
    return now.hour >= start or now.hour < end


def get_brightness(spotify_playing):
    # 0 means the screen should be off
    now = datetime.now()
    if in_screen_off_hours(now):
        return settings["night_brightness"] if spotify_playing else 0
    return settings["day_brightness"] if is_daytime(now) else settings["night_brightness"]


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


class ShiftedCanvas:
    # Lets the pixel-art weather icons (drawn for the left half) be drawn somewhere else
    def __init__(self, canvas, dx, dy=0):
        self.canvas, self.dx, self.dy = canvas, dx, dy

    def SetPixel(self, x, y, r, g, b):
        self.canvas.SetPixel(x + self.dx, y + self.dy, r, g, b)


def draw_weather_icon(canvas, now, is_day, blink_on):
    # If rain is on the way but it isn't raining yet, alternate the icon with an umbrella every 2s
    raining_now = weather_code < 700 and weather_code != 0
    if rain_expected and not raining_now and (now.second // 2) % 2 == 1:
        draw_umbrella(canvas)
    else:
        draw_weather(canvas, is_day, not blink_on)


def draw_clock_full(canvas, blink_on):
    # Whole-screen clock for when there are no buses: big time on top, then date, temperature
    # and the weather icon along the bottom
    temp = current_temperature
    now = datetime.now()
    canvas.Clear()
    time_str = now.strftime("%I:%M" if blink_on else "%I %M")
    time_width = sum(clock_font.CharacterWidth(ord(c)) for c in time_str)
    graphics.DrawText(canvas, clock_font, (options.cols - time_width) // 2, 16, default_color, time_str)
    graphics.DrawText(canvas, date_font, 2, 30, date_color, f"{now:%b} {now.day}")
    if not weather_loaded:
        return canvas
    temp_color = get_temp_color(temp)
    graphics.DrawText(canvas, wait_time_font, 34, 30, temp_color, f"{temp:.0f}")
    if temp < 100:
        graphics.DrawText(canvas, arrival_time_font, 47, 27, temp_color, "°")
        draw_weather_icon(ShiftedCanvas(canvas, 32), now, is_daytime(now), blink_on)
    return canvas


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
    if not weather_loaded:
        pass  # no temperature until the first weather update
    elif (temp < 100):
        graphics.DrawText(canvas, wait_time_font, 2, 30, temp_color, f"{temp:.0f}")
        graphics.DrawText(canvas, arrival_time_font, 15, 27, temp_color, "°")
        draw_weather_icon(canvas, now, is_day, blink_on)
    else:
        graphics.DrawText(canvas, wait_time_font, 5, 30, temp_color, f"{temp:.0f}")
        graphics.DrawText(canvas, arrival_time_font, 24, 27, temp_color, "°")
    return canvas


def draw_blinking_clock(canvas):
    return draw_clock_left(canvas, False)


def to_display_text(text):
    # The LED fonts are ASCII: turn "Beyoncé" into "Beyonce" and drop what can't be drawn
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(ascii_text.split())


def draw_marquee(canvas, font, text, y, color, elapsed):
    # Draw text in the left half; if it's too wide, hold, then scroll it in a loop.
    # Anything past x=32 is covered by the album art drawn afterwards.
    width = sum(font.CharacterWidth(ord(c)) for c in text)
    if width <= 30:
        graphics.DrawText(canvas, font, 1, y, color, text)
        return
    loop = width + MUSIC_SCROLL_GAP
    period = MUSIC_SCROLL_PAUSE + loop / MUSIC_SCROLL_SPEED
    shift = int(max(0.0, elapsed % period - MUSIC_SCROLL_PAUSE) * MUSIC_SCROLL_SPEED)
    graphics.DrawText(canvas, font, 1 - shift, y, color, text)
    graphics.DrawText(canvas, font, 1 - shift + loop, y, color, text)


def draw_music_screen(canvas, art, elapsed):
    # elapsed: seconds since this song started showing (restarts the scrolling for each song)
    title_color = nfl_score_color if spotify_is_playing else nfl_loser_score_color
    draw_marquee(canvas, nfl_score_font, to_display_text(spotify_track_name), 14, title_color, elapsed)
    draw_marquee(canvas, nfl_status_font, to_display_text(spotify_artist_name), 26, nfl_status_color, elapsed)
    draw_spotify_art(canvas, art)


def draw_spotify_art(canvas, art):
    canvas.SetImage(art, 32, 0)

    # 1px progress bar along the bottom of the cover, estimated between polls
    if spotify_duration_ms <= 0:
        return
    progress_ms = spotify_progress_ms
    if spotify_is_playing:
        progress_ms += (time.monotonic() - spotify_progress_at) * 1000
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

WATCHDOG_PING_SECONDS = 5  # bustracker.service restarts us if these stop for WatchdogSec


def sd_notify(message):
    # Tell systemd we're alive (Type=notify + WatchdogSec in bustracker.service). Does nothing when
    # not run by systemd.
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]  # abstract socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError:
        pass


# systemctl stop sends SIGTERM; exit cleanly so the matrix gets cleared
signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

# Initialize matrix
matrix = frame.Display(RGBMatrix(options=options))
offscreen_canvas = matrix.CreateFrameCanvas()  # Create ONCE and reuse

STARTED_AT = time.monotonic()


def show_drawing():
    # Editing the drawing from the control page puts it on the sign
    if settings["display_mode"] != "drawing":
        update_settings({"display_mode": "drawing"})


painting = drawing.Drawing(os.path.join(APP_DIR, "drawings"), options.cols, options.rows, on_edit=show_drawing)

# Control page first, so it's up within seconds of a restart even if a data source is slow
try:
    web.start_server(WEB_PORT, WEB_PIN, os.path.join(APP_DIR, "web", "index.html"),
                     SETTINGS_FIELDS, get_settings, update_settings, matrix.screen_png, current_mode, painting)
    print(f"Control page running on port {WEB_PORT}{' (PIN required)' if WEB_PIN else ''}")
except Exception as e:
    # The sign keeps working without the control page
    log_error("Control page", e)

# Data loads in the background; the display starts right away and fills in as it arrives
for loader, name in ((schedule_weather_update, "weather"), (schedule_bus_update, "buses"),
                     (schedule_nfl_update, "nfl-today"), (spotify_loop, "spotify"), (nfl_week_loop, "nfl-week")):
    threading.Thread(target=loader, name=name, daemon=True).start()
td_alert = None
td_alert_start = 0.0
music_track = None
auto_showing = None
ambient_scenes = {}  # scene name -> scene, made the first time it's shown
sports_ticker = None
sports_ticker_user = None
sports_start = 0.0
sports_last_frame = 0.0
music_start = 0.0
cycle_start = time.monotonic()
bus_screen = get_bus_screen()
has_buses = bool(arrival_times)
last_watchdog_ping = 0.0
sd_notify("READY=1")

try:
    while True:
        if time.monotonic() - last_watchdog_ping >= WATCHDOG_PING_SECONDS:
            sd_notify("WATCHDOG=1")
            last_watchdog_ping = time.monotonic()
        art = spotify_album_art
        mode = current_mode()
        if settings["display_mode"] == "auto" and mode != auto_showing:
            print(f"Auto mode: showing {mode}")
            auto_showing = mode
        music_mode = mode == "music"

        # In music mode, a song playing keeps the screen on overnight (dimmed)
        brightness = get_brightness(music_mode and spotify_is_playing and art is not None)
        if brightness == 0:
            offscreen_canvas.Clear()
            offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
            time.sleep(1)
            continue

        # Touchdowns (sports mode) interrupt right away
        if td_alert is None and nfl_td_alerts and mode == "sports":
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

        # Drawing mode: whatever's been painted from the control page
        if mode == "drawing":
            offscreen_canvas.brightness = brightness
            offscreen_canvas.SetImage(painting.image(), 0, 0)
            offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
            time.sleep(NFL_TICKER_FRAME)
            cycle_start = 0.0  # start a fresh cycle if drawing mode ends
            continue

        # Ambient mode: the aquarium, fireplace or rainy window, whichever scene is chosen
        if mode == "ambient":
            scene_name = settings["ambient_scene"]
            if scene_name not in ambient_scenes:
                ambient_scenes[scene_name] = ambient.SCENES[scene_name]()
            offscreen_canvas.brightness = brightness
            ambient_scenes[scene_name].draw(offscreen_canvas, time.monotonic())
            offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
            time.sleep(NFL_TICKER_FRAME)
            cycle_start = 0.0  # start a fresh cycle if ambient mode ends
            continue

        # Sports mode: one ticker with this week's games, then our fantasy matchups. Touchdown alerts
        # above still interrupt; the loop picks up where it left off. With nothing loaded
        # yet it falls through to the normal screens.
        week_games = nfl_week_games
        matchups = fantasy_matchups if settings["fantasy_enabled"] else []
        if mode == "sports" and (week_games or matchups):
            now = time.monotonic()
            if sports_ticker_user != settings["sleeper_username"]:
                sports_ticker, sports_ticker_user = None, settings["sleeper_username"]
            if sports_ticker is None:
                sports_ticker = build_nfl_ticker(week_games, hold_seconds=SPORTS_GAME_SECONDS, matchups=matchups)
                sports_start = now
            elif now - sports_last_frame > 0.5:
                sports_start += now - sports_last_frame  # resume after an interruption
            sports_last_frame = now
            offscreen_canvas.brightness = brightness
            offscreen_canvas.Clear()
            if not draw_nfl_ticker(offscreen_canvas, sports_ticker, now - sports_start):
                sports_ticker = None  # finished a loop: rebuild next frame with the latest scores
            offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
            time.sleep(NFL_TICKER_FRAME)
            cycle_start = 0.0  # start a fresh cycle if sports mode ends
            continue
        sports_ticker = None

        # Music mode: song title and artist on the left, album art on the right. With nothing
        # loaded in Spotify it falls through to the normal clock and bus screens.
        if music_mode and art is not None:
            track = (spotify_track_name, spotify_artist_name)
            if track != music_track:
                music_track = track
                music_start = time.monotonic()
            offscreen_canvas.brightness = brightness
            offscreen_canvas.Clear()
            draw_music_screen(offscreen_canvas, art, time.monotonic() - music_start)
            offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
            time.sleep(NFL_TICKER_FRAME)
            cycle_start = 0.0  # start a fresh cycle if music mode ends
            continue

        elapsed = time.monotonic() - cycle_start
        if elapsed >= bus_screen["cycle_seconds"] or (not has_buses and arrival_times):
            # Restart the cycle on a fresh snapshot (right away when buses first show up)
            cycle_start = time.monotonic()
            bus_screen = get_bus_screen()
            has_buses = bool(arrival_times)
            elapsed = 0

        stale = bus_data_is_stale()
        offscreen_canvas.brightness = brightness
        offscreen_canvas.Clear()
        # Normal mode: clock on the left and buses on the right, or a full-screen clock with no buses
        if has_buses:
            draw_clock_left(offscreen_canvas, datetime.now().second % 2 == 0)
            draw_bus(offscreen_canvas, bus_screen, elapsed, 32)
        else:
            draw_clock_full(offscreen_canvas, datetime.now().second % 2 == 0)
        if stale:
            draw_stale_dot(offscreen_canvas, 32)
        offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
        time.sleep(FRAME_INTERVAL)

except KeyboardInterrupt:
    print("Exiting...")
finally:
    matrix.Clear()
    del matrix
