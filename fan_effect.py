#!/usr/bin/env python3
"""
Does running the fan actually do anything?

Peter's question: the floor delta basically never closes (it's always 3.5-12F,
see analyze.py), and the fan runs ~100% of the day regardless. So... is it
even helping? Or is the basement just going to sit at ~66F and the main floor
at ~74F no matter what we do?

Naively comparing "delta while fan is ON" vs "delta while fan is OFF" is
confounded — fan-OFF is almost entirely quiet hours (10pm-6:30am), a
completely different thermal regime (no sun, AC behaves differently, etc).
So instead this script leans on two cleaner natural experiments:

  1. EVENT STUDY around the twice-daily quiet-hours transitions. The fan
     flips ON at ~6:30am and OFF at ~10pm on a *clock schedule*, independent
     of delta. Time-of-day drifts continuously through that instant, but fan
     state jumps. If the fan does anything, the delta/basement trajectory
     should visibly bend at that instant — averaged over ~8 days, random
     day-to-day noise should cancel out and leave the fan's signature (if any).

  2. THE OUTAGE NIGHT (6/3 9:19pm - 6/4 8:09am): the hub was down, so the fan
     almost certainly sat OFF ~2 hours longer than a normal quiet-hours night
     (~10.5h of no-fan vs the usual ~8.5h). If "more hours without the fan"
     measurably moves the basement or the delta, that's a real-world answer
     to "is it worth it" — accidentally run for us.

Usage:
    python fan_effect.py
"""
import os
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analyze import _to_local, find_hub_outages, NEST_LOCATION, LOCAL_TZ

DB_PATH = os.getenv("DB_PATH", "nest.db")
OUT_DIR = "analysis"
RESAMPLE = "5min"


def load_frame():
    conn = sqlite3.connect(DB_PATH)
    sensors = pd.read_sql_query("SELECT ts, location, temp_f FROM sensor_readings", conn)
    fan = pd.read_sql_query("SELECT ts, state FROM fan_events ORDER BY ts", conn)
    conn.close()

    sensors["dt"] = _to_local(sensors["ts"], "s")
    fan["dt"] = _to_local(fan["ts"], "ms")

    wide = (sensors.set_index("dt").groupby("location")["temp_f"]
            .resample(RESAMPLE).mean().unstack(level=0))
    basement_cols = [c for c in wide.columns if c.startswith("basement")]
    wide["basement_avg"] = wide[basement_cols].mean(axis=1)
    wide["nest"] = wide.get(NEST_LOCATION)
    wide["delta_f"] = wide["nest"] - wide["basement_avg"]
    wide = wide.dropna(subset=["nest", "basement_avg"])

    # Fan state forward-filled onto the same 5-min grid; NaN inside known outages
    outages = find_hub_outages(sensors.loc[sensors["location"] == NEST_LOCATION, "dt"])
    fan_state = (fan.set_index("dt")["state"].reindex(wide.index, method="ffill"))
    for s, e in outages:
        fan_state[(wide.index >= s) & (wide.index <= e)] = np.nan
    wide["fan_on"] = fan_state.map({"ON": True, "OFF": False})

    return wide, outages


# ── 1. Naive ON-vs-OFF rate-of-change comparison ─────────────────

def naive_comparison(df: pd.DataFrame):
    sub = df.dropna(subset=["fan_on"]).copy()
    sub["fan_on"] = sub["fan_on"].astype(bool)
    # Rate of change over a trailing 30-min window, in °F/hr
    step_hr = pd.Timedelta(RESAMPLE).total_seconds() / 3600
    sub["d_delta_per_hr"]    = sub["delta_f"].diff(6) / (6 * step_hr)        # 6 * 5min = 30min
    sub["d_basement_per_hr"] = sub["basement_avg"].diff(6) / (6 * step_hr)
    sub = sub.dropna(subset=["d_delta_per_hr"])

    print("\n=== Naive comparison: rate of change, fan ON vs fan OFF (confounded by time-of-day!) ===")
    for col, label in [("d_delta_per_hr", "Δ(floor delta)"), ("d_basement_per_hr", "Δ(basement temp)")]:
        on  = sub.loc[sub.fan_on,  col]
        off = sub.loc[~sub.fan_on, col]
        print(f"  {label:20s}  fan ON : mean={on.mean():+.3f} °F/hr  (n={len(on)})")
        print(f"  {'':20s}  fan OFF: mean={off.mean():+.3f} °F/hr  (n={len(off)})")
    print("  (fan-OFF is ~entirely quiet hours — nighttime physics differ from daytime")
    print("   regardless of the fan, so don't read too much into this side-by-side.)")


# ── 2. Event study around the quiet-hours ON/OFF clock transitions ─

def daily_transition_times(fan: pd.DataFrame):
    """For each day, find the actual ON transition near 06:30 and OFF near 22:00."""
    ev = fan.sort_values("dt")
    ons  = ev[ev.state == "ON"]
    offs = ev[ev.state == "OFF"]
    on_times, off_times = [], []
    for d in pd.date_range(ev["dt"].min().normalize(), ev["dt"].max().normalize(), freq="1D"):
        win_on = ons[(ons.dt >= d + pd.Timedelta("5:00:00")) & (ons.dt <= d + pd.Timedelta("8:00:00"))]
        win_off = offs[(offs.dt >= d + pd.Timedelta("21:00:00")) & (offs.dt <= d + pd.Timedelta("23:30:00"))]
        if len(win_on):
            on_times.append(win_on.dt.iloc[0])
        if len(win_off):
            off_times.append(win_off.dt.iloc[0])
    return on_times, off_times


def event_study(df: pd.DataFrame, transitions: list, label: str, window_min=120):
    """Average the delta & basement trajectories in a window around each transition,
    aligned on t=0 = the transition instant. Returns the aligned DataFrame."""
    step = pd.Timedelta(RESAMPLE)
    offsets = pd.timedelta_range(-pd.Timedelta(minutes=window_min), pd.Timedelta(minutes=window_min), freq=RESAMPLE)
    rows = []
    for i, t0 in enumerate(transitions):
        for off in offsets:
            ts = t0 + off
            idx = df.index.asof(ts)
            if pd.isna(idx) or abs((idx - ts)) > step * 2:
                continue
            rows.append({
                "event": i, "offset_min": off.total_seconds() / 60,
                "delta_f": df.at[idx, "delta_f"], "basement_avg": df.at[idx, "basement_avg"],
                "nest": df.at[idx, "nest"],
            })
    aligned = pd.DataFrame(rows)
    if aligned.empty:
        print(f"  (no usable events for {label})")
        return aligned

    g = aligned.groupby("offset_min")[["delta_f", "basement_avg", "nest"]].mean()

    # Slope before vs after t=0 (simple linear fit on the averaged trajectory)
    before = g[g.index < 0]
    after  = g[g.index > 0]
    print(f"\n--- Event study: {label} (n={aligned['event'].nunique()} mornings/evenings, "
          f"±{window_min} min, averaged) ---")
    for col, name in [("basement_avg", "Basement avg temp"), ("delta_f", "Floor delta")]:
        if len(before) > 2 and len(after) > 2:
            mb, _ = np.polyfit(before.index, before[col], 1)   # °F per minute, before
            ma, _ = np.polyfit(after.index, after[col], 1)     # °F per minute, after
            print(f"  {name:20s}  slope before: {mb*60:+.3f} °F/hr   slope after: {ma*60:+.3f} °F/hr"
                  f"   (change: {(ma-mb)*60:+.3f} °F/hr)")
    return aligned


def plot_event_study(aligned_on: pd.DataFrame, aligned_off: pd.DataFrame, path: str):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, aligned, title in [(axes[0], aligned_on, "Fan turns ON  (~6:30 AM, end of quiet hours)"),
                                (axes[1], aligned_off, "Fan turns OFF (~10:00 PM, start of quiet hours)")]:
        if aligned.empty:
            continue
        g = aligned.groupby("offset_min")[["delta_f", "basement_avg"]].agg(["mean", "sem"])
        x = g.index

        ax2 = ax.twinx()
        m, sem = g[("delta_f", "mean")], g[("delta_f", "sem")]
        ax.plot(x, m, color="#1a73e8", lw=2, label="Floor delta (°F)  [left axis]")
        ax.fill_between(x, m - sem, m + sem, color="#1a73e8", alpha=0.2)
        ax.set_ylabel("Floor delta (°F)", color="#1a73e8")
        ax.tick_params(axis="y", labelcolor="#1a73e8")

        m2, sem2 = g[("basement_avg", "mean")], g[("basement_avg", "sem")]
        ax2.plot(x, m2, color="#e8711a", lw=2, label="Basement avg (°F)  [right axis]")
        ax2.fill_between(x, m2 - sem2, m2 + sem2, color="#e8711a", alpha=0.2)
        ax2.set_ylabel("Basement avg temp (°F)", color="#e8711a")
        ax2.tick_params(axis="y", labelcolor="#e8711a")

        ax.axvline(0, color="black", ls="--", lw=1)
        ax.set_title(title)
        ax.set_xlabel("Minutes relative to fan state change  (shaded = ±SEM across days)")
        ax.grid(alpha=0.3)
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="best", fontsize=8)
    fig.suptitle("Event study: averaged trajectory around the daily fan ON/OFF clock transitions\n"
                 "(dashed line = the instant the fan flips state, on a fixed clock schedule)")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# ── 3. Outage night vs a typical night ───────────────────────────

def outage_vs_typical_night(df: pd.DataFrame, outages: list, on_times, off_times):
    if not outages:
        print("\n(no outage detected — skipping outage-night comparison)")
        return

    out_start, out_end = outages[0]
    # The outage swallowed the ~10pm OFF transition; the fan was last alive ~9:19pm
    # and the hub came back ~8:09am — both well past the usual transition times.
    print(f"\n--- Natural experiment: the outage night ({out_start:%a %m/%d %H:%M} -> "
          f"{out_end:%a %m/%d %H:%M}) ---")
    print("  The hub was down, so nothing commanded the fan — it almost certainly sat OFF")
    print("  the entire night (its timed ON command expires ~20 min after the last refresh).")
    print(f"  That's roughly {(out_end - out_start).total_seconds()/3600:.1f}h without the fan,")
    print("  vs. the usual ~8.5h quiet-hours window (10:00 PM -> 6:30 AM).")

    def night_stats(start, end, label):
        sub = df[(df.index >= start) & (df.index <= end)]
        if sub.empty:
            return
        d0, d1 = sub["delta_f"].iloc[0], sub["delta_f"].iloc[-1]
        b0, b1 = sub["basement_avg"].iloc[0], sub["basement_avg"].iloc[-1]
        hrs = (end - start).total_seconds() / 3600
        print(f"  {label:28s}  {hrs:4.1f}h no-fan   "
              f"delta {d0:.1f}->{d1:.1f}°F ({(d1-d0)/hrs:+.2f}/hr)   "
              f"basement {b0:.1f}->{b1:.1f}°F ({(b1-b0)/hrs:+.2f}/hr)")

    night_stats(out_start, out_end, "Outage night (6/3->6/4)")

    # Compare against each "typical" quiet-hours window from the transition log
    off_to_on = sorted(zip(off_times, [t for t in on_times if t > min(off_times, default=t)]))
    paired = []
    offs_sorted = sorted(off_times)
    ons_sorted = sorted(on_times)
    for off in offs_sorted:
        nxt = [o for o in ons_sorted if o > off]
        if nxt:
            paired.append((off, nxt[0]))
    for off, on in paired:
        if (on - off) < pd.Timedelta(hours=20):  # sanity: same-night pairing
            night_stats(off, on, f"Typical night {off:%a %m/%d}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df, outages = load_frame()

    conn = sqlite3.connect(DB_PATH)
    fan = pd.read_sql_query("SELECT ts, state FROM fan_events ORDER BY ts", conn)
    conn.close()
    fan["dt"] = _to_local(fan["ts"], "ms")

    naive_comparison(df)

    on_times, off_times = daily_transition_times(fan)
    print(f"\nFound {len(on_times)} morning ON transitions, {len(off_times)} evening OFF transitions")

    aligned_on  = event_study(df, on_times,  "fan turns ON at end of quiet hours")
    aligned_off = event_study(df, off_times, "fan turns OFF at start of quiet hours")
    plot_event_study(aligned_on, aligned_off, os.path.join(OUT_DIR, "fan_event_study.png"))

    outage_vs_typical_night(df, outages, on_times, off_times)

    print(f"\nSaved: {OUT_DIR}/fan_event_study.png")


if __name__ == "__main__":
    main()
