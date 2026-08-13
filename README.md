# Nest Fan Optimizer

A home temperature-sensor network and control loop that decides when to run the
HVAC blower on a Nest thermostat — with the goal of keeping the rooms people
actually sit in comfortable while the air-conditioning compressor runs less.

ESP32 nodes and a Raspberry Pi report temperatures to a Flask hub. The hub
polls the Nest via Google's Smart Device Management API, decides whether
circulating air is worth it right now, and issues timed fan commands. Every
reading, fan command, HVAC state change and thermostat setpoint is logged to
SQLite so the control strategy can be evaluated after the fact rather than by
vibes.

> **Nothing here is a general-purpose product.** It is tuned to one house, one
> duct layout and one thermostat. The parts worth stealing are the measurement
> discipline and the analysis scripts, not the thresholds.

---

## The idea

A two-storey house with a basement stratifies: the basement sits in the low
60s°F year-round because it is coupled to the ground, while upstairs bakes.
The HVAC blower can move that cold basement air around without running the
compressor at all — the fan draws a fraction of the power the compressor does.

So: **can circulating basement air keep the upstairs comfortable, and displace
compressor runtime doing it?**

That is the current question. It is not the question the project started with,
which matters for reading the code and the data — see
[Two objectives](#two-objectives-read-this-before-the-data).

---

## Architecture

```
ESP32-C3 + DHT11  ──┐
ESP32-C3 + DHT11  ──┤
ESP32-C3 + DHT11  ──┼──HTTP POST /sensor──►  hub.py  ──SDM API──►  Nest thermostat
Raspberry Pi      ──┘        (5 min)         (Flask)   Fan.SetTimer
   + DHT11, cron                                │
                                                ├──►  SQLite  (nest.db)
                                                └──►  dashboard + WebSocket
                                                        localhost:5001/status
```

- **Sensor nodes** deep-sleep between readings and POST a small JSON heartbeat.
  They are dumb on purpose: no logic, no schedule, no thermostat access.
- **The hub** is the only thing that talks to the Nest. It polls every
  2 minutes, records what it sees, and issues `Fan.SetTimer` commands.
- **The Nest fan timer auto-expires**, so a hub crash fails safe — the fan
  stops on its own rather than running forever.

### Sensors are named by the hub, not by firmware

The string a node reports is its **permanent device id**, not its room. The hub
owns the mapping from device to room, so a sensor can be moved to a different
room from the dashboard without reflashing it:

```bash
curl -X POST http://localhost:5001/api/devices/basement%3A%20gracie/location \
  -H 'Content-Type: application/json' -d '{"location":"upstairs: office"}'
```

Historical readings are deliberately **not** relabelled — they really were
taken in the old room, and rewriting them would corrupt what the analysis
reads. A moved sensor's chart shows the old series ending and a new one
starting, which is the honest representation.

---

## Two objectives (read this before the data)

The project changed goals partway through, and the database spans both. Mixing
them silently produces nonsense, so the analysis refuses to.

**Phase 1 — minimise the floor-to-floor delta** (Jun 8 – Aug 11 2026).
Four sensors in the basement, one on the main floor. Run the fan when the
spread between floors exceeds a threshold. A five-week randomised A/B test
compared four cycling strategies; a 15-min-on / 15-min-off duty cycle won,
cutting blower runtime roughly in half versus running it continuously, for
about 0.7°F of extra spread. See `experiment_analysis.py` (kept, marked
superseded).

**Phase 2 — hold the occupied rooms under a comfort cap** (from Aug 12 2026).
Three of the basement sensors moved upstairs, which invalidated the old
premise. The metrics inverted:

| | Phase 1 | Phase 2 |
|---|---|---|
| Outcome | floor-to-floor delta | **compressor hours** |
| Delta is | the thing being minimised | a *resource* — how much cold air is available |
| Comfort is | implied by delta | an explicit **constraint** (cap °F) |
| Guardrails | — | hours over cap, worst excursion, manual setpoint overrides |

`comfort_analysis.py` implements Phase 2 and hard-separates the regimes: the
default window starts at the change, and `--all` still works but stamps every
figure with a "mixed regimes" warning.

---

## Two measurement traps

Most of the care in this repo goes into not fooling ourselves. Two specific
hazards drove real design decisions:

**1. The setpoint confound.** The thermostat target moves on a schedule *and*
gets changed by hand. Without recording it, "the AC ran more today" is
indistinguishable from "the target was colder today" — every comparison would
have been confounded. Setpoint is now logged as change-events
(`thermostat_state`) and travels alongside every AC number.

**2. The hallway blind spot.** The Nest decides when to cool based on its own
sensor, which sits in a hallway and runs up to **2.6°F cooler** than the living
room in the late afternoon. A fan that pushes cool air past the thermostat
could suppress the compressor while the room people occupy stays hot — a *win*
on the primary metric that is really a comfort regression. This is why an
independent living-room sensor is mandatory, and why `hall_bias` is reported
explicitly.

A related trick: **manual setpoint drops are treated as revealed comfort
failures.** If someone got up and turned the thermostat down, that is a better
comfort signal than any threshold we could invent, because it is the occupants
voting.

---

## Layout

| Path | What it is |
|---|---|
| `hub.py` | The whole running system — Flask receiver, SDM client, control loop, SQLite logging, dashboard |
| `pi_sensor/sensor.py` | Raspberry Pi sensor node (DHT11, cron-driven) |
| `firmware/sensor_node/` | ESP32-C3 firmware (Arduino). Copy `config.h.example` → `config.h` |
| `comfort_analysis.py` | **Current** analysis — compressor hours vs comfort guardrails |
| `analyze.py` | Shared loaders/helpers: timestamp handling, hub-outage detection, weather join |
| `fan_effect.py` | Event study: does the fan measurably do anything? |
| `experiment_analysis.py` | Phase 1 A/B arm comparison (superseded, kept reproducible) |
| `fetch_weather.py` | Backfills outdoor temperature from Open-Meteo (free, no key) |
| `get_token.py`, `exchange_code.py`, `list_devices.py` | One-time Google OAuth setup and device inspection |
| `fan_optimizer.py` | The original standalone poller that predates `hub.py`. Superseded |
| `deploy/` | launchd unit so the hub restarts on crash and boot |

---

## Setup

### 1. Google Device Access

Requires a [Device Access](https://device-access.nest.google.com) project
(one-time US$5 fee) and a Google Cloud OAuth client.

```bash
cp .env.example .env      # fill in client id/secret and project id
python get_token.py       # one-time OAuth flow → refresh token
python list_devices.py    # confirm the API can see your thermostat
```

### 2. Hub

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python hub.py
```

Dashboard at `http://localhost:5001/status`.

To run it supervised (restarts on crash, starts at boot):

```bash
launchctl bootstrap gui/$(id -u) deploy/com.neverbehind.nest-hub.plist
```

### 3. Sensor nodes

**ESP32:** copy `firmware/sensor_node/config.h.example` to `config.h`, set WiFi
and hub URL, flash. `config.h` is gitignored — it holds WiFi credentials and
must never be committed.

**Raspberry Pi:** clone this repo and add a cron entry:

```
*/5 * * * * /usr/bin/python3 /home/pi/nest/pi_sensor/sensor.py >> /home/pi/sensor.log 2>&1
```

### 4. Analysis

```bash
python fetch_weather.py --zip 84096   # backfill outdoor temps
python comfort_analysis.py            # current objective
python comfort_analysis.py --all      # include Phase 1, flagged as mixed
```

Writes plots and CSVs to `analysis/` (gitignored — regenerate rather than
commit).

---

## Configuration

All optional; defaults in `.env.example`. The ones that change behaviour most:

| Variable | Default | Meaning |
|---|---|---|
| `COMFORT_MAX_F` | `74.0` | Comfort ceiling for occupied rooms |
| `COMFORT_DEADBAND_F` | `1.5` | Start circulating this far below the cap |
| `MIN_GRADIENT_F` | `3.0` | Minimum basement gradient worth running the blower for |
| `EXPERIMENT_DUTY_ON_MINUTES` / `OFF` | `15` / `15` | Duty cycle |
| `FAN_QUIET_START` / `END` | *(empty)* | Quiet hours. Blank disables — see below |
| `EXPERIMENT_ENABLED` | `false` | Daily randomised A/B arms |

**Quiet hours are disabled by default**, which is deliberate and
counter-intuitive. The evening and overnight window is when the compressor
works hardest (77–91% duty, 18:00–24:00) *and* when the basement reservoir is
deepest (11–12°F). Suppressing the fan there was blocking the highest-value
cooling hours of the day.

---

## Experiment framework

The hub can run a randomised daily A/B test over control strategies. Each day
is assigned an arm by a **randomised complete block** schedule — every run of
N days contains each arm exactly once, in a seeded shuffle — so arms stay
balanced against weather without a fixed rotation aliasing with weekly
patterns. Assignment is deterministic, so a mid-day restart recomputes the same
arm.

Arms compose with the comfort gate rather than replacing it: **the arm decides
*how* to pulse, the gate decides whether pulsing achieves anything.**

Enable with `EXPERIMENT_ENABLED=true`; today's arm appears on the dashboard and
at `/api/experiment`.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /sensor` | Heartbeat intake |
| `GET /status` | Live dashboard |
| `GET /api/status` | Sensors, delta, fan state, comfort gate, thermostat |
| `GET /api/history` | Recent readings per sensor |
| `GET /api/devices` | Devices and their room mappings |
| `POST /api/devices/<id>/location` | Move a sensor to another room |
| `GET /api/fan_events` · `/api/hvac_events` · `/api/setpoint_events` | State-change logs |
| `GET /api/experiment` | Active arm and assignment history |

---

## Hardware

- ESP32-C3 SuperMini + DHT11, battery or USB powered, deep-sleeping between reads
- Raspberry Pi + DHT11 on GPIO4
- Nest thermostat (SDM API)

**On the DHT11:** it is a ±2°C part, which is poor. The 5-minute interval is
chosen accordingly — the house changes at roughly 0.1–0.3°F/hour, so sampling
faster mostly captures sensor noise, and the DHT11 self-heats slightly when
read back-to-back. Where accuracy matters most (the basement reservoir
reading), a calibration offset is applied and documented at the point it is
used.

---

## Licence

Personal project, no licence granted. Read it, learn from it, don't expect it
to work in your house without retuning.
