#!/usr/bin/env python3
"""
Nest Fan Optimizer Hub
======================
Receives temperature heartbeats from ESP32-C3 sensor nodes over WiFi,
calculates the floor-to-floor delta, and controls the Nest fan via SDM API.

Run with:  .venv/bin/python hub.py
"""

import os
import time
import random
import hashlib
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify
from flask_socketio import SocketIO
import requests as req
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────
SDM_PROJECT_ID        = os.environ["SDM_PROJECT_ID"]
GOOGLE_CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN  = os.environ["GOOGLE_REFRESH_TOKEN"]

HUB_PORT              = int(os.getenv("HUB_PORT",              "5001"))
TEMP_DELTA_THRESHOLD_F = float(os.getenv("TEMP_DELTA_THRESHOLD_F", "3.0"))
FAN_RUN_DURATION_SECONDS = int(os.getenv("FAN_RUN_DURATION_SECONDS", "1200"))
SENSOR_STALE_SECONDS  = int(os.getenv("SENSOR_STALE_SECONDS",  "600"))   # ignore readings >10 min old
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))  # evaluate fan every 2 min
# Quiet hours. Set both to the same value (or blank) to disable entirely.
# Disabled as of 2026-08-12: the evening/overnight window is when the compressor
# works hardest (77-91% duty, 18:00-24:00) *and* when the basement reservoir is
# deepest (11-12°F) — suppressing the fan there was blocking the highest-value
# cooling hours of the day.
FAN_QUIET_START       = os.getenv("FAN_QUIET_START", "")
FAN_QUIET_END         = os.getenv("FAN_QUIET_END",   "")

# ── Comfort-based control ─────────────────────────────────────
# Objective (from 2026-08-12): keep the occupied upstairs rooms below
# COMFORT_MAX_F using basement air, so the compressor runs less — replacing the
# older "narrow the floor-to-floor band" goal, which no longer matches the
# sensor layout (3 upstairs, 1 basement, 1 main floor).
#
# Two conditions must BOTH hold before the fan is worth running:
#   1. the occupied rooms are at/near the comfort ceiling, and
#   2. the basement is actually cold enough to be worth moving air from.
# Condition 2 is new — circulating air when the gradient has collapsed just
# runs the blower for nothing.
COMFORT_MAX_F        = float(os.getenv("COMFORT_MAX_F",      "74.0"))
COMFORT_DEADBAND_F   = float(os.getenv("COMFORT_DEADBAND_F", "1.5"))
MIN_GRADIENT_F       = float(os.getenv("MIN_GRADIENT_F",     "3.0"))
OCCUPIED_PREFIX      = os.getenv("OCCUPIED_PREFIX",  "upstairs")
RESERVOIR_PREFIX     = os.getenv("RESERVOIR_PREFIX", "basement")

# Which room actually matters, by time of day.
#
# Taking the hottest upstairs room is wrong: at 3am that's often the office,
# which nobody is in. Optimise for where people actually are — the living room
# by day, the bedroom overnight.
#
# This matters more than it sounds, because the rooms sit on opposite sides of
# the thermostat. Measured over the current layout, the living room runs
# ~0.6°F HOTTER than the hallway by day (peaking +2.5°F), while the master
# bedroom runs ~0.3-1.0°F COOLER. A single target can't serve both.
#
# Format:  HH:MM-HH:MM=location[@cap_f]; ...
# Windows may wrap midnight. @cap overrides COMFORT_MAX_F for that window.
# Falls back to the hottest OCCUPIED_PREFIX room if the scheduled sensor is
# stale, so a dead battery degrades rather than blinds the controller.
OCCUPANCY_SCHEDULE = os.getenv(
    "OCCUPANCY_SCHEDULE",
    "07:00-20:00=upstairs: living room@74; 20:00-07:00=upstairs: master bedroom@70",
)

# ── Thermostat setpoint control ───────────────────────────────
# The hub normally only touches the fan. With this on it also drives the cool
# setpoint on a schedule, so the target stops overshooting what comfort needs
# during the day (measured: the hallway only needs ~72°F to keep the living
# room under 74°F, but it was being held at 69.8°F).
#
# Derived from the measured hallway bias, NOT from the comfort caps directly:
# the living room runs hotter than the thermostat, the bedroom cooler, so the
# thermostat target is offset from the room ceiling in opposite directions.
#
# Three safeguards, because this changes how the house feels for people who
# did not ask the computer for an opinion:
#   1. A MANUAL CHANGE WINS. If the setpoint is ever something we did not
#      command, that's a human overriding us — back off for the rest of that
#      window rather than fighting them two minutes later. The override is
#      logged, which also makes it a clean comfort-failure signal.
#   2. Hard bounds. Nothing outside [SETPOINT_MIN_F, SETPOINT_MAX_F] is ever
#      sent, whatever the schedule says.
#   3. An expiry date. This is a two-week experiment, not a permanent
#      takeover; past SETPOINT_CONTROL_UNTIL the hub goes back to fan-only.
SETPOINT_CONTROL_ENABLED = os.getenv("SETPOINT_CONTROL_ENABLED", "false").lower() == "true"
SETPOINT_SCHEDULE        = os.getenv("SETPOINT_SCHEDULE", "07:00-20:00=73; 20:00-07:00=70")
SETPOINT_CONTROL_UNTIL   = os.getenv("SETPOINT_CONTROL_UNTIL", "")   # YYYY-MM-DD, blank = no expiry
SETPOINT_MIN_F           = float(os.getenv("SETPOINT_MIN_F", "68.0"))
SETPOINT_MAX_F           = float(os.getenv("SETPOINT_MAX_F", "76.0"))
SETPOINT_TOLERANCE_F     = float(os.getenv("SETPOINT_TOLERANCE_F", "0.6"))  # Nest rounds to 0.5°C steps

# ── Experiment: rotate daily through fan-control strategies ──
# Phase 2 (from 2026-06-27): duty_cycle (15 on / 15 off) is now the
# production default — the fallback for any unrecognized arm and what runs
# when EXPERIMENT_ENABLED=false.  The rotation tests two alternatives against
# that new baseline: burst (morning-only window) and higher_threshold (runs
# the duty-cycle pattern only when Δ > EXPERIMENT_HIGH_THRESHOLD_F).
# Default off: the phase-1 A/B test concluded (duty_cycle won), so a fresh
# clone should run the settled strategy rather than start randomising.
EXPERIMENT_ENABLED          = os.getenv("EXPERIMENT_ENABLED", "false").lower() == "true"
EXPERIMENT_SEED_SALT        = os.getenv("EXPERIMENT_SEED_SALT", "nest-fan-arms-v2")
EXPERIMENT_ARMS             = ["burst", "higher_threshold"]
EXPERIMENT_HIGH_THRESHOLD_F = float(os.getenv("EXPERIMENT_HIGH_THRESHOLD_F", "9.0"))
EXPERIMENT_BURST_MINUTES    = int(os.getenv("EXPERIMENT_BURST_MINUTES",     "120"))
EXPERIMENT_DUTY_ON_MINUTES  = int(os.getenv("EXPERIMENT_DUTY_ON_MINUTES",   "15"))
EXPERIMENT_DUTY_OFF_MINUTES = int(os.getenv("EXPERIMENT_DUTY_OFF_MINUTES",  "15"))

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Sensor store ──────────────────────────────────────────────
# Keyed by *resolved* location (see device remapping below), not by whatever
# the firmware happens to report.
# { location: { temp_f, humidity, received_at } }
sensor_data: dict = {}
# { location: deque([{ts, temp_f, humidity}, ...]) }
sensor_history: dict = {}
HISTORY_MAX = 1000  # keep last 1000 readings per sensor (~3.5 days at 5 min)
sensor_lock = threading.Lock()

# ── Device → location remapping ───────────────────────────────
# Sensors are physically moved between rooms, and reflashing a board just to
# rename it is a pain. So the string a board reports is treated as its
# immutable *device id* (a serial number that happens to look like a room),
# and the hub owns the mapping from device id → the room it's actually in.
# Rename from the dashboard or POST /api/devices/<id>/location; no reflash.
#
# Unmapped devices fall through to their device id, so a brand-new board that
# has never been renamed behaves exactly as it always did.
# { device_id: location }
device_locations: dict = {}

# ── Fan event log ─────────────────────────────────────────────
# [{ ts_ms, state: "ON"|"OFF" }, ...]  — only state-change events
fan_events: deque = deque(maxlen=500)
_last_fan_state: str = "OFF"

# ── HVAC event log ────────────────────────────────────────────
# [{ ts_ms, state: "COOLING"|"HEATING"|"OFF" }, ...]
hvac_events: deque = deque(maxlen=500)
_last_hvac_state: str = "OFF"

# ── Setpoint control state ────────────────────────────────────
_setpoint_commanded: float | None = None   # what we last told the Nest
_setpoint_yielded_window: str | None = None  # window we've conceded to a human
_setpoint_last_window: str | None = None     # window we last acted in

# ── Thermostat target ─────────────────────────────────────────
# The setpoint moves both on a schedule and by hand, so it's a confounder for
# any "did the fan displace AC?" comparison — a colder target explains more
# compressor time on its own. Tracked as change-events, same as fan/hvac.
# [{ ts_ms, cool_f, prev_cool_f, mode }, ...]
setpoint_events: deque = deque(maxlen=500)
_thermostat_state: dict | None = None

# ── Experiment-arm tracking ───────────────────────────────────
_experiment_dates_logged: set = set()   # date.isoformat() strings already written to DB
_last_logged_arm: str | None = None     # avoid re-logging "active arm" every poll cycle

# ── Database ──────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "nest.db")

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                location  TEXT    NOT NULL,
                temp_f    REAL    NOT NULL,
                temp_c    REAL,
                humidity  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_readings_ts       ON sensor_readings(ts);
            CREATE INDEX IF NOT EXISTS idx_readings_location ON sensor_readings(location, ts);

            CREATE TABLE IF NOT EXISTS fan_events (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    REAL    NOT NULL,
                state TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fan_ts ON fan_events(ts);

            CREATE TABLE IF NOT EXISTS hvac_events (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    REAL    NOT NULL,
                state TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hvac_ts ON hvac_events(ts);

            -- What the thermostat was actually targeting at each poll. Without
            -- this, AC-runtime comparisons are meaningless: the setpoint moves
            -- on a schedule (observed 70.0°F at 07:30, 73.6°F at 08:00), so a
            -- day with more compressor time may simply have had a colder target.
            CREATE TABLE IF NOT EXISTS thermostat_state (
                ts          REAL NOT NULL,
                mode        TEXT,
                cool_f      REAL,
                heat_f      REAL,
                hvac_status TEXT,
                eco         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_thermostat_ts ON thermostat_state(ts);

            CREATE TABLE IF NOT EXISTS device_locations (
                device_id  TEXT PRIMARY KEY,
                location   TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS experiment_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT    NOT NULL UNIQUE,
                arm       TEXT    NOT NULL,
                logged_at REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_experiment_date ON experiment_log(date);
        """)
    log.info("Database ready: %s", DB_PATH)

def db_write_reading(ts, location, temp_f, temp_c, humidity):
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO sensor_readings (ts, location, temp_f, temp_c, humidity) VALUES (?,?,?,?,?)",
                (ts, location, temp_f, temp_c, humidity)
            )
    except Exception as e:
        log.error("DB write reading error: %s", e)

def db_write_fan_event(ts_ms, state):
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO fan_events (ts, state) VALUES (?,?)",
                         (ts_ms, state))
    except Exception as e:
        log.error("DB write fan event error: %s", e)

def db_write_hvac_event(ts_ms, state):
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO hvac_events (ts, state) VALUES (?,?)",
                         (ts_ms, state))
    except Exception as e:
        log.error("DB write hvac event error: %s", e)

def db_write_experiment_arm(date_str, arm, ts):
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO experiment_log (date, arm, logged_at) VALUES (?,?,?)",
                (date_str, arm, ts)
            )
    except Exception as e:
        log.error("DB write experiment_log error: %s", e)

def db_load_experiment_log():
    """Preload which dates already have a logged arm, so a restart mid-day
    doesn't write a duplicate row (the UNIQUE constraint would just no-op,
    but this keeps the in-memory set consistent with the DB)."""
    with db_connect() as conn:
        rows = conn.execute("SELECT date FROM experiment_log").fetchall()
    _experiment_dates_logged.update(r["date"] for r in rows)
    log.info("Loaded %d experiment-log dates from DB", len(rows))

def db_write_thermostat_state(ts, mode, cool_f, heat_f, hvac_status, eco):
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO thermostat_state (ts, mode, cool_f, heat_f, hvac_status, eco) "
                "VALUES (?,?,?,?,?,?)",
                (ts, mode, cool_f, heat_f, hvac_status, eco)
            )
    except Exception as e:
        log.error("DB write thermostat_state error: %s", e)

def db_write_device_location(device_id, location, ts):
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO device_locations (device_id, location, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET location=excluded.location, "
                "updated_at=excluded.updated_at",
                (device_id, location, ts)
            )
    except Exception as e:
        log.error("DB write device_location error: %s", e)

def db_delete_device_location(device_id):
    try:
        with db_connect() as conn:
            conn.execute("DELETE FROM device_locations WHERE device_id = ?", (device_id,))
    except Exception as e:
        log.error("DB delete device_location error: %s", e)

def db_load_device_locations():
    """Load device→location overrides so renames survive a restart."""
    with db_connect() as conn:
        rows = conn.execute("SELECT device_id, location FROM device_locations").fetchall()
    device_locations.update({r["device_id"]: r["location"] for r in rows})
    if rows:
        log.info("Loaded %d device location override(s): %s", len(rows),
                 ", ".join(f"{r['device_id']} → {r['location']}" for r in rows))
    else:
        log.info("No device location overrides (all sensors use their firmware name)")

def resolve_location(device_id: str) -> str:
    """Map a board's hard-coded id to the room it's actually in right now."""
    return device_locations.get(device_id, device_id)

def db_load_history():
    """Preload recent sensor readings and fan events into in-memory stores on startup."""
    cutoff = time.time() - 259200  # last 3 days
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT ts, location, temp_f, humidity FROM sensor_readings "
            "WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
        for r in rows:
            loc = r["location"]
            if loc not in sensor_history:
                sensor_history[loc] = deque(maxlen=HISTORY_MAX)
            sensor_history[loc].append({
                "ts":       r["ts"] * 1000,
                "temp_f":   r["temp_f"],
                "humidity": r["humidity"],
            })

        evts = conn.execute(
            "SELECT ts, state FROM fan_events WHERE ts >= ? ORDER BY ts ASC",
            (cutoff * 1000,)
        ).fetchall()
        for e in evts:
            fan_events.append({"ts": e["ts"], "state": e["state"]})

        hvac_evts = conn.execute(
            "SELECT ts, state FROM hvac_events WHERE ts >= ? ORDER BY ts ASC",
            (cutoff * 1000,)
        ).fetchall()
        for e in hvac_evts:
            hvac_events.append({"ts": e["ts"], "state": e["state"]})

    log.info("Loaded %d sensor rows, %d fan events, %d hvac events from DB",
             len(rows), len(evts), len(hvac_evts))

# ── Flask app (receives heartbeats) ───────────────────────────
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")


@app.route("/sensor", methods=["POST"])
def receive_sensor():
    data = request.get_json(silent=True)
    if not data or "location" not in data or "temp_f" not in data:
        return jsonify({"error": "missing fields"}), 400

    # Boards identify themselves with their flashed name. Newer firmware may
    # send an explicit device_id (e.g. MAC); either way the id is immutable
    # and the hub decides which room it maps to.
    device_id = data.get("device_id") or data["location"]
    location  = resolve_location(device_id)

    now = time.time()
    with sensor_lock:
        sensor_data[location] = {
            "temp_f":      data["temp_f"],
            "temp_c":      data.get("temp_c"),
            "humidity":    data.get("humidity"),
            "received_at": now,
            "device_id":   device_id,
        }
        if location not in sensor_history:
            sensor_history[location] = deque(maxlen=HISTORY_MAX)
        sensor_history[location].append({
            "ts":       now * 1000,   # ms for Chart.js
            "temp_f":   data["temp_f"],
            "humidity": data.get("humidity"),
        })

    log.info("❶ Heartbeat  %-15s  %.1f°F  %s%% RH",
             location, data["temp_f"], data.get("humidity", "?"))

    threading.Thread(target=db_write_reading, daemon=True,
                     args=(now, location, data["temp_f"], data.get("temp_c"), data.get("humidity"))).start()

    # Push live update to all connected browsers
    socketio.emit("sensor_update", _dashboard_payload())
    # Push the new history point so charts update without a full reload
    socketio.emit("history_point", {
        "location": location,
        "point":    {"ts": now * 1000, "temp_f": data["temp_f"], "humidity": data.get("humidity")},
    })
    return jsonify({"ok": True})


def _dashboard_payload() -> dict:
    """Build the status payload shared by the REST endpoint and WebSocket events."""
    now = time.time()
    with sensor_lock:
        sensors = [
            {
                "location":    loc,
                "device_id":   d.get("device_id", loc),
                "renamed":     d.get("device_id", loc) != loc,
                "temp_f":      d["temp_f"],
                "humidity":    d.get("humidity"),
                "age_seconds": int(now - d["received_at"]),
            }
            for loc, d in sorted(sensor_data.items())
        ]
    temps = [s["temp_f"] for s in sensors]
    delta = round(max(temps) - min(temps), 1) if len(temps) >= 2 else None

    arm = arm_for_date(datetime.now().date()) if EXPERIMENT_ENABLED else "duty_cycle"

    with sensor_lock:
        fresh = {loc: d for loc, d in sensor_data.items()
                 if now - d["received_at"] <= SENSOR_STALE_SECONDS}
    comfort = comfort_state(fresh)
    ts_state = _thermostat_state or {}

    return {
        "sensors":    sensors,
        "fan_state":  _last_fan_state,
        "delta_f":    delta,
        "threshold_f": TEMP_DELTA_THRESHOLD_F,
        "experiment": {"enabled": EXPERIMENT_ENABLED, "arm": arm},
        "comfort":    comfort,          # carries its own cap_f for this window
        "thermostat": {
            "mode":        ts_state.get("mode"),
            "cool_f":      ts_state.get("cool_f"),
            "hvac_status": ts_state.get("hvac_status"),
        },
    }


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(_dashboard_payload())


@app.route("/api/history", methods=["GET"])
def api_history():
    with sensor_lock:
        return jsonify({
            loc: list(pts)
            for loc, pts in sensor_history.items()
        })


@app.route("/api/fan_events", methods=["GET"])
def api_fan_events():
    return jsonify(list(fan_events))


@app.route("/api/hvac_events", methods=["GET"])
def api_hvac_events():
    return jsonify(list(hvac_events))


@app.route("/api/setpoint_events", methods=["GET"])
def api_setpoint_events():
    """Every observed change to the thermostat target — schedule or manual."""
    return jsonify(list(setpoint_events))


@app.route("/api/devices", methods=["GET"])
def api_devices():
    """Every device the hub has seen live, with its firmware id and the room
    it's currently mapped to."""
    now = time.time()
    with sensor_lock:
        devices = [
            {
                "device_id":   d.get("device_id", loc),
                "location":    loc,
                "renamed":     d.get("device_id", loc) != loc,
                "age_seconds": int(now - d["received_at"]),
            }
            for loc, d in sorted(sensor_data.items())
        ]
    return jsonify({"devices": devices, "overrides": dict(device_locations)})


@app.route("/api/devices/<path:device_id>/location", methods=["POST"])
def api_set_device_location(device_id):
    """Point a device at a different room. Send {"location": "upstairs: aria"},
    or {"location": null} to fall back to the firmware name."""
    data = request.get_json(silent=True) or {}
    if "location" not in data:
        return jsonify({"error": "missing 'location'"}), 400

    new_location = data["location"]
    if new_location is not None:
        if not isinstance(new_location, str) or not new_location.strip():
            return jsonify({"error": "'location' must be a non-empty string or null"}), 400
        new_location = new_location.strip()

    old_location = resolve_location(device_id)

    # Reject a name another *different* device is already reporting under —
    # two sensors sharing a key would silently overwrite each other.
    target = new_location if new_location is not None else device_id
    with sensor_lock:
        clash = sensor_data.get(target)
        if clash is not None and clash.get("device_id", target) != device_id:
            return jsonify({
                "error": f"'{target}' is already in use by device "
                         f"'{clash.get('device_id', target)}'"
            }), 409

    if new_location is None:
        device_locations.pop(device_id, None)
        threading.Thread(target=db_delete_device_location, daemon=True,
                         args=(device_id,)).start()
    else:
        device_locations[device_id] = new_location
        threading.Thread(target=db_write_device_location, daemon=True,
                         args=(device_id, new_location, time.time())).start()

    resolved = resolve_location(device_id)

    # Drop the live entry under the old name so the moved sensor doesn't linger
    # in the table as a ghost that slowly goes stale. History is deliberately
    # left alone: those readings really were taken in the old room, and
    # relabelling them would corrupt the record the analysis scripts read.
    if resolved != old_location:
        with sensor_lock:
            sensor_data.pop(old_location, None)
        log.info("📍 Device '%s' moved: %s → %s", device_id, old_location, resolved)

    socketio.emit("sensor_update", _dashboard_payload())
    return jsonify({"ok": True, "device_id": device_id, "location": resolved})


@app.route("/api/experiment", methods=["GET"])
def api_experiment():
    """Today's active arm plus the recent assignment history (for later
    analysis — e.g. grouping fan/temperature data by arm)."""
    today = datetime.now().date()
    arm_today = arm_for_date(today) if EXPERIMENT_ENABLED else "duty_cycle"
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT date, arm, logged_at FROM experiment_log ORDER BY date DESC LIMIT 30"
        ).fetchall()
    return jsonify({
        "enabled": EXPERIMENT_ENABLED,
        "arms":    EXPERIMENT_ARMS,
        "today":   {"date": today.isoformat(), "arm": arm_today},
        "history": [{"date": r["date"], "arm": r["arm"], "logged_at": r["logged_at"]} for r in rows],
    })


@app.route("/status", methods=["GET"])
def status():
    """Live dashboard — auto-updates every 10 s without a full page reload."""
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Nest Fan Optimizer</title>
  <style>
    body  { font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
    h1    { font-size: 1.3rem; color: #333; margin-bottom: 4px; }
    p.sub { color: #888; font-size: .85rem; margin: 0 0 16px; }
    .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
    .tab  { padding: 7px 20px; border-radius: 20px; border: none; cursor: pointer; font-size: .9rem; background: #e0e0e0; color: #555; }
    .tab.active { background: #1a73e8; color: #fff; }
    .view { display: none; }
    .view.active { display: block; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
    th    { background: #1a73e8; color: #fff; text-align: left; padding: 10px 14px; font-size: .85rem; }
    td    { padding: 10px 14px; border-bottom: 1px solid #eee; font-size: .95rem; }
    tr:last-child td { border-bottom: none; }
    .stale { color: #e53935; }
    .ok    { color: #43a047; }
    .rename { margin-left: 8px; font-size: .7rem; padding: 2px 8px; border-radius: 10px;
              border: 1px solid #ddd; background: #fafafa; color: #888; cursor: pointer; opacity: 0; transition: opacity .12s; }
    tr:hover .rename { opacity: 1; }
    .rename:hover { background: #1a73e8; border-color: #1a73e8; color: #fff; }
    .devid { font-size: .7rem; color: #bbb; margin-top: 2px; }
    .delta { margin-top: 16px; padding: 12px 16px; background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1); font-size: .95rem; }
    .delta span { font-weight: 600; }
    .experiment { margin-top: 10px; padding: 10px 16px; background: #fff8e1; border: 1px solid #ffe082; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.06); font-size: .85rem; color: #6d4c00; }
    .experiment .arm { font-weight: 700; text-transform: uppercase; letter-spacing: .03em; padding: 2px 8px; background: #ffe082; border-radius: 12px; margin-left: 4px; }
    #updated { color: #aaa; font-size: .78rem; margin-top: 12px; }
    .chart-wrap { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 16px; }
    .chart-label { font-size: .8rem; font-weight: 600; color: #666; text-transform: uppercase; letter-spacing: .05em; margin: 0 0 6px 4px; }
  </style>
</head>
<body>
  <h1>Nest Fan Optimizer</h1>
  <p class="sub">Updates instantly via WebSocket</p>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('sensors')">Sensors</button>
    <button class="tab"        onclick="switchTab('graph')">Graph</button>
  </div>

  <div id="view-sensors" class="view active">
    <table>
      <thead><tr><th>Location</th><th>Temp</th><th>Humidity</th><th>Last seen</th></tr></thead>
      <tbody id="rows"><tr><td colspan="4" style="color:#aaa">Waiting for sensors...</td></tr></tbody>
    </table>
    <div class="delta" id="delta"></div>
    <div class="experiment" id="experiment" style="display:none"></div>
  </div>

  <div id="view-graph" class="view">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
      <p class="chart-label" style="margin:0">Temperature (°F)</p>
      <button onclick="resetZoom()" style="font-size:.8rem; padding:4px 12px; border-radius:12px; border:1px solid #ccc; background:#fff; cursor:pointer; color:#555;">Reset view</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-temp"></canvas></div>
    <p class="chart-label">Humidity (%)</p>
    <div class="chart-wrap"><canvas id="chart-humidity"></canvas></div>
  </div>

  <div id="updated"></div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
  <script>
    // ── Tab switching ─────────────────────────────────────────
    function switchTab(name) {
      document.querySelectorAll('.tab').forEach((t, i) => {
        const names = ['sensors', 'graph'];
        t.classList.toggle('active', names[i] === name);
      });
      document.querySelectorAll('.view').forEach(v => {
        v.classList.toggle('active', v.id === 'view-' + name);
      });
      if (name === 'graph') { chartTemp.resize(); chartHumidity.resize(); resetZoom(); }
    }

    // ── Charts ────────────────────────────────────────────────
    const COLORS = ['#1a73e8','#e53935','#43a047','#fb8c00','#8e24aa','#00acc1'];
    let colorIdx = 0;
    const colorByLocation = {};

    function locationColor(loc) {
      if (!colorByLocation[loc]) colorByLocation[loc] = COLORS[colorIdx++ % COLORS.length];
      return colorByLocation[loc];
    }

    // ── Overlay helpers ───────────────────────────────────────
    let fanEventsCache  = [];
    let hvacEventsCache = [];

    const OVERLAY = {
      fan:     { color: 'rgba(26,115,232,0.12)',  activeKey: 'ON' },
      COOLING: { color: 'rgba(255,152,0,0.15)'              },
      HEATING: { color: 'rgba(244,67,54,0.13)'              },
    };

    function buildBands(events, prefix, activeStates) {
      const boxes = {};
      let onTs = null, onState = null;
      events.forEach(e => {
        const active = activeStates.includes(e.state);
        if (active && onTs === null) { onTs = e.ts; onState = e.state; }
        if (!active && onTs !== null) {
          const color = prefix === 'fan'
            ? OVERLAY.fan.color
            : (OVERLAY[onState]?.color ?? 'rgba(0,0,0,0.08)');
          boxes[prefix + '_' + onTs] = { type: 'box', xMin: onTs, xMax: e.ts,
            backgroundColor: color, borderWidth: 0 };
          onTs = null; onState = null;
        }
      });
      if (onTs !== null) {
        const color = prefix === 'fan'
          ? OVERLAY.fan.color
          : (OVERLAY[onState]?.color ?? 'rgba(0,0,0,0.08)');
        boxes[prefix + '_open'] = { type: 'box', xMin: onTs, xMax: Date.now(),
          backgroundColor: color, borderWidth: 0 };
      }
      return boxes;
    }

    function refreshAnnotations() {
      const boxes = {
        ...buildBands(fanEventsCache,  'fan',  ['ON']),
        ...buildBands(hvacEventsCache, 'hvac', ['COOLING', 'HEATING']),
      };
      [chartTemp, chartHumidity].forEach(c => {
        c.options.plugins.annotation.annotations = boxes;
        c.update('none');
      });
    }

    function loadFanEvents() {
      fetch('/api/fan_events').then(r => r.json()).then(evts => {
        fanEventsCache = evts;
        refreshAnnotations();
      });
    }

    function loadHvacEvents() {
      fetch('/api/hvac_events').then(r => r.json()).then(evts => {
        hvacEventsCache = evts;
        refreshAnnotations();
      });
    }

    const WINDOW_12H  = 12 * 3600 * 1000;
    const WINDOW_3DAY = 3  * 86400 * 1000;

    function makeChart(canvasId, yLabel) {
      return new Chart(document.getElementById(canvasId), {
        type: 'line',
        data: { datasets: [] },
        options: {
          animation: false,
          responsive: true,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { position: 'top' },
            annotation: { annotations: {} },
            zoom: {
              limits: {
                x: { min: Date.now() - WINDOW_3DAY, minRange: 3600000 },
              },
              pan:  { enabled: true, mode: 'x' },
              zoom: {
                wheel:  { enabled: true },
                pinch:  { enabled: true },
                mode:   'x',
              },
            },
          },
          scales: {
            x: {
              type: 'time',
              min:  Date.now() - WINDOW_12H,
              time: { tooltipFormat: 'h:mm:ss a', displayFormats: { minute: 'h:mm a', hour: 'h a' } },
            },
            y: { title: { display: true, text: yLabel } },
          },
        },
      });
    }

    function resetZoom() {
      const now = Date.now();
      [chartTemp, chartHumidity].forEach(c => {
        c.resetZoom();                       // clears the plugin's stored pan/zoom state
        c.options.scales.x.min = now - WINDOW_12H;
        c.options.scales.x.max = now;
        c.update('none');                    // re-render without animation; no plugin state to override
      });
    }

    const chartTemp     = makeChart('chart-temp',     'Temperature (°F)');
    const chartHumidity = makeChart('chart-humidity', 'Humidity (%)');
    const dsByLocTemp     = {};
    const dsByLocHumidity = {};

    function ensureDataset(chart, cache, location) {
      if (!cache[location]) {
        const color = locationColor(location);
        const ds = { label: location, data: [], borderColor: color, backgroundColor: color + '22', borderWidth: 2, pointRadius: 2, tension: 0.3 };
        cache[location] = ds;
        chart.data.datasets.push(ds);
      }
      return cache[location];
    }

    function loadHistory() {
      fetch('/api/history').then(r => r.json()).then(h => {
        Object.entries(h).forEach(([loc, pts]) => {
          ensureDataset(chartTemp,     dsByLocTemp,     loc).data = pts.map(p => ({ x: p.ts, y: p.temp_f }));
          ensureDataset(chartHumidity, dsByLocHumidity, loc).data = pts.filter(p => p.humidity != null).map(p => ({ x: p.ts, y: p.humidity }));
        });
        chartTemp.update();
        chartHumidity.update();
      });
    }

    // ── Age countdown ─────────────────────────────────────────
    const seenAt = {};   // { location: Date.now() when reading arrived }

    function fmtAge(ms) {
      const s = Math.floor(ms / 1000);
      if (s < 60)   return s + 's ago';
      if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's ago';
      return Math.floor(s / 3600) + 'h ago';
    }

    function tickAges() {
      const now = Date.now();
      document.querySelectorAll('[data-location]').forEach(el => {
        const loc = el.dataset.location;
        if (!seenAt[loc]) return;
        const ms    = now - seenAt[loc];
        const stale = ms > 600000;
        el.className = stale ? 'stale' : 'ok';
        el.textContent = fmtAge(ms);
      });
    }

    function esc(s) {
      return String(s).replace(/[&<>"']/g, c => (
        {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
      ));
    }

    async function renameDevice(deviceId, current) {
      const next = prompt(
        `Which room is this sensor in now?\n\n` +
        `Device (flashed name): ${deviceId}\n` +
        `Leave blank to reset it back to the flashed name.`,
        current);
      if (next === null) return;                       // cancelled

      const body = { location: next.trim() === '' ? null : next.trim() };
      const res  = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/location`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert('Could not rename: ' + (err.error || res.status));
        return;
      }
      // Old series keeps its history on the chart; the new name starts fresh.
      delete seenAt[current];
      const status = await fetch('/api/status').then(r => r.json());
      render(status);
    }

    function render(d) {
      const now   = Date.now();
      const tbody = document.getElementById('rows');

      if (!d.sensors.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:#aaa">Waiting for sensors...</td></tr>';
      } else {
        tbody.innerHTML = d.sensors.map(s => {
          // Anchor the timestamp: if we have a local record use it, otherwise
          // back-calculate from age_seconds so the counter starts correctly.
          if (!seenAt[s.location]) seenAt[s.location] = now - s.age_seconds * 1000;
          const ms    = now - seenAt[s.location];
          const stale = ms > 600000;
          const sub = s.renamed
            ? `<div class="devid">device: ${esc(s.device_id)}</div>` : '';
          return `<tr>
            <td>
              <span>${esc(s.location)}</span>
              <button class="rename" title="Move this sensor to another room"
                      onclick="renameDevice(${JSON.stringify(s.device_id).replace(/"/g, '&quot;')},
                                            ${JSON.stringify(s.location).replace(/"/g, '&quot;')})">rename</button>
              ${sub}
            </td>
            <td>${s.temp_f.toFixed(1)} °F</td>
            <td>${s.humidity ?? '—'} %</td>
            <td class="${stale ? 'stale' : 'ok'}" data-location="${esc(s.location)}">${fmtAge(ms)}</td>
          </tr>`;
        }).join('');
      }

      const deltaEl = document.getElementById('delta');
      if (d.delta_f !== null) {
        const running = d.fan_state === 'ON';
        deltaEl.innerHTML = `Floor delta: <span>${d.delta_f.toFixed(1)} °F</span> &nbsp;|&nbsp; `
          + `Threshold: ${d.threshold_f} °F &nbsp;|&nbsp; `
          + `Fan: <span class="${running ? 'stale' : 'ok'}">${running ? 'RUNNING' : 'OFF'}</span>`;
      } else {
        deltaEl.innerHTML = 'Waiting for 2+ sensors to compute delta...';
      }

      const expEl = document.getElementById('experiment');
      if (d.experiment && d.experiment.enabled) {
        const names = {
          control:          'Control (today’s logic)',
          higher_threshold: 'Higher threshold',
          burst:            'Morning burst',
          duty_cycle:       'Duty cycle',
        };
        const arm = d.experiment.arm;
        expEl.style.display = '';
        expEl.innerHTML = `🧪 Today's experiment arm: <span class="arm">${names[arm] || arm}</span>`
          + ` &nbsp;<span style="color:#9b7b00">(rotates daily — see /api/experiment)</span>`;
      } else {
        expEl.style.display = 'none';
      }

      document.getElementById('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
    }

    function onSensorUpdate(d) {
      // Reset the timestamp for any sensor that just sent a reading
      const now = Date.now();
      d.sensors.forEach(s => { seenAt[s.location] = now - s.age_seconds * 1000; });
      render(d);
    }

    // Tick the age counters every second
    setInterval(tickAges, 1000);

    // Load current state immediately on page open
    fetch('/api/status').then(r => r.json()).then(render);
    loadHistory();
    loadFanEvents();
    loadHvacEvents();

    // Live updates via WebSocket — no polling needed
    const socket = io();
    socket.on('sensor_update', onSensorUpdate);
    socket.on('fan_event', evt => {
      fanEventsCache.push(evt);
      refreshAnnotations();
    });
    socket.on('hvac_event', evt => {
      hvacEventsCache.push(evt);
      refreshAnnotations();
    });
    socket.on('history_point', ({ location, point }) => {
      const dsT = ensureDataset(chartTemp,     dsByLocTemp,     location);
      dsT.data.push({ x: point.ts, y: point.temp_f });
      if (dsT.data.length > 300) dsT.data.shift();
      chartTemp.update('none');

      if (point.humidity != null) {
        const dsH = ensureDataset(chartHumidity, dsByLocHumidity, location);
        dsH.data.push({ x: point.ts, y: point.humidity });
        if (dsH.data.length > 300) dsH.data.shift();
        chartHumidity.update('none');
      }
    });
    socket.on('connect',    () => { document.getElementById('updated').textContent = 'Connected ✓'; loadHistory(); loadFanEvents(); loadHvacEvents(); });
    socket.on('disconnect', () => document.getElementById('updated').textContent = 'Disconnected — reconnecting...');
  </script>
</body>
</html>""", 200


# ── SDM helpers ───────────────────────────────────────────────
TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_BASE  = "https://smartdevicemanagement.googleapis.com/v1"

_token_cache: dict = {}


def get_access_token() -> str:
    if _token_cache.get("expires_at", 0) > time.time() + 60:
        return _token_cache["token"]
    resp = req.post(TOKEN_URL, data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=10)
    resp.raise_for_status()
    j = resp.json()
    _token_cache["token"] = j["access_token"]
    _token_cache["expires_at"] = time.time() + j.get("expires_in", 3600)
    return _token_cache["token"]


def list_devices(token: str) -> list:
    resp = req.get(
        f"{SDM_BASE}/enterprises/{SDM_PROJECT_ID}/devices",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("devices", [])


def find_thermostat(token: str) -> dict | None:
    for d in list_devices(token):
        if "sdm.devices.traits.Fan" in d.get("traits", {}):
            return d
    return None


def fan_mode(device: dict) -> str:
    return device["traits"].get("sdm.devices.traits.Fan", {}).get("timerMode", "OFF")


def set_fan(device_name: str, mode: str, duration_s: int | None, token: str):
    params: dict = {"timerMode": mode}
    if mode == "ON" and duration_s:
        params["duration"] = f"{duration_s}s"
    resp = req.post(
        f"{SDM_BASE}/{device_name}:executeCommand",
        json={"command": "sdm.devices.commands.Fan.SetTimer", "params": params},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    log.info("Fan → %s%s", mode,
             f" for {duration_s // 60} min" if mode == "ON" and duration_s else "")
    global _last_fan_state
    if mode != _last_fan_state:
        _last_fan_state = mode
        event = {"ts": time.time() * 1000, "state": mode}
        fan_events.append(event)
        socketio.emit("fan_event", event)
        threading.Thread(target=db_write_fan_event, daemon=True,
                         args=(event["ts"], mode)).start()


def set_cool_setpoint(device_name: str, target_f: float, token: str):
    """Command the Nest's cool setpoint. Clamped to the safety bounds."""
    clamped = max(SETPOINT_MIN_F, min(SETPOINT_MAX_F, target_f))
    if clamped != target_f:
        log.warning("Setpoint %.1f°F outside [%.1f, %.1f] — clamped to %.1f°F",
                    target_f, SETPOINT_MIN_F, SETPOINT_MAX_F, clamped)
    celsius = round((clamped - 32) * 5 / 9, 1)
    resp = req.post(
        f"{SDM_BASE}/{device_name}:executeCommand",
        json={"command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool",
              "params": {"coolCelsius": celsius}},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    log.info("🎯 Setpoint → %.1f°F (%.1f°C)", clamped, celsius)
    return clamped


# ── Quiet hours ───────────────────────────────────────────────
def _parse_hhmm(s: str):
    h, m = map(int, s.split(":"))
    return h * 60 + m

def quiet_hours_enabled() -> bool:
    return bool(FAN_QUIET_START.strip() and FAN_QUIET_END.strip()
                and FAN_QUIET_START.strip() != FAN_QUIET_END.strip())

def in_quiet_hours(now_dt: datetime | None = None) -> bool:
    if not quiet_hours_enabled():
        return False
    now  = now_dt or datetime.now()
    mins = now.hour * 60 + now.minute
    start = _parse_hhmm(FAN_QUIET_START)
    end   = _parse_hhmm(FAN_QUIET_END)
    if start > end:          # spans midnight (e.g. 22:00 → 06:30)
        return mins >= start or mins < end
    return start <= mins < end


# ── Experiment: randomized daily treatment arms ───────────────
# Goal: find out whether any alternative fan strategy beats what's running
# today ("control" = the exact `delta > threshold` logic above), without ever
# being riskier than today. Design notes:
#
#   • One arm is active for an entire calendar day — long enough for the
#     thermal system (which has hours of lag) to actually respond to it,
#     and short enough that ~3 weeks gives each arm a good sample of
#     different weather/seasonal conditions.
#   • Arms are assigned via a *randomized complete block* schedule: every
#     run of len(EXPERIMENT_ARMS) consecutive days is a "block" containing
#     each arm exactly once, in an order shuffled by a date-derived seed.
#     This avoids both (a) pure-random streaks (e.g. "control" 5 days running
#     by bad luck) and (b) a fixed rotation that could alias with a weekly
#     weather pattern — while staying perfectly deterministic, so a hub
#     restart mid-day always recomputes the *same* arm for that date.
#   • "control" is always in the rotation and is also the fallback for any
#     unrecognized arm name — so the worst case is identical to today.
_EXPERIMENT_EPOCH = date(2026, 1, 1)

def arm_for_date(d: date) -> str:
    """Deterministically pick today's experiment arm via a seeded shuffle
    of a balanced block — see design notes above."""
    block_len = len(EXPERIMENT_ARMS)
    day_index = (d - _EXPERIMENT_EPOCH).days
    block_index, position = divmod(day_index, block_len)
    seed = hashlib.sha256(f"{EXPERIMENT_SEED_SALT}:{block_index}".encode()).digest()
    shuffled = EXPERIMENT_ARMS[:]
    random.Random(seed).shuffle(shuffled)
    return shuffled[position]


def _minutes_since_quiet_hours_end(now_dt: datetime):
    """Minutes elapsed since quiet hours last ended, or None if we're
    currently inside quiet hours (caller already gates on that, but this
    stays safe to call standalone). With quiet hours disabled the "burst"
    arm still needs a morning anchor, so fall back to the old 06:30 end."""
    if in_quiet_hours(now_dt):
        return None
    end_mins = _parse_hhmm(FAN_QUIET_END) if quiet_hours_enabled() else _parse_hhmm("06:30")
    now_mins = now_dt.hour * 60 + now_dt.minute + now_dt.second / 60
    elapsed = now_mins - end_mins
    if elapsed < 0:          # we're past midnight, before quiet hours starts again
        elapsed += 24 * 60
    return elapsed


def _duty_cycle_on_phase(now_dt: datetime, on_minutes: int, off_minutes: int) -> bool:
    """A repeating on/off window anchored to midnight, so the phase is
    stable across restarts (no drift, no need to persist phase state)."""
    cycle = on_minutes + off_minutes
    mins = now_dt.hour * 60 + now_dt.minute + now_dt.second / 60
    return (mins % cycle) < on_minutes


def _set_thermostat_snapshot(mode, cool_f, heat_f, hvac_status, eco):
    """Track what the thermostat is targeting.

    The setpoint is changed by hand as well as on a schedule, so it moves
    unpredictably. Every change is written as its own timestamped row (the same
    write-on-change pattern as fan/hvac events) — that keeps the table small and
    makes "what was the target at time T" a simple as-of lookup, while a manual
    nudge shows up as an explicit event rather than being smeared across
    two-minute samples.
    """
    global _thermostat_state
    snapshot = {"mode": mode, "cool_f": cool_f, "heat_f": heat_f,
                "hvac_status": hvac_status, "eco": eco}

    prev = _thermostat_state
    # hvac_status flips constantly as the compressor cycles; it already has its
    # own event log, so don't let it trigger a setpoint row.
    watched = ("mode", "cool_f", "heat_f", "eco")
    changed = prev is None or any(prev.get(k) != snapshot[k] for k in watched)

    _thermostat_state = {**snapshot, "updated_at": time.time()}
    if not changed:
        return

    if prev is not None and prev.get("cool_f") != cool_f:
        log.info("🎯 Cool setpoint changed: %s°F → %s°F",
                 prev.get("cool_f"), cool_f)
        event = {"ts": time.time() * 1000, "cool_f": cool_f,
                 "prev_cool_f": prev.get("cool_f"), "mode": mode}
        setpoint_events.append(event)
        socketio.emit("setpoint_event", event)
    elif prev is not None:
        log.info("🎯 Thermostat changed: mode=%s eco=%s cool=%s°F", mode, eco, cool_f)

    threading.Thread(target=db_write_thermostat_state, daemon=True,
                     args=(time.time(), mode, cool_f, heat_f, hvac_status, eco)).start()


def _parse_occupancy_schedule(spec: str) -> list:
    """'07:00-22:00=upstairs: living room@74' -> [(start_min, end_min, loc, cap)]"""
    windows = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            times, target = chunk.split("=", 1)
            start_s, end_s = times.split("-", 1)
            loc, cap = target, None
            if "@" in target:
                loc, cap_s = target.rsplit("@", 1)
                cap = float(cap_s)
            windows.append((_parse_hhmm(start_s.strip()), _parse_hhmm(end_s.strip()),
                            loc.strip(), cap))
        except Exception as e:
            log.error("Bad OCCUPANCY_SCHEDULE entry %r (%s) — ignoring", chunk, e)
    return windows


_OCCUPANCY_WINDOWS = _parse_occupancy_schedule(OCCUPANCY_SCHEDULE)


def occupancy_target(now_dt: datetime):
    """Which room matters right now, and the cap it must stay under."""
    mins = now_dt.hour * 60 + now_dt.minute
    for start, end, loc, cap in _OCCUPANCY_WINDOWS:
        active = (mins >= start or mins < end) if start > end else (start <= mins < end)
        if active:
            return loc, (cap if cap is not None else COMFORT_MAX_F)
    return None, COMFORT_MAX_F


_SETPOINT_WINDOWS = _parse_occupancy_schedule(
    # Reuse the same parser; the "location" slot carries the target °F.
    "; ".join(f"{w}" for w in SETPOINT_SCHEDULE.split(";"))
) if SETPOINT_SCHEDULE.strip() else []


def setpoint_target(now_dt: datetime):
    """(target_f, window_key) for right now, or (None, None) outside any window."""
    mins = now_dt.hour * 60 + now_dt.minute
    for start, end, target, _cap in _SETPOINT_WINDOWS:
        active = (mins >= start or mins < end) if start > end else (start <= mins < end)
        if active:
            try:
                target_f = float(target)
            except ValueError:
                log.error("Bad SETPOINT_SCHEDULE target %r — ignoring", target)
                return None, None
            # Key the window to a date so a human override expires at the next
            # transition rather than lasting forever. Overnight windows are
            # keyed to the day they began.
            day = now_dt.date()
            if start > end and mins < end:
                day = day - timedelta(days=1)
            return target_f, f"{day.isoformat()}:{start}"
    return None, None


def setpoint_control_active(now_dt: datetime) -> tuple[bool, str]:
    if not SETPOINT_CONTROL_ENABLED:
        return False, "disabled"
    if SETPOINT_CONTROL_UNTIL.strip():
        try:
            until = date.fromisoformat(SETPOINT_CONTROL_UNTIL.strip())
            if now_dt.date() > until:
                return False, f"expired {until.isoformat()}"
        except ValueError:
            log.error("Bad SETPOINT_CONTROL_UNTIL %r — treating as no expiry",
                      SETPOINT_CONTROL_UNTIL)
    return True, "active"


def manage_setpoint(device_name: str, current_f: float | None,
                    mode: str | None, now_dt: datetime, token: str):
    """Drive the cool setpoint on a schedule, yielding to any human change."""
    global _setpoint_commanded, _setpoint_yielded_window

    active, why = setpoint_control_active(now_dt)
    if not active:
        return
    if mode != "COOL":
        log.info("Setpoint control idle — thermostat mode is %s, not COOL", mode)
        return
    if current_f is None:
        return

    target_f, window = setpoint_target(now_dt)
    if target_f is None:
        return

    # New window (or first run): a previous window's override doesn't carry
    # over, so forget what we commanded and re-assert the schedule.
    global _setpoint_last_window
    if window != _setpoint_last_window:
        if _setpoint_yielded_window is not None:
            log.info("New setpoint window — resuming schedule control")
        _setpoint_last_window   = window
        _setpoint_commanded     = None
        _setpoint_yielded_window = None

    if _setpoint_yielded_window == window:
        return   # conceded to a human for the rest of this window

    # A human moved it: it's neither what we last commanded nor the target.
    if _setpoint_commanded is not None \
            and abs(current_f - _setpoint_commanded) > SETPOINT_TOLERANCE_F \
            and abs(current_f - target_f) > SETPOINT_TOLERANCE_F:
        _setpoint_yielded_window = window
        log.warning("🙋 Setpoint changed by hand to %.1f°F (we had set %.1f°F) — "
                    "yielding until the next window", current_f, _setpoint_commanded)
        return

    if abs(current_f - target_f) <= SETPOINT_TOLERANCE_F:
        _setpoint_commanded = current_f    # already where we want it
        return

    try:
        _setpoint_commanded = set_cool_setpoint(device_name, target_f, token)
        log.info("   (scheduled target for this window: %.1f°F)", target_f)
    except Exception as e:
        log.error("Setpoint command failed: %s", e)


def comfort_state(fresh: dict, now_dt: datetime | None = None) -> dict:
    """Summarise the house against the comfort objective.

    occupied  — the room that matters right now, per OCCUPANCY_SCHEDULE
    reservoir — coldest basement sensor (the free cooling we can draw on)
    gradient  — how much usable cooling sits below that room

    Returns ok=False with a reason when it can't be evaluated, so callers can
    fall back rather than act on a guess.
    """
    now_dt = now_dt or datetime.now()
    res = {loc: d["temp_f"] for loc, d in fresh.items()
           if loc.startswith(RESERVOIR_PREFIX)}
    occ = {loc: d["temp_f"] for loc, d in fresh.items()
           if loc.startswith(OCCUPIED_PREFIX)}

    if not res:
        return {"ok": False, "why": "no reservoir sensor reporting"}
    if not occ:
        return {"ok": False, "why": "no occupied sensor reporting"}

    target_loc, cap = occupancy_target(now_dt)

    if target_loc and target_loc in occ:
        occ_loc, basis = target_loc, "scheduled"
    else:
        # Scheduled room is stale or unmapped — fall back to the hottest
        # occupied room so a dead battery degrades rather than blinds us.
        occ_loc = max(occ, key=occ.get)
        basis = ("fallback: %s not reporting" % target_loc) if target_loc \
                else "fallback: no window matches"

    res_loc = min(res, key=res.get)
    occupied, reservoir = occ[occ_loc], res[res_loc]
    gradient = occupied - reservoir

    too_warm     = occupied >= cap - COMFORT_DEADBAND_F
    worth_moving = gradient >= MIN_GRADIENT_F

    return {
        "ok":            True,
        "occupied":      occupied,
        "occupied_loc":  occ_loc,
        "basis":         basis,
        "cap_f":         cap,
        "reservoir":     reservoir,
        "reservoir_loc": res_loc,
        "gradient":      gradient,
        "too_warm":      too_warm,
        "worth_moving":  worth_moving,
        "should_cool":   too_warm and worth_moving,
        "over_cap":      occupied > cap,
    }


def decide_fan_mode(delta: float, arm: str, now_dt: datetime):
    """Translate (delta, active arm, time) into a fan decision.

    Returns (desired_mode, threshold_used, run_duration_s, note) where `note`
    is a short human-readable explanation of *why*, for the logs/dashboard.
    Every branch still respects the measured delta — none of these arms will
    run the fan when there's no temperature difference to move around;  they
    only vary *how readily* / *how long* / *on what schedule* it kicks in.
    """
    if arm == "higher_threshold":
        # Phase 2 hypothesis (9°F): only run the duty-cycle pattern when the delta
        # is genuinely large — saves the most runtime on well-mixed days while still
        # circulating air on the hottest days. 6°F saved nothing (was non-binding on
        # 5/6 days); 9°F is nearer the p90 and should gate more meaningfully.
        threshold = EXPERIMENT_HIGH_THRESHOLD_F
        on_phase  = _duty_cycle_on_phase(now_dt, EXPERIMENT_DUTY_ON_MINUTES, EXPERIMENT_DUTY_OFF_MINUTES)
        desired   = "ON" if (on_phase and delta > threshold) else "OFF"
        return desired, threshold, EXPERIMENT_DUTY_ON_MINUTES * 60, \
            f"higher_threshold: Δ {delta:.1f}°F vs {threshold:.1f}°F, " \
            f"{'ON' if on_phase else 'OFF'}-phase"

    if arm == "burst":
        # Hypothesis: most of the mixing happens soon after the morning
        # ramp-up (per the event-study "elbow"); a finite morning burst may
        # capture most of the benefit for a fraction of the runtime/wear.
        threshold = TEMP_DELTA_THRESHOLD_F
        elapsed = _minutes_since_quiet_hours_end(now_dt)
        in_window = elapsed is not None and elapsed < EXPERIMENT_BURST_MINUTES
        desired = "ON" if (in_window and delta > threshold) else "OFF"
        return desired, threshold, FAN_RUN_DURATION_SECONDS, \
            (f"burst: {elapsed:.0f}/{EXPERIMENT_BURST_MINUTES}min since quiet hours ended"
             if elapsed is not None else "burst: in quiet hours")

    if arm == "duty_cycle":
        # Hypothesis: continuous mixing isn't necessary — short on/off
        # pulses might sustain most of the benefit at a fraction of the
        # runtime (less wear, quieter house, same comfort).
        threshold = TEMP_DELTA_THRESHOLD_F
        on_phase = _duty_cycle_on_phase(now_dt, EXPERIMENT_DUTY_ON_MINUTES, EXPERIMENT_DUTY_OFF_MINUTES)
        desired = "ON" if (on_phase and delta > threshold) else "OFF"
        return desired, threshold, EXPERIMENT_DUTY_ON_MINUTES * 60, \
            f"duty_cycle: {'ON' if on_phase else 'OFF'}-phase " \
            f"({EXPERIMENT_DUTY_ON_MINUTES}m on / {EXPERIMENT_DUTY_OFF_MINUTES}m off)"

    # Fallback (includes old "control" and "duty_cycle" arm names from phase 1) —
    # duty_cycle is now the production default: 15 min on / 15 min off when Δ > threshold.
    threshold = TEMP_DELTA_THRESHOLD_F
    on_phase = _duty_cycle_on_phase(now_dt, EXPERIMENT_DUTY_ON_MINUTES, EXPERIMENT_DUTY_OFF_MINUTES)
    desired = "ON" if (on_phase and delta > threshold) else "OFF"
    return desired, threshold, EXPERIMENT_DUTY_ON_MINUTES * 60, \
        f"duty_cycle [default]: {'ON' if on_phase else 'OFF'}-phase " \
        f"({EXPERIMENT_DUTY_ON_MINUTES}m on / {EXPERIMENT_DUTY_OFF_MINUTES}m off)"


def ensure_experiment_logged(d: date, arm: str):
    """Write today's assigned arm to the DB exactly once (restart-safe)."""
    key = d.isoformat()
    if key in _experiment_dates_logged:
        return
    _experiment_dates_logged.add(key)
    log.info("🧪 Experiment arm for %s → %s", key, arm)
    threading.Thread(target=db_write_experiment_arm, daemon=True,
                     args=(key, arm, time.time())).start()


# ── Fan control loop ──────────────────────────────────────────
def fan_control_loop():
    log.info("Fan control loop started (checks every %ds)", CHECK_INTERVAL_SECONDS)
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        try:
            evaluate_and_act()
        except Exception as exc:
            log.error("Fan control error: %s", exc, exc_info=True)


def evaluate_and_act():
    now = time.time()

    # ── Poll Nest first so it's always in sensor_data ─────────
    token      = get_access_token()
    thermostat = find_thermostat(token)
    if thermostat is None:
        log.error("No thermostat found via SDM API.")
        return

    traits   = thermostat.get("traits", {})
    temp_c   = traits.get("sdm.devices.traits.Temperature", {}).get("ambientTemperatureCelsius")
    humidity = traits.get("sdm.devices.traits.Humidity",    {}).get("ambientHumidityPercent")
    room     = (thermostat.get("parentRelations") or [{}])[0].get("displayName", "thermostat")
    nest_device_id = f"nest: {room.lower()}"
    location = resolve_location(nest_device_id)

    if temp_c is not None:
        temp_f = round(temp_c * 9 / 5 + 32, 1)
        ts = time.time()
        with sensor_lock:
            sensor_data[location] = {
                "temp_f":      temp_f,
                "temp_c":      round(temp_c, 1),
                "humidity":    humidity,
                "received_at": ts,
                "device_id":   nest_device_id,
            }
            if location not in sensor_history:
                sensor_history[location] = deque(maxlen=HISTORY_MAX)
            sensor_history[location].append({
                "ts":       ts * 1000,
                "temp_f":   temp_f,
                "humidity": humidity,
            })
        hvac_status = traits.get("sdm.devices.traits.ThermostatHvac", {}).get("status", "OFF")

        # Record what the thermostat is targeting — it moves on a schedule, and
        # without it "the AC ran more today" can't be separated from
        # "the target was colder today".
        sp      = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
        mode    = traits.get("sdm.devices.traits.ThermostatMode", {}).get("mode")
        eco     = traits.get("sdm.devices.traits.ThermostatEco", {}).get("mode")
        cool_f  = round(sp["coolCelsius"] * 9 / 5 + 32, 1) if sp.get("coolCelsius") is not None else None
        heat_f  = round(sp["heatCelsius"] * 9 / 5 + 32, 1) if sp.get("heatCelsius") is not None else None
        _set_thermostat_snapshot(mode, cool_f, heat_f, hvac_status, eco)

        log.info("❶ Nest         %-15s  %.1f°F  %s%% RH  fan=%s  hvac=%s  mode=%s  set=%s°F",
                 location, temp_f, humidity or "?", fan_mode(thermostat), hvac_status,
                 mode, f"{cool_f:.1f}" if cool_f is not None else "?")
        threading.Thread(target=db_write_reading, daemon=True,
                         args=(ts, location, temp_f, round(temp_c, 1), humidity)).start()

        # Drive the target on a schedule (no-op unless explicitly enabled).
        manage_setpoint(thermostat["name"], cool_f, mode, datetime.now(), token)

        global _last_hvac_state
        if hvac_status != _last_hvac_state:
            _last_hvac_state = hvac_status
            hvac_event = {"ts": ts * 1000, "state": hvac_status}
            hvac_events.append(hvac_event)
            socketio.emit("hvac_event", hvac_event)
            threading.Thread(target=db_write_hvac_event, daemon=True,
                             args=(ts * 1000, hvac_status)).start()

        socketio.emit("sensor_update", _dashboard_payload())

    # ── Now check delta across all fresh sensors ───────────────
    with sensor_lock:
        fresh = {
            loc: d for loc, d in sensor_data.items()
            if now - d["received_at"] <= SENSOR_STALE_SECONDS
        }

    if len(fresh) < 2:
        log.warning("Only %d fresh sensor(s) — need ≥2 to compute delta. "
                    "Waiting for more heartbeats.", len(fresh))
        return

    temps = {loc: d["temp_f"] for loc, d in fresh.items()}
    max_temp = max(temps.values())
    min_temp = min(temps.values())
    delta = max_temp - min_temp
    hot_loc = max(temps, key=temps.get)
    cold_loc = min(temps, key=temps.get)

    log.info("Δ %.1f°F  |  %s=%.1f°F (hot)  %s=%.1f°F (cold)  |  threshold=%.1f°F",
             delta, hot_loc, max_temp, cold_loc, min_temp, TEMP_DELTA_THRESHOLD_F)

    current_mode = fan_mode(thermostat)
    device_name  = thermostat["name"]

    now_dt = datetime.now()
    today  = now_dt.date()

    # Quiet hours are an unconditional override — they apply identically no
    # matter which experiment arm is active today, so this check happens
    # *before* any arm-aware logic runs (and short-circuits it entirely).
    if in_quiet_hours(now_dt):
        log.info("Quiet hours (%s–%s) — fan suppressed", FAN_QUIET_START, FAN_QUIET_END)
        if current_mode == "ON":
            log.info("Turning fan OFF for quiet hours")
            set_fan(device_name, "OFF", None, token)
        return

    # ── Experiment arm: pick today's strategy and act on it ────
    # "duty_cycle" here means the permanent default, not an active experiment
    # arm — it's what decide_fan_mode()'s fallback branch actually runs.
    arm = arm_for_date(today) if EXPERIMENT_ENABLED else "duty_cycle"
    ensure_experiment_logged(today, arm)

    global _last_logged_arm
    if arm != _last_logged_arm:
        log.info("🧪 Today's active experiment arm: %s", arm)
        _last_logged_arm = arm

    # ── Comfort gate ───────────────────────────────────────────
    # The arm decides *how* to cycle; this decides whether cycling is worth
    # anything right now. If the occupied rooms are comfortable, or the
    # basement has no cold left to give, the blower stays off.
    comfort = comfort_state(fresh, now_dt)
    if comfort["ok"]:
        log.info("🛋  %s=%.1f°F (cap %.1f, %s)  |  %s=%.1f°F  |  usable %.1f°F  → %s",
                 comfort["occupied_loc"], comfort["occupied"], comfort["cap_f"],
                 comfort["basis"],
                 comfort["reservoir_loc"], comfort["reservoir"], comfort["gradient"],
                 "cool it" if comfort["should_cool"] else "no action needed")
        if comfort["over_cap"]:
            log.warning("⚠️  %s is %.1f°F — above the %.1f°F comfort cap",
                        comfort["occupied_loc"], comfort["occupied"], comfort["cap_f"])

        if not comfort["should_cool"]:
            reason = (f"{comfort['occupied_loc']} comfortable" if not comfort["too_warm"]
                      else f"gradient only {comfort['gradient']:.1f}°F "
                           f"(<{MIN_GRADIENT_F:.1f}) — not worth circulating")
            if current_mode == "ON":
                log.info("[%s] comfort gate: %s → fan OFF", arm, reason)
                set_fan(device_name, "OFF", None, token)
            else:
                log.info("[%s] comfort gate: %s → fan already OFF.", arm, reason)
            return
    else:
        # Can't evaluate comfort (e.g. the living-room sensor is down). Fall
        # through to the delta-based behaviour rather than guessing.
        log.warning("Comfort gate unavailable (%s) — falling back to delta logic",
                    comfort["why"])

    desired_mode, threshold_used, run_duration, note = decide_fan_mode(delta, arm, now_dt)

    if desired_mode == "ON":
        log.info("[%s] %s → fan ON (%d min)", arm, note, run_duration // 60)
        set_fan(device_name, "ON", run_duration, token)
    else:
        if current_mode == "ON":
            log.info("[%s] %s → fan OFF", arm, note)
            set_fan(device_name, "OFF", None, token)
        else:
            log.info("[%s] %s → fan already OFF. Nothing to do.", arm, note)


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    log.info("═" * 60)
    log.info("Nest Fan Optimizer Hub starting")
    log.info("  Listening    : http://0.0.0.0:%d/sensor", HUB_PORT)
    log.info("  Dashboard    : http://localhost:%d/status", HUB_PORT)
    log.info("  Threshold    : %.1f°F delta", TEMP_DELTA_THRESHOLD_F)
    log.info("  Fan duration : %d min", FAN_RUN_DURATION_SECONDS // 60)
    log.info("  Stale after  : %ds", SENSOR_STALE_SECONDS)
    log.info("  Default mode : duty_cycle (%dm on / %dm off)",
             EXPERIMENT_DUTY_ON_MINUTES, EXPERIMENT_DUTY_OFF_MINUTES)
    log.info("  Objective    : hold the occupied room under its cap using '%s*' air",
             RESERVOIR_PREFIX)
    for start, end, loc, cap in _OCCUPANCY_WINDOWS:
        log.info("     %02d:%02d-%02d:%02d  %-28s cap %.1f°F",
                 start // 60, start % 60, end // 60, end % 60, loc,
                 cap if cap is not None else COMFORT_MAX_F)
    log.info("  Comfort gate : start %.1f°F below the window's cap, "
             "need %.1f°F usable gradient", COMFORT_DEADBAND_F, MIN_GRADIENT_F)
    log.info("  Quiet hours  : %s",
             f"{FAN_QUIET_START}–{FAN_QUIET_END}" if quiet_hours_enabled()
             else "disabled — fan may run overnight")
    _sp_active, _sp_why = setpoint_control_active(datetime.now())
    if _sp_active:
        log.info("  Setpoint ctrl: ON — hub drives the target (bounds %.1f–%.1f°F)",
                 SETPOINT_MIN_F, SETPOINT_MAX_F)
        for start, end, target, _ in _SETPOINT_WINDOWS:
            log.info("     %02d:%02d-%02d:%02d  target %s°F",
                     start // 60, start % 60, end // 60, end % 60, target)
        log.info("     manual changes win until the next window; expires %s",
                 SETPOINT_CONTROL_UNTIL or "never")
    else:
        log.info("  Setpoint ctrl: off (%s) — thermostat left alone", _sp_why)
    if EXPERIMENT_ENABLED:
        log.info("  Experiment   : ON — arms %s (vs duty_cycle baseline)", EXPERIMENT_ARMS)
        log.info("  Today's arm  : %s", arm_for_date(datetime.now().date()))
        log.info("  High thr.    : %.1f°F", EXPERIMENT_HIGH_THRESHOLD_F)
    else:
        log.info("  Experiment   : disabled (always running duty_cycle)")
    log.info("═" * 60)

    # Init database and preload history
    db_init()
    db_load_history()
    db_load_experiment_log()
    db_load_device_locations()

    # Start fan control loop in background thread
    t = threading.Thread(target=fan_control_loop, daemon=True)
    t.start()

    # Start Flask-SocketIO (blocks)
    socketio.run(app, host="0.0.0.0", port=HUB_PORT, debug=False, allow_unsafe_werkzeug=True)
