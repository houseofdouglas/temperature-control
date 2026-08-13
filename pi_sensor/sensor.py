#!/usr/bin/env python3
"""
Raspberry Pi sensor node — reads a DHT11 and POSTs a heartbeat to the hub.

The Pi is a sensor like any ESP32 node, it just runs Python instead of
firmware. Unlike the ESP32s it has no WiFi credentials to hold (the OS owns
networking), so nothing here is secret and the file is tracked as-is.

Deploy (from a clone of this repo on the Pi):

    */5 * * * * /usr/bin/python3 /home/pi/nest/pi_sensor/sensor.py >> /home/pi/sensor.log 2>&1

Runs once and exits — cron owns the schedule, so there's no daemon to
supervise and a crashed run simply retries five minutes later.

TEMP_OFFSET_F is a real calibration, not a fudge. The DHT11 sits close enough
to the Pi's SoC to read high, so the raw value is corrected before sending.
It matters more than it looks: this node is currently the *only* basement
sensor, which makes it the reservoir reference for the comfort objective —
the "is there cold air worth moving?" decision is measured against this
number. If the sensor is repositioned, re-tune the offset against a known-good
thermometer, because an error here biases every gradient the hub computes.

Config comes from the environment so this file needs no per-host edits;
the defaults match the current deployment.
"""

import os
import sys
import time

import requests
import board
import adafruit_dht

# ── Config ────────────────────────────────────────────────────
# LOCATION is the board's permanent *id*, not its room. The hub maps
# device -> room (see /api/devices), so this node can be moved without
# touching this file.
LOCATION      = os.getenv("SENSOR_LOCATION", "basement: family")
HUB_URL       = os.getenv("HUB_URL", "http://10.0.0.140:5001/sensor")
TEMP_OFFSET_F = float(os.getenv("TEMP_OFFSET_F", "-4.0"))   # corrects for Pi self-heating
DHT_PIN       = getattr(board, os.getenv("DHT_PIN", "D4"))  # GPIO4 = physical pin 7
READ_ATTEMPTS = int(os.getenv("READ_ATTEMPTS", "5"))        # DHT11 is flaky; retry

# ── Read sensor ───────────────────────────────────────────────
sensor = adafruit_dht.DHT11(DHT_PIN, use_pulseio=False)

temp_f = temp_c = humidity = None
for _ in range(READ_ATTEMPTS):
    try:
        raw_c    = sensor.temperature
        humidity = sensor.humidity
        if raw_c is None or humidity is None:
            raise RuntimeError("null reading")
        # Apply the offset to BOTH units. The original script corrected only
        # temp_f, so each payload carried two contradictory temperatures
        # (temp_c ran ~4°F hotter than temp_f). Nothing analyses temp_c today,
        # which is why it went unnoticed.
        temp_f = round(raw_c * 9 / 5 + 32 + TEMP_OFFSET_F, 1)
        temp_c = round(raw_c + TEMP_OFFSET_F * 5 / 9, 1)
        break
    except RuntimeError:
        time.sleep(2)
else:
    print(f"ERROR: no valid reading after {READ_ATTEMPTS} attempts", file=sys.stderr)
    sys.exit(1)

try:
    sensor.exit()
except Exception:
    pass

# ── Send heartbeat ────────────────────────────────────────────
payload = {
    "location": LOCATION,
    "temp_f":   temp_f,
    "temp_c":   temp_c,
    "humidity": humidity,
}

try:
    resp = requests.post(HUB_URL, json=payload, timeout=10)
    print(f"Sent {payload} → HTTP {resp.status_code}")
    resp.raise_for_status()
except Exception as e:
    print(f"ERROR sending heartbeat: {e}", file=sys.stderr)
    sys.exit(1)
