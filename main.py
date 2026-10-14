# sudo /home/pi/bustracker/.venv/bin/python main.py 
import requests
from datetime import datetime, timedelta, timezone
import time
import math
import io
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

# Bus cycle: wait times while "<route> BUS TRACKER" scrolls, then arrival times
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

arrival_times = []  # (arrival datetime, route id), sorted by time
last_bus_update = None  # time.monotonic() of the last successful bus fetch
rain_expected = False
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
date_font = graphics.Font()
date_font.LoadFont("/home/pi/bustracker/fonts/5x8.bdf")
default_color = graphics.Color(255, 255, 0)
bus_text_color = graphics.Color(214, 11, 180)
route_text_color = graphics.Color(255, 255, 255)
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
                        new_arrivals.append((arrival_time, entity.trip_update.trip.route_id))
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
    waits, arrivals, colors, routes = [], [], [], []
    for t, route in arrival_times[:count]:
        if route and route not in routes:
            routes.append(route)
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

    # Route numbers lead the label, e.g. "955 BUS TRACKER"; scrolling stops with the last 3 letters showing
    route_text = " ".join(routes)
    label = f"{route_text} BUS TRACKER" if route_text else "BUS TRACKER"
    scroll_pixels = 10 * (len(label) - 3)
    scroll_end = LABEL_PAUSE + scroll_pixels * FRAME_INTERVAL
    arrivals_start = scroll_end + ARRIVAL_PAUSE
    return {
        "waits": waits,
        "arrivals": arrivals,
        "colors": colors,
        "label": label,
        "route_chars": len(route_text),
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
        color = route_text_color if i < screen["route_chars"] else bus_text_color
        graphics.DrawText(canvas, wait_time_font, x + 24, y, color, letter)


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

        cycle_seconds = bus_screen["cycle_seconds"] + (SPOTIFY_CLOCK_SECONDS if spotify_now else 0)
        elapsed = time.monotonic() - cycle_start
        if spotify_now != showing_spotify or elapsed >= cycle_seconds:
            # Restart the cycle on a fresh snapshot (also when Spotify starts or stops)
            showing_spotify = spotify_now
            cycle_start = time.monotonic()
            bus_screen = get_bus_screen()
            has_buses = bool(arrival_times)
            elapsed = 0

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
