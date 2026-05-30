#!/usr/bin/env python3
"""
Device inspector — run this after get_token.py to confirm what the SDM API
can see. Lists all devices and their current temperature readings.

Usage:
    python list_devices.py
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

SDM_PROJECT_ID       = os.environ["SDM_PROJECT_ID"]
GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_BASE  = "https://smartdevicemanagement.googleapis.com/v1"


def get_access_token():
    resp = requests.post(TOKEN_URL, data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def celsius_to_f(c):
    return c * 9 / 5 + 32


def main():
    token = get_access_token()
    url   = f"{SDM_BASE}/enterprises/{SDM_PROJECT_ID}/devices"
    resp  = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    resp.raise_for_status()
    devices = resp.json().get("devices", [])

    print(f"\nFound {len(devices)} device(s):\n")

    for d in devices:
        name   = d.get("name", "")
        dtype  = d.get("type", "")
        traits = d.get("traits", {})

        info        = traits.get("sdm.devices.traits.Info", {})
        custom_name = info.get("customName", "(no custom name)")
        temp_trait  = traits.get("sdm.devices.traits.Temperature", {})
        fan_trait   = traits.get("sdm.devices.traits.Fan", {})

        temp_c = temp_trait.get("ambientTemperatureCelsius")
        temp_f = f"{celsius_to_f(temp_c):.1f}°F" if temp_c is not None else "N/A"
        fan    = fan_trait.get("timerMode", "N/A") if fan_trait else "no fan trait"

        print(f"  Name    : {custom_name}")
        print(f"  Type    : {dtype.split('.')[-1]}")
        print(f"  Temp    : {temp_f}")
        print(f"  Fan     : {fan}")
        print(f"  ID      : {name}")
        print()

    print("Tip: copy the device IDs above into .env if you want to pin which")
    print("sensor is 'upstairs' vs 'basement' (optional — auto-detected otherwise).")


if __name__ == "__main__":
    main()
