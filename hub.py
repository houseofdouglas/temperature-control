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
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime
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
FAN_QUIET_START       = os.getenv("FAN_QUIET_START", "22:00")   # fan off after this time
FAN_QUIET_END         = os.getenv("FAN_QUIET_END",   "06:30")   # fan back on after this time

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Sensor store ──────────────────────────────────────────────
# { location: { temp_f, humidity, received_at } }
sensor_data: dict = {}
# { location: deque([{ts, temp_f, humidity}, ...]) }
sensor_history: dict = {}
HISTORY_MAX = 1000  # keep last 1000 readings per sensor (~3.5 days at 5 min)
sensor_lock = threading.Lock()

# ── Fan event log ─────────────────────────────────────────────
# [{ ts_ms, state: "ON"|"OFF" }, ...]  — only state-change events
fan_events: deque = deque(maxlen=500)
_last_fan_state: str = "OFF"

# ── HVAC event log ────────────────────────────────────────────
# [{ ts_ms, state: "COOLING"|"HEATING"|"OFF" }, ...]
hvac_events: deque = deque(maxlen=500)
_last_hvac_state: str = "OFF"

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

    location = data["location"]
    now = time.time()
    with sensor_lock:
        sensor_data[location] = {
            "temp_f":      data["temp_f"],
            "temp_c":      data.get("temp_c"),
            "humidity":    data.get("humidity"),
            "received_at": now,
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
                "temp_f":      d["temp_f"],
                "humidity":    d.get("humidity"),
                "age_seconds": int(now - d["received_at"]),
            }
            for loc, d in sorted(sensor_data.items())
        ]
    temps = [s["temp_f"] for s in sensors]
    delta = round(max(temps) - min(temps), 1) if len(temps) >= 2 else None
    return {"sensors": sensors, "delta_f": delta, "threshold_f": TEMP_DELTA_THRESHOLD_F}


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
    .delta { margin-top: 16px; padding: 12px 16px; background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1); font-size: .95rem; }
    .delta span { font-weight: 600; }
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
        c.options.scales.x.min = now - WINDOW_12H;
        c.options.scales.x.max = undefined;
        c.resetZoom();
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
          return `<tr>
            <td>${s.location}</td>
            <td>${s.temp_f.toFixed(1)} °F</td>
            <td>${s.humidity ?? '—'} %</td>
            <td class="${stale ? 'stale' : 'ok'}" data-location="${s.location}">${fmtAge(ms)}</td>
          </tr>`;
        }).join('');
      }

      const deltaEl = document.getElementById('delta');
      if (d.delta_f !== null) {
        const over = d.delta_f > d.threshold_f;
        deltaEl.innerHTML = `Floor delta: <span>${d.delta_f.toFixed(1)} °F</span> &nbsp;|&nbsp; `
          + `Threshold: ${d.threshold_f} °F &nbsp;|&nbsp; `
          + `Fan: <span class="${over ? 'stale' : 'ok'}">${over ? 'RUNNING' : 'OFF'}</span>`;
      } else {
        deltaEl.innerHTML = 'Waiting for 2+ sensors to compute delta...';
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


# ── Quiet hours ───────────────────────────────────────────────
def _parse_hhmm(s: str):
    h, m = map(int, s.split(":"))
    return h * 60 + m

def in_quiet_hours() -> bool:
    from datetime import datetime
    now  = datetime.now()
    mins = now.hour * 60 + now.minute
    start = _parse_hhmm(FAN_QUIET_START)
    end   = _parse_hhmm(FAN_QUIET_END)
    if start > end:          # spans midnight (e.g. 22:00 → 06:30)
        return mins >= start or mins < end
    return start <= mins < end


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
    location = f"nest: {room.lower()}"

    if temp_c is not None:
        temp_f = round(temp_c * 9 / 5 + 32, 1)
        ts = time.time()
        with sensor_lock:
            sensor_data[location] = {
                "temp_f":      temp_f,
                "temp_c":      round(temp_c, 1),
                "humidity":    humidity,
                "received_at": ts,
            }
            if location not in sensor_history:
                sensor_history[location] = deque(maxlen=HISTORY_MAX)
            sensor_history[location].append({
                "ts":       ts * 1000,
                "temp_f":   temp_f,
                "humidity": humidity,
            })
        hvac_status = traits.get("sdm.devices.traits.ThermostatHvac", {}).get("status", "OFF")
        log.info("❶ Nest         %-15s  %.1f°F  %s%% RH  fan=%s  hvac=%s",
                 location, temp_f, humidity or "?", fan_mode(thermostat), hvac_status)
        threading.Thread(target=db_write_reading, daemon=True,
                         args=(ts, location, temp_f, round(temp_c, 1), humidity)).start()

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
        socketio.emit("history_point", {
            "location": location,
            "point":    {"ts": ts * 1000, "temp_f": temp_f, "humidity": humidity},
        })

    current_mode = fan_mode(thermostat)
    device_name  = thermostat["name"]

    if in_quiet_hours():
        log.info("Quiet hours (%s–%s) — fan suppressed", FAN_QUIET_START, FAN_QUIET_END)
        if current_mode == "ON":
            log.info("Turning fan OFF for quiet hours")
            set_fan(device_name, "OFF", None, token)
        return

    if delta > TEMP_DELTA_THRESHOLD_F:
        log.info("Above threshold → fan ON (%d min)", FAN_RUN_DURATION_SECONDS // 60)
        set_fan(device_name, "ON", FAN_RUN_DURATION_SECONDS, token)
    else:
        if current_mode == "ON":
            log.info("Within threshold → fan OFF")
            set_fan(device_name, "OFF", None, token)
        else:
            log.info("Within threshold → fan already OFF. Nothing to do.")


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    log.info("═" * 60)
    log.info("Nest Fan Optimizer Hub starting")
    log.info("  Listening    : http://0.0.0.0:%d/sensor", HUB_PORT)
    log.info("  Dashboard    : http://localhost:%d/status", HUB_PORT)
    log.info("  Threshold    : %.1f°F delta", TEMP_DELTA_THRESHOLD_F)
    log.info("  Fan duration : %d min", FAN_RUN_DURATION_SECONDS // 60)
    log.info("  Stale after  : %ds", SENSOR_STALE_SECONDS)
    log.info("═" * 60)

    # Init database and preload history
    db_init()
    db_load_history()

    # Start fan control loop in background thread
    t = threading.Thread(target=fan_control_loop, daemon=True)
    t.start()

    # Start Flask-SocketIO (blocks)
    socketio.run(app, host="0.0.0.0", port=HUB_PORT, debug=False, allow_unsafe_werkzeug=True)
