from gpiozero import DistanceSensor, AngularServo, RGBLED, Buzzer
from gpiozero import PWMOutputDevice, DigitalOutputDevice
from time import sleep
import time
import os
import subprocess
import shutil
import urllib.request
import urllib.parse
import mimetypes
import uuid

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
TG_COOLDOWN = 8.0
last_tg_time = 0.0

sensor = DistanceSensor(echo=6, trigger=2, max_distance=2.0)

servo = AngularServo(
    12,
    min_angle=-90,
    max_angle=90,
    min_pulse_width=0.5/1000,
    max_pulse_width=2.5/1000
)

led = RGBLED(red=16, green=20, blue=21)
buzzer = Buzzer(18)

ENA = PWMOutputDevice(19)
IN1 = DigitalOutputDevice(23)
IN2 = DigitalOutputDevice(24)

ENB = PWMOutputDevice(13)
IN3 = DigitalOutputDevice(17)
IN4 = DigitalOutputDevice(22)

CRUISE_SPEED = 0.50
SLOW_SPEED = 0.32
TURN_SPEED = 0.42

FAR_CM = 145
OBSTACLE_CM = 65
CLEAR_CM = 90

SCAN_STEP = 12
SCAN_PAUSE = 0.10

TURN_BURST = 0.16
MAX_TURN_BURSTS = 14
SIDESTEP_FORWARD_TIME = 0.45

PHOTO_COOLDOWN = 2.0
last_photo_time = 0.0

PHOTO_DIR = os.path.expanduser("~/photos")
os.makedirs(PHOTO_DIR, exist_ok=True)
HAS_RPICAM = shutil.which("rpicam-still") is not None

STUCK_COUNT_LIMIT = 6
stuck_count = 0

def stop():
    ENA.value = 0
    ENB.value = 0
    IN1.off()
    IN2.off()
    IN3.off()
    IN4.off()

def forward(speed):
    ENA.value = max(0.0, min(1.0, speed))
    ENB.value = max(0.0, min(1.0, speed))
    IN1.on()
    IN2.off()
    IN3.on()
    IN4.off()

def turn_left(speed=TURN_SPEED):
    ENA.value = max(0.0, min(1.0, speed))
    ENB.value = max(0.0, min(1.0, speed))
    IN1.off()
    IN2.on()
    IN3.on()
    IN4.off()

def turn_right(speed=TURN_SPEED):
    ENA.value = max(0.0, min(1.0, speed))
    ENB.value = max(0.0, min(1.0, speed))
    IN1.on()
    IN2.off()
    IN3.off()
    IN4.on()

def beep(t=0.12):
    buzzer.on()
    sleep(t)
    buzzer.off()

def center_servo():
    servo.angle = 0
    sleep(0.12)

def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x

def distance_cm_once():
    d = sensor.distance * 100.0
    return clamp(d, 0.0, sensor.max_distance * 100.0)

def smooth_distance(samples=5, delay=0.02):
    vals = []
    for _ in range(samples):
        vals.append(distance_cm_once())
        sleep(delay)
    vals.sort()
    return vals[len(vals)//2]

def tg_ready():
    global last_tg_time
    if not BOT_TOKEN or not CHAT_ID:
        return False
    now = time.time()
    if now - last_tg_time < TG_COOLDOWN:
        return False
    last_tg_time = now
    return True

def tg_post_json(url, data_dict, timeout=12):
    body = urllib.parse.urlencode(data_dict).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def send_message(text):
    if not tg_ready():
        return False
    try:
        tg_post_json(f"{TG_API}/sendMessage", {"chat_id": CHAT_ID, "text": text}, timeout=12)
        return True
    except Exception:
        return False

def _multipart_form(fields, files):
    boundary = uuid.uuid4().hex
    lines = []
    for k, v in fields.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{k}"')
        lines.append("")
        lines.append(str(v))
    for name, filepath in files.items():
        filename = os.path.basename(filepath)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(filepath, "rb") as f:
            filebytes = f.read()
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"')
        lines.append(f"Content-Type: {ctype}")
        lines.append("")
        lines.append(filebytes)
    lines.append(f"--{boundary}--")
    lines.append("")
    body = b""
    for item in lines:
        if isinstance(item, bytes):
            body += item + b"\r\n"
        else:
            body += item.encode("utf-8") + b"\r\n"
    return body, f"multipart/form-data; boundary={boundary}"

def send_photo(photo_path, caption=""):
    if not tg_ready():
        return False
    try:
        body, ctype = _multipart_form(
            fields={"chat_id": CHAT_ID, "caption": caption},
            files={"photo": photo_path}
        )
        req = urllib.request.Request(f"{TG_API}/sendPhoto", data=body, method="POST")
        req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True
    except Exception:
        return False

def take_photo():
    global last_photo_time
    if not HAS_RPICAM:
        return None
    now = time.time()
    if now - last_photo_time < PHOTO_COOLDOWN:
        return None
    filename = os.path.join(PHOTO_DIR, f"obstacle_{int(now)}.jpg")
    subprocess.run(
        ["rpicam-still", "-o", filename, "--nopreview", "-t", "1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    if os.path.exists(filename):
        last_photo_time = now
        return filename
    return None

def sonar_scan_best_direction():
    best_angle = 0
    best_dist = -1.0
    for angle in range(-90, 91, SCAN_STEP):
        servo.angle = angle
        sleep(SCAN_PAUSE)
        d = smooth_distance(samples=3, delay=0.015)
        if d > best_dist:
            best_dist = d
            best_angle = angle
    for angle in range(90, -91, -SCAN_STEP):
        servo.angle = angle
        sleep(SCAN_PAUSE)
        d = smooth_distance(samples=3, delay=0.015)
        if d > best_dist:
            best_dist = d
            best_angle = angle
    center_servo()
    return best_angle, best_dist

def avoid_to_side():
    best_angle, best_dist = sonar_scan_best_direction()
    strong = (best_dist < CLEAR_CM) or (stuck_count >= STUCK_COUNT_LIMIT)
    bursts = MAX_TURN_BURSTS + (6 if strong else 0)
    burst_time = TURN_BURST + (0.06 if strong else 0.0)
    for _ in range(bursts):
        if best_angle < 0:
            turn_left(TURN_SPEED)
        else:
            turn_right(TURN_SPEED)
        sleep(burst_time)
        stop()
        sleep(0.05)
        front = smooth_distance(samples=3, delay=0.02)
        if front > CLEAR_CM:
            break
    forward(SLOW_SPEED)
    sleep(SIDESTEP_FORWARD_TIME)
    stop()
    sleep(0.05)

try:
    stop()
    led.off()
    buzzer.off()
    center_servo()
    led.color = (0, 1, 0)
    send_message("✅ Robot WALL-E started")
    while True:
        center_servo()
        front = smooth_distance(samples=5, delay=0.02)
        if front >= FAR_CM:
            led.color = (0, 1, 0)
            forward(CRUISE_SPEED)
            stuck_count = max(0, stuck_count - 1)
            sleep(0.03)
            continue
        if OBSTACLE_CM <= front < FAR_CM:
            led.color = (1, 1, 0)
            forward(SLOW_SPEED)
            stuck_count = max(0, stuck_count - 1)
            sleep(0.03)
            continue
        if front < OBSTACLE_CM:
            stuck_count += 1
            stop()
            led.color = (1, 0, 0)
            beep(0.12)
            photo = take_photo()
            caption = f"⚠️ Obstacle detected! Distance: {front:.1f} cm"
            if photo:
                send_photo(photo, caption=caption)
            else:
                send_message(caption + " (no photo)")
            avoid_to_side()
            led.color = (0, 1, 0)
            forward(CRUISE_SPEED)
            sleep(0.05)
            continue
except KeyboardInterrupt:
    send_message("🛑 Robot stopped")
finally:
    stop()
    led.off()
    buzzer.off()
    servo.angle = 0
