#!/usr/bin/env python3
"""
Fetch & cache historical outdoor weather for correlation analysis.

We don't have a physical outdoor sensor (yet), so this pulls hourly
historical temperature/humidity/solar data from Open-Meteo's free
archive API (no key required) for the ZIP code's location, and stores
it in the same nest.db SQLite file as an `outdoor_weather` table —
keyed by timestamp so it joins naturally with sensor_readings,
fan_events, and hvac_events.

Usage:
    python fetch_weather.py                  # backfill from earliest sensor reading to now
    python fetch_weather.py --zip 84096      # override ZIP (default cached after first run)
    python fetch_weather.py --days 14        # explicit lookback window
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

DB_PATH = os.getenv("DB_PATH", "nest.db")
GEOCODE_CACHE = "weather_location.json"
DEFAULT_ZIP = "84096"

TIMEZONE = "America/Denver"


def geocode_zip(zip_code: str) -> dict:
    """ZIP -> {lat, lon, place, state}, cached to disk so we don't re-hit the API."""
    if os.path.exists(GEOCODE_CACHE):
        with open(GEOCODE_CACHE) as f:
            cached = json.load(f)
        if cached.get("zip") == zip_code:
            return cached

    r = requests.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=10)
    r.raise_for_status()
    data = r.json()
    place = data["places"][0]
    loc = {
        "zip": zip_code,
        "place": place["place name"],
        "state": place["state abbreviation"],
        "lat": float(place["latitude"]),
        "lon": float(place["longitude"]),
    }
    with open(GEOCODE_CACHE, "w") as f:
        json.dump(loc, f, indent=2)
    print(f"Geocoded {zip_code} -> {loc['place']}, {loc['state']} "
          f"({loc['lat']}, {loc['lon']})  [cached to {GEOCODE_CACHE}]")
    return loc


def db_init_weather(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outdoor_weather (
            ts              REAL PRIMARY KEY,
            temp_f          REAL,
            humidity        INTEGER,
            cloud_cover     INTEGER,
            solar_radiation REAL
        )
    """)
    conn.commit()


def earliest_sensor_ts(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT MIN(ts) FROM sensor_readings").fetchone()
    return row[0] if row and row[0] else None


def fetch_archive(lat: float, lon: float, start_date: str, end_date: str) -> dict:
    """Hourly historical weather from Open-Meteo's free archive API."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": "temperature_2m,relative_humidity_2m,cloud_cover,shortwave_radiation",
        "temperature_unit": "fahrenheit",
        "timezone": TIMEZONE,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def upsert_weather(conn: sqlite3.Connection, hourly: dict) -> int:
    times = hourly["time"]
    temps = hourly["temperature_2m"]
    hums  = hourly["relative_humidity_2m"]
    clouds = hourly["cloud_cover"]
    rad   = hourly["shortwave_radiation"]

    rows = []
    for i, t in enumerate(times):
        # Open-Meteo returns local ISO timestamps (no offset) for the requested timezone
        dt = datetime.fromisoformat(t).replace(tzinfo=_local_tz())
        ts = dt.timestamp()
        if temps[i] is None:
            continue
        rows.append((ts, temps[i], hums[i], clouds[i], rad[i]))

    conn.executemany(
        "INSERT OR REPLACE INTO outdoor_weather (ts, temp_f, humidity, cloud_cover, solar_radiation) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


_TZ_CACHE = None
def _local_tz():
    """Resolve America/Denver to a tzinfo without relying on optional zoneinfo data."""
    global _TZ_CACHE
    if _TZ_CACHE is None:
        try:
            from zoneinfo import ZoneInfo
            _TZ_CACHE = ZoneInfo(TIMEZONE)
        except Exception:
            _TZ_CACHE = timezone(timedelta(hours=-6))  # MDT fallback
    return _TZ_CACHE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=DEFAULT_ZIP)
    ap.add_argument("--days", type=int, default=None,
                    help="Lookback window in days (default: back to earliest sensor reading)")
    args = ap.parse_args()

    loc = geocode_zip(args.zip)

    conn = sqlite3.connect(DB_PATH)
    db_init_weather(conn)

    if args.days:
        start = datetime.now() - timedelta(days=args.days)
    else:
        earliest = earliest_sensor_ts(conn)
        start = datetime.fromtimestamp(earliest) if earliest else datetime.now() - timedelta(days=7)

    end = datetime.now()
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")

    print(f"Fetching hourly outdoor weather for {loc['place']}, {loc['state']} "
          f"from {start_date} to {end_date} ...")
    data = fetch_archive(loc["lat"], loc["lon"], start_date, end_date)
    n = upsert_weather(conn, data["hourly"])
    print(f"Stored {n} hourly outdoor readings in {DB_PATH} (table: outdoor_weather)")

    # Quick sanity peek
    rows = conn.execute(
        "SELECT datetime(ts, 'unixepoch', 'localtime'), temp_f, humidity, cloud_cover, solar_radiation "
        "FROM outdoor_weather ORDER BY ts DESC LIMIT 5"
    ).fetchall()
    print("\nMost recent rows:")
    for r in rows:
        print(" ", r)

    conn.close()


if __name__ == "__main__":
    sys.exit(main())
