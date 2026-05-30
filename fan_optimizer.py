#!/usr/bin/env python3
"""
Nest Fan Optimizer
Polls Nest temperature sensors every POLL_INTERVAL_SECONDS.
Turns the HVAC fan ON when the floor-to-floor delta exceeds TEMP_DELTA_THRESHOLD_F,
and back to AUTO when floors are balanced.

First run: python get_token.py  (one-time OAuth flow)
Then:       python fan_optimizer.py
"""

import os
import time
import logging
from dotenv import load_dotenv
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

SDM_PROJECT_ID          = os.environ["SDM_PROJECT_ID"]           # Device Access project ID
GOOGLE_CLIENT_ID        = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET    = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN    = os.environ["GOOGLE_REFRESH_TOKEN"]

TEMP_DELTA_THRESHOLD_F  = float(os.getenv("TEMP_DELTA_THRESHOLD_F",  "3.0"))   # °F
POLL_INTERVAL_SECONDS   = int(os.getenv("POLL_INTERVAL_SECONDS",     "900"))    # 15 min
FAN_RUN_DURATION_SECONDS = int(os.getenv("FAN_RUN_DURATION_SECONDS", "1200"))   # 20 min

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------
TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_BASE  = "https://smartdevicemanagement.googleapis.com/v1"


def get_access_token() -> str:
    resp = requests.post(TOKEN_URL, data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def sdm_get(path: str, token: str) -> dict:
    resp = requests.get(f"{SDM_BASE}{path}", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json()


def sdm_command(device_name: str, command: str, params: dict, token: str) -> dict:
    """Execute a command on a device.
    device_name is the full resource name e.g. enterprises/xxx/devices/yyy
    """
    url = f"{SDM_BASE}/{device_name}:executeCommand"
    resp = requests.post(url, json={"command": command, "params": params},
                         headers=_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def list_devices(token: str) -> list[dict]:
    data = sdm_get(f"/enterprises/{SDM_PROJECT_ID}/devices", token)
    return data.get("devices", [])


def celsius_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def display_name(device: dict) -> str:
    info = device.get("traits", {}).get("sdm.devices.traits.Info", {})
    custom = info.get("customName", "")
    return custom if custom else device["name"].split("/")[-1]


def temperature_f(device: dict) -> float | None:
    trait = device.get("traits", {}).get("sdm.devices.traits.Temperature", {})
    c = trait.get("ambientTemperatureCelsius")
    return celsius_to_f(c) if c is not None else None


def has_fan(device: dict) -> bool:
    return "sdm.devices.traits.Fan" in device.get("traits", {})


def fan_mode(device: dict) -> str:
    return device.get("traits", {}).get("sdm.devices.traits.Fan", {}).get("timerMode", "OFF")


def set_fan(device_name: str, mode: str, duration_s: int | None, token: str):
    params: dict = {"timerMode": mode}
    if mode == "ON" and duration_s:
        params["duration"] = f"{duration_s}s"
    sdm_command(device_name, "sdm.devices.commands.Fan.SetTimer", params, token)
    log.info("Fan → %s%s", mode,
             f" for {duration_s // 60} min" if mode == "ON" and duration_s else "")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def classify_devices(devices: list[dict]) -> tuple[dict | None, list[dict]]:
    """Return (thermostat_with_fan, [temperature_sensors])."""
    thermostat = None
    sensors = []
    for d in devices:
        if has_fan(d):
            thermostat = d
        elif temperature_f(d) is not None:
            sensors.append(d)
    return thermostat, sensors


def run_cycle(token: str):
    devices = list_devices(token)
    thermostat, sensors = classify_devices(devices)

    if thermostat is None:
        log.error("No thermostat with fan control found. Check SDM project permissions.")
        return

    thermo_name  = display_name(thermostat)
    thermo_temp  = temperature_f(thermostat)
    current_mode = fan_mode(thermostat)

    log.info("Thermostat: %-30s  %5.1f°F  fan=%s",
             thermo_name, thermo_temp or 0, current_mode)
    for s in sensors:
        log.info("  Sensor:   %-30s  %5.1f°F", display_name(s), temperature_f(s) or 0)

    # ------------------------------------------------------------------
    # Determine the temperature delta
    # ------------------------------------------------------------------
    temps: list[float] = [t for t in [temperature_f(d) for d in sensors] if t is not None]
    if thermo_temp is not None:
        temps.append(thermo_temp)

    if len(temps) < 2:
        log.warning("Need at least 2 temperature readings; only %d found. "
                    "Check sensor access in Device Access Console.", len(temps))
        return

    delta = max(temps) - min(temps)
    log.info("Δ temp = %.1f°F  (threshold %.1f°F)", delta, TEMP_DELTA_THRESHOLD_F)

    # ------------------------------------------------------------------
    # Act
    # ------------------------------------------------------------------
    device_name = thermostat["name"]

    if delta > TEMP_DELTA_THRESHOLD_F:
        # Floors unbalanced — keep fan running (refresh the timer every cycle)
        log.info("Above threshold → fan ON (%d min)", FAN_RUN_DURATION_SECONDS // 60)
        set_fan(device_name, "ON", FAN_RUN_DURATION_SECONDS, token)
    else:
        if current_mode == "ON":
            log.info("Within threshold → fan OFF")
            set_fan(device_name, "OFF", None, token)
        else:
            log.info("Within threshold → fan already OFF. Nothing to do.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    log.info("═" * 60)
    log.info("Nest Fan Optimizer started")
    log.info("  Threshold : %.1f°F", TEMP_DELTA_THRESHOLD_F)
    log.info("  Poll      : %d s (%d min)", POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS // 60)
    log.info("  Fan timer : %d s (%d min)", FAN_RUN_DURATION_SECONDS, FAN_RUN_DURATION_SECONDS // 60)
    log.info("═" * 60)

    while True:
        try:
            token = get_access_token()
            run_cycle(token)
        except requests.HTTPError as exc:
            log.error("HTTP error: %s  —  %s", exc.response.status_code, exc.response.text[:200])
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)

        log.info("Sleeping %d min…\n", POLL_INTERVAL_SECONDS // 60)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
