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

# Wait-time color gradient (minutes): red at or below BUS_WAIT_RED, green at or above BUS_WAIT_GREEN
BUS_WAIT_RED = 2
BUS_WAIT_GREEN = 12

# Arrival time shown under the wait is the bus's arrival plus this ride time
BUS_RIDE_MINUTES = 5

# Configure the LED matrix
options = RGBMatrixOptions()
options.rows = 32
options.cols = 64
options.chain_length = 1
options.parallel = 1
options.hardware_mapping = 'regular'
options.gpio_slowdown = 2
options.brightness = 75  # Adjust 1-100 (100 = full brightness)
## NECESSARY!!!!! VVVVV
options.hardware_mapping = 'adafruit-hat'

wait_times = []
arrival_times = []
current_temperature = 0
weather_code = 0
sunrise_time = None
sunset_time = None
spotify_access_token = ""
spotify_token_expires_at = 0
spotify_album_art = None
spotify_track_name = ""
spotify_artist_name = ""

wait_time_font = graphics.Font()
wait_time_font.LoadFont("/home/pi/bustracker/fonts/7x13.bdf")
arrival_time_font = graphics.Font()
arrival_time_font.LoadFont("/home/pi/bustracker/fonts/6x10.bdf")
bus_number_font = graphics.Font()
bus_number_font.LoadFont("/home/pi/bustracker/fonts/4x6.bdf")
next_bus_wait_font = graphics.Font()
next_bus_wait_font.LoadFont("/home/pi/bustracker/fonts/8x13B.bdf")
date_font = graphics.Font()
date_font.LoadFont("/home/pi/bustracker/fonts/5x8.bdf")
default_color = graphics.Color(255, 255, 0)
bus_text_color = graphics.Color(214, 11, 180)
next_bus_arrival_color = graphics.Color(255, 100, 255)
no_bus_color = graphics.Color(255, 0, 0)
date_color = graphics.Color(43, 224, 227)

sun_color = graphics.Color(255, 225, 0)

# Get bus arrival times from MTS API
def get_bus_times():
    global arrival_times
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
                        if timestamp:
                            new_arrivals.append(datetime.fromtimestamp(timestamp))
        # Swap in the whole list at once so the draw loop never sees a half-built list
        arrival_times = sorted(new_arrivals)
    except Exception as e:
        print(f"Bus API error: {e}")
        # Keep existing arrival_times on error


def schedule_bus_update():
    get_bus_times()
    timer = threading.Timer(30.0, schedule_bus_update)
    timer.daemon = True
    timer.start()


def get_wait_color(minutes):
    # Red when the bus is about to arrive, fading through yellow to green once there's plenty of time
    if minutes <= BUS_WAIT_RED:
        return graphics.Color(255, 0, 0)
    if minutes >= BUS_WAIT_GREEN:
        return graphics.Color(0, 255, 0)
    mid = (BUS_WAIT_RED + BUS_WAIT_GREEN) / 2
    if minutes <= mid:
        g = int(255 * (minutes - BUS_WAIT_RED) / (mid - BUS_WAIT_RED))
        return graphics.Color(255, g, 0)
    r = int(255 * (BUS_WAIT_GREEN - minutes) / (BUS_WAIT_GREEN - mid))
    return graphics.Color(r, 255, 0)


def draw_centered_right(canvas, font, char_width, y, color, text):
    # Center text within the right 32x32 half of the panel
    x = 32 + max(0, (32 - len(text) * char_width) // 2)
    graphics.DrawText(canvas, font, x, y, color, text)


def draw_next_bus(canvas):
    draw_centered_right(canvas, bus_number_font, 4, 6, default_color, "NEXT BUS")

    upcoming = [t for t in arrival_times if t >= datetime.now()]
    if not upcoming:
        draw_centered_right(canvas, next_bus_wait_font, 8, 20, no_bus_color, "NONE")
        return

    next_bus_time = upcoming[0]
    wait_minutes = (next_bus_time - datetime.now()).total_seconds() / 60
    wait_text = "now" if wait_minutes < 1 else f"{min(int(wait_minutes), 99)}m"
    draw_centered_right(canvas, next_bus_wait_font, 8, 20, get_wait_color(wait_minutes), wait_text)

    estimated_arrival = next_bus_time + timedelta(minutes=BUS_RIDE_MINUTES)
    draw_centered_right(canvas, arrival_time_font, 6, 30, next_bus_arrival_color, estimated_arrival.strftime("%I:%M"))


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
        current_temperature = 0
        weather_code = 0
        sunrise_time = None
        sunset_time = None

def schedule_weather_update():
    get_openmeteo_weather()
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


def fetch_spotify_now_playing():
    global spotify_album_art
    global spotify_track_name
    global spotify_artist_name

    token = get_spotify_access_token()
    if not token:
        spotify_album_art = None
        spotify_track_name = ""
        spotify_artist_name = ""
        return

    try:
        response = requests.get(
            "https://api.spotify.com/v1/me/player/currently-playing",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        if response.status_code == 204:
            spotify_album_art = None
            spotify_track_name = ""
            spotify_artist_name = ""
            return

        response.raise_for_status()
        data = response.json()
        item = data.get("item") or {}
        spotify_track_name = item.get("name", "")
        spotify_artist_name = ", ".join(
            artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")
        )

        images = item.get("album", {}).get("images", [])
        if not images:
            spotify_album_art = None
            return

        image_url = images[0].get("url")
        if not image_url:
            spotify_album_art = None
            return

        image_response = requests.get(image_url, timeout=10)
        image_response.raise_for_status()
        image = Image.open(io.BytesIO(image_response.content)).convert("RGB")
        spotify_album_art = image
    except Exception as e:
        print(f"Spotify playback error: {e}")


def schedule_spotify_update():
    fetch_spotify_now_playing()
    timer = threading.Timer(30.0, schedule_spotify_update)
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

def draw_rain(canvas, _is_animated=False):
    draw_cloud(canvas)
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

def draw_thunderstorm(canvas, _is_animated=False):
    draw_cloud(canvas)
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


def draw_clock_left(canvas, blink_on):
    temp = current_temperature
    
    # Determine if it's day or night based on sunrise/sunset
    now = datetime.now()
    is_day = True
    if sunrise_time and sunset_time:
        is_day = not ((now > sunset_time + timedelta(minutes=30)) or (now < sunrise_time - timedelta(minutes=30)))
    
    temp_color = get_temp_color(temp)

    now = datetime.now()
    time_str = now.strftime("%I:%M")
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
        draw_weather(canvas, is_day)
    else:
        graphics.DrawText(canvas, wait_time_font, 5, 30, temp_color, f"{temp:.0f}")
        graphics.DrawText(canvas, arrival_time_font, 24, 27, temp_color, "°")
    return canvas


def draw_blinking_clock(canvas):
    return draw_clock_left(canvas, False)


def draw_spotify_art(canvas):
    if spotify_album_art is None:
        if spotify_track_name:
            graphics.DrawText(canvas, wait_time_font, 34, 31, bus_text_color, spotify_track_name[:8])
        return

    size = 32
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    art_square = ImageOps.fit(spotify_album_art, (size, size), method=resample)
    canvas.SetImage(art_square.convert("RGB"), 32, 0)

def draw_weather(canvas, is_day):
    if weather_code in [300, 301, 302, 310, 311, 312, 313, 314, 321, 500, 501, 502, 503, 504, 511, 520, 521, 522, 531]:
        draw_rain(canvas, False)
    elif weather_code in [200, 201, 202, 210, 211, 212, 221, 230, 231, 232]:
        draw_thunderstorm(canvas, False)
    elif not is_day:
        draw_moon(canvas, False)
    elif weather_code == 800:
        draw_sun(canvas)
    elif weather_code in [801, 802]:
        draw_partly_cloudy(canvas)
    else:
        draw_cloud(canvas)

# Initialize matrix
matrix = RGBMatrix(options=options)
offscreen_canvas = matrix.CreateFrameCanvas()  # Create ONCE and reuse

# Start weather update scheduler
schedule_weather_update()
schedule_spotify_update()
schedule_bus_update()

try:
    while True:
        offscreen_canvas.Clear()
        draw_clock_left(offscreen_canvas, datetime.now().second % 2 == 0)
        draw_next_bus(offscreen_canvas)
        offscreen_canvas = matrix.SwapOnVSync(offscreen_canvas)
        time.sleep(1)

except KeyboardInterrupt:
    print("Exiting...")
finally:
    matrix.Clear()
    del matrix
