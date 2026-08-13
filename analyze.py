#!/usr/bin/env python3
"""
Correlate fan/HVAC behavior with outdoor weather.

Question we're chasing (Peter's hypothesis):
    The Nest holds the main floor close to setpoint (small swings, actively
    conditioned), while the basement free-floats more and absorbs whatever
    heat moves through the house as outdoor temperature rises through the
    day. So the floor-to-floor delta — and therefore fan runtime — should
    track outdoor temperature / solar gain more than it tracks any single
    indoor reading.

This script joins:
    sensor_readings   (our ESP32 + Nest indoor readings, resampled hourly)
    fan_events        (ON/OFF transitions -> per-hour runtime fraction)
    hvac_events       (HEATING/COOLING/OFF transitions)
    outdoor_weather   (hourly outdoor temp / solar radiation, via fetch_weather.py)

...into one hourly DataFrame, prints summary correlations, and saves plots
to ./analysis/.

Usage:
    python analyze.py              # last 7 days
    python analyze.py --days 14
"""
import argparse
import os
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

DB_PATH = os.getenv("DB_PATH", "nest.db")
OUT_DIR = "analysis"

NEST_LOCATION = "nest: hallway"
LOCAL_TZ = "America/Denver"


def _to_local(epoch_series, unit):
    """Epoch (UTC) -> naive local-time datetime, so hour-of-day / plots read correctly."""
    return (
        pd.to_datetime(epoch_series, unit=unit, utc=True)
        .dt.tz_convert(LOCAL_TZ)
        .dt.tz_localize(None)
    )


# ── Loaders ───────────────────────────────────────────────────────

def load_sensor_readings(conn, since_ts):
    df = pd.read_sql_query(
        "SELECT ts, location, temp_f, humidity FROM sensor_readings WHERE ts >= ?",
        conn, params=(since_ts,),
    )
    df["dt"] = _to_local(df["ts"], "s")
    return df


def load_events(conn, table, since_ts_ms):
    df = pd.read_sql_query(
        f"SELECT ts, state FROM {table} WHERE ts >= ? ORDER BY ts",
        conn, params=(since_ts_ms,),
    )
    df["dt"] = _to_local(df["ts"], "ms")
    return df


def load_weather(conn, since_ts):
    df = pd.read_sql_query(
        "SELECT ts, temp_f, humidity, cloud_cover, solar_radiation FROM outdoor_weather WHERE ts >= ?",
        conn, params=(since_ts,),
    )
    df["dt"] = _to_local(df["ts"], "s")
    return df


# ── Transforms ────────────────────────────────────────────────────

HUB_GAP_THRESHOLD = pd.Timedelta(hours=1.5)


def find_hub_outages(nest_dts: pd.Series) -> list:
    """
    The hub only logs `nest: hallway` readings while it's actively polling —
    so a gap in that series means the hub process was down (machine restart,
    crash, etc.), NOT that the fan/HVAC genuinely held state for hours.
    Returns a list of (start, end) outage windows longer than the threshold.
    """
    dts = nest_dts.sort_values().reset_index(drop=True)
    if len(dts) < 2:
        return []
    gaps = dts.diff()
    outages = []
    for i in range(1, len(dts)):
        if gaps.iloc[i] > HUB_GAP_THRESHOLD:
            outages.append((dts.iloc[i - 1], dts.iloc[i]))
    return outages


def hourly_state_fraction(events: pd.DataFrame, on_states, index: pd.DatetimeIndex,
                          outages: list = ()) -> pd.Series:
    """
    From a sparse ON/OFF (or HEATING/COOLING/OFF) transition log, compute
    the fraction of each hourly bucket in `index` that the system spent in
    one of `on_states`. Forward-fills state between transitions.

    Hours that overlap a known hub outage are set to NaN — during an outage
    nothing is logging state changes, so naively forward-filling would claim
    whatever state was last seen (usually "ON") persisted for the whole gap,
    which isn't physically true (e.g. the Nest's timed fan-ON command expires
    on its own after FAN_RUN_DURATION_SECONDS with no hub around to refresh it).
    """
    if events.empty:
        return pd.Series(np.nan, index=index)

    ev = events.sort_values("dt")[["dt", "state"]].drop_duplicates(subset="dt").copy()
    # Build a fine-grained timeline (1-min resolution) by forward-filling state
    start = min(ev["dt"].min(), index.min())
    end = max(ev["dt"].max(), index.max()) + pd.Timedelta(hours=1)
    timeline = pd.date_range(start, end, freq="1min")
    state = ev.set_index("dt")["state"].reindex(timeline, method="ffill").fillna("OFF")
    is_on = state.isin(on_states).astype(float)

    # Resample to hourly mean = fraction of that hour spent ON
    hourly = is_on.resample("1h").mean().reindex(index)

    for gap_start, gap_end in outages:
        overlap = (hourly.index + pd.Timedelta(hours=1) > gap_start) & (hourly.index < gap_end)
        hourly[overlap] = np.nan

    return hourly


def build_hourly_frame(sensors: pd.DataFrame, fan_events, hvac_events, weather: pd.DataFrame) -> pd.DataFrame:
    # Hourly mean indoor temps, pivoted: one column per location
    sensors_h = (
        sensors.set_index("dt")
        .groupby("location")["temp_f"]
        .resample("1h").mean()
        .unstack(level=0)
    )
    sensors_h.columns = [c for c in sensors_h.columns]

    basement_cols = [c for c in sensors_h.columns if c.startswith("basement")]
    sensors_h["basement_avg"] = sensors_h[basement_cols].mean(axis=1)
    sensors_h["nest"] = sensors_h.get(NEST_LOCATION)
    sensors_h["delta_f"] = sensors_h["nest"] - sensors_h["basement_avg"]

    # Outdoor weather, resampled to the same hourly grid
    weather_h = weather.set_index("dt")[["temp_f", "solar_radiation", "cloud_cover"]]
    weather_h = weather_h.resample("1h").mean()
    weather_h.columns = ["outdoor_temp_f", "solar_radiation", "cloud_cover"]

    df = sensors_h.join(weather_h, how="inner")

    # The hub can only log state changes while it's running — detect outages
    # (e.g. machine restarts) from gaps in Nest polling so we don't mistake
    # "nothing was logged" for "the fan ran continuously for N hours".
    outages = find_hub_outages(sensors.loc[sensors["location"] == NEST_LOCATION, "dt"])
    if outages:
        print("\n⚠ Detected hub outage window(s) — excluded from fan/HVAC duty-cycle stats:")
        for s, e in outages:
            print(f"   {s:%Y-%m-%d %H:%M} -> {e:%Y-%m-%d %H:%M}  ({(e - s).total_seconds()/3600:.1f} hrs)")

    # Fan / HVAC duty cycle per hour
    df["fan_on_frac"] = hourly_state_fraction(fan_events, {"ON"}, df.index, outages)
    df["cooling_frac"] = hourly_state_fraction(hvac_events, {"COOLING"}, df.index, outages)
    df["heating_frac"] = hourly_state_fraction(hvac_events, {"HEATING"}, df.index, outages)

    df["hour"] = df.index.hour
    return df


# ── Reporting ─────────────────────────────────────────────────────

def print_correlations(df: pd.DataFrame):
    pairs = [
        ("outdoor_temp_f", "nest",            "Outdoor temp  vs  Nest (main floor) temp"),
        ("outdoor_temp_f", "basement_avg",    "Outdoor temp  vs  Basement avg temp"),
        ("outdoor_temp_f", "delta_f",         "Outdoor temp  vs  Floor delta (nest - basement)"),
        ("outdoor_temp_f", "fan_on_frac",     "Outdoor temp  vs  Fan ON fraction"),
        ("solar_radiation", "delta_f",        "Solar radiation  vs  Floor delta"),
        ("solar_radiation", "nest",           "Solar radiation  vs  Nest temp"),
        ("delta_f", "fan_on_frac",            "Floor delta  vs  Fan ON fraction"),
        ("outdoor_temp_f", "cooling_frac",    "Outdoor temp  vs  AC (cooling) fraction"),
        ("hour", "fan_on_frac",               "Hour-of-day  vs  Fan ON fraction"),
        ("hour", "delta_f",                   "Hour-of-day  vs  Floor delta"),
    ]
    print("\n=== Correlations (Pearson r, hourly resolution) ===")
    for a, b, label in pairs:
        sub = df[[a, b]].dropna()
        if len(sub) < 3:
            continue
        r = sub[a].corr(sub[b])
        print(f"  {label:50s}  r = {r:+.2f}   (n={len(sub)})")

    print("\n=== Variability (how much each series swings, std-dev °F) ===")
    for col, label in [("nest", "Nest (main floor)"), ("basement_avg", "Basement avg"),
                       ("outdoor_temp_f", "Outdoor")]:
        s = df[col].dropna()
        print(f"  {label:25s} std = {s.std():5.2f} °F   range = {s.min():.1f}–{s.max():.1f} °F")

    print("\n=== Fan runtime by outdoor-temp bucket ===")
    bins = [-100, 50, 60, 70, 80, 90, 200]
    labels = ["<50°F", "50-60°F", "60-70°F", "70-80°F", "80-90°F", "90°F+"]
    df = df.copy()
    df["temp_bucket"] = pd.cut(df["outdoor_temp_f"], bins=bins, labels=labels)
    grp = df.groupby("temp_bucket", observed=True)["fan_on_frac"].agg(["mean", "count"])
    for bucket, row in grp.iterrows():
        bar = "█" * int(round(row["mean"] * 40))
        print(f"  {str(bucket):10s} avg fan-on = {row['mean']*100:5.1f}%  {bar}  (n={int(row['count'])} hrs)")


# ── Plots ─────────────────────────────────────────────────────────

def plot_timeseries(df: pd.DataFrame, path: str):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(df.index, df["outdoor_temp_f"], color="#888", lw=1.5, label="Outdoor")
    ax1.plot(df.index, df["nest"], color="#1a73e8", lw=1.5, label="Main floor (Nest)")
    ax1.plot(df.index, df["basement_avg"], color="#e8711a", lw=1.5, label="Basement (avg)")
    ax1.set_ylabel("Temperature (°F)")
    ax1.legend(loc="upper left")
    ax1.set_title("Indoor vs outdoor temperature, with fan runtime")
    ax1.grid(alpha=0.3)

    ax2.fill_between(df.index, 0, df["fan_on_frac"] * 100, color="#1a73e8", alpha=0.5, step="mid", label="Fan ON %")
    ax2.fill_between(df.index, 0, df["cooling_frac"] * 100, color="#ff9800", alpha=0.4, step="mid", label="AC ON %")
    ax2.set_ylabel("% of hour ON")
    ax2.set_ylim(0, 105)
    ax2.legend(loc="upper left")
    ax2.grid(alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%a %m/%d"))

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_scatter_outdoor_vs_delta(df: pd.DataFrame, path: str):
    fig, ax = plt.subplots(figsize=(8, 6))
    sub = df.dropna(subset=["outdoor_temp_f", "delta_f"])
    sc = ax.scatter(sub["outdoor_temp_f"], sub["delta_f"], c=sub["hour"], cmap="twilight", s=22, alpha=0.85)
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("Hour of day")
    ax.set_xlabel("Outdoor temperature (°F)")
    ax.set_ylabel("Floor delta — Nest minus Basement (°F)")
    ax.set_title("Does the floor-to-floor delta widen as it gets hotter outside?")
    ax.grid(alpha=0.3)

    if len(sub) > 2:
        m, b = np.polyfit(sub["outdoor_temp_f"], sub["delta_f"], 1)
        xs = np.linspace(sub["outdoor_temp_f"].min(), sub["outdoor_temp_f"].max(), 50)
        ax.plot(xs, m * xs + b, color="black", ls="--", lw=1, label=f"trend: Δ = {m:.2f}×T + {b:.1f}")
        ax.legend()

    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_fan_vs_outdoor_temp(df: pd.DataFrame, path: str):
    fig, ax = plt.subplots(figsize=(8, 6))
    sub = df.dropna(subset=["outdoor_temp_f", "fan_on_frac"])
    ax.scatter(sub["outdoor_temp_f"], sub["fan_on_frac"] * 100, s=22, alpha=0.6, color="#1a73e8")
    ax.set_xlabel("Outdoor temperature (°F)")
    ax.set_ylabel("Fan ON (% of hour)")
    ax.set_title("Fan runtime vs outdoor temperature")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_daily_summary(df: pd.DataFrame, path: str):
    daily = df.resample("1D").agg(
        outdoor_mean=("outdoor_temp_f", "mean"),
        outdoor_max=("outdoor_temp_f", "max"),
        delta_mean=("delta_f", "mean"),
        fan_hours=("fan_on_frac", "sum"),
    )
    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax1.bar(daily.index, daily["fan_hours"], width=0.6, color="#1a73e8", alpha=0.6, label="Fan runtime (hrs/day)")
    ax1.set_ylabel("Fan runtime (hours/day)", color="#1a73e8")
    ax1.tick_params(axis="y", labelcolor="#1a73e8")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%a %m/%d"))

    ax2 = ax1.twinx()
    ax2.plot(daily.index, daily["outdoor_max"], color="#e8711a", marker="o", label="Daily high (°F)")
    ax2.plot(daily.index, daily["delta_mean"], color="#34a853", marker="s", label="Avg floor delta (°F)")
    ax2.set_ylabel("°F")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    ax1.set_title("Daily fan runtime vs outdoor high & floor delta")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# ── Main ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    since_ts = pd.Timestamp.now().timestamp() - args.days * 86400
    since_ts_ms = since_ts * 1000

    sensors = load_sensor_readings(conn, since_ts)
    fan_events = load_events(conn, "fan_events", since_ts_ms)
    hvac_events = load_events(conn, "hvac_events", since_ts_ms)
    weather = load_weather(conn, since_ts)
    conn.close()

    if weather.empty:
        print("No outdoor_weather data found — run `python fetch_weather.py` first.")
        return
    if sensors.empty:
        print("No sensor_readings found in the requested window.")
        return

    df = build_hourly_frame(sensors, fan_events, hvac_events, weather)
    print(f"Built hourly frame: {len(df)} hours, {df.index.min()} -> {df.index.max()}")
    print(f"Locations found: {sorted(sensors['location'].unique())}")

    print_correlations(df)

    plot_timeseries(df, os.path.join(OUT_DIR, "timeseries.png"))
    plot_scatter_outdoor_vs_delta(df, os.path.join(OUT_DIR, "outdoor_vs_delta.png"))
    plot_fan_vs_outdoor_temp(df, os.path.join(OUT_DIR, "fan_vs_outdoor_temp.png"))
    plot_daily_summary(df, os.path.join(OUT_DIR, "daily_summary.png"))

    csv_path = os.path.join(OUT_DIR, "hourly_joined.csv")
    df.to_csv(csv_path)

    print(f"\nSaved plots + data to ./{OUT_DIR}/:")
    for f in ["timeseries.png", "outdoor_vs_delta.png", "fan_vs_outdoor_temp.png", "daily_summary.png", "hourly_joined.csv"]:
        print(f"  - {OUT_DIR}/{f}")


if __name__ == "__main__":
    main()
