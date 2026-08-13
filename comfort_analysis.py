#!/usr/bin/env python3
"""
Comfort-objective analysis — "can fan circulation displace the compressor?"

Supersedes experiment_analysis.py, which answered a question we no longer ask.
That script optimised the *floor-to-floor delta* under a 4-basement/1-main-floor
sensor layout. As of 2026-08-12 three of those sensors moved upstairs and the
goal changed: hold the occupied upstairs rooms below a comfort cap using
basement air, so the AC runs less.

So the metrics invert. Delta is no longer the outcome — it's a *resource*
(how much cold air is available to move). The outcome is compressor runtime,
and comfort becomes a constraint rather than the thing being optimised:

  primary    ac_hours          compressor hours/day        ↓ lower is better
  guardrail  over_cap_hours    occupied room > cap          must stay ~0
  guardrail  worst_excursion   hottest occupied reading     must stay ~cap
  guardrail  setpoint_drops    manual thermostat overrides  must stay ~0
  cost       fan_hours         blower hours/day
  covariate  outdoor_high      weather
  covariate  setpoint_mean     what the thermostat targeted

Two measurement traps this script exists to avoid:

  1. THE SETPOINT CONFOUND. The target moves on a schedule *and* by hand.
     A day with more compressor time may simply have had a colder target, so
     ac_hours is meaningless unless setpoint travels alongside it.

  2. THE HALLWAY BLIND SPOT. The Nest decides when to cool from the hallway,
     which runs up to 2.5°F cooler than the living room in the late afternoon.
     A fan that washes basement air past the thermostat could suppress the
     compressor while the living room stays hot — a "win" on the primary
     metric that is actually a comfort regression. Only the independent
     living-room sensor separates those, which is why the guardrails are not
     optional and `hall_bias` is reported explicitly.

Manual setpoint drops are treated as revealed comfort failures: someone was
uncomfortable enough to reach for the thermostat. That's a better guardrail
than any threshold we'd invent, because it's the occupants voting.

Usage:
    python comfort_analysis.py                 # since the 2026-08-12 regime change
    python comfort_analysis.py --days 30       # explicit window (may span regimes)
    python comfort_analysis.py --all           # everything, regimes flagged
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

from analyze import _to_local, find_hub_outages, LOCAL_TZ

DB_PATH  = os.getenv("DB_PATH", "nest.db")
OUT_DIR  = "analysis"
RESAMPLE = "5min"

# Sensors were relocated and setpoint logging began on this date. Earlier data
# describes a different house layout with an unrecorded target, so it cannot be
# compared against — see the module docstring.
REGIME_START = pd.Timestamp("2026-08-12")

COMFORT_CAP_F   = float(os.getenv("COMFORT_MAX_F", "74.0"))
MIN_GRADIENT_F  = float(os.getenv("MIN_GRADIENT_F", "3.0"))
OCCUPIED_PREFIX = os.getenv("OCCUPIED_PREFIX",  "upstairs")
RESERVOIR_PREFIX = os.getenv("RESERVOIR_PREFIX", "basement")
THERMOSTAT_PREFIX = "nest"
LIVING_ROOM     = "upstairs: living room"   # the comfort reference room

C_AC   = "#e34948"
C_FAN  = "#2a78d6"
C_GRAD = "#1baf7a"
C_WARN = "#eb6834"


# ── Loading ──────────────────────────────────────────────────────

def load_frame(conn, since_ts):
    """5-min grid: per-zone temps, fan/AC state, setpoint, outdoor."""
    raw = pd.read_sql_query(
        "SELECT ts, location, temp_f FROM sensor_readings WHERE ts >= ?",
        conn, params=(since_ts,))
    if raw.empty:
        return pd.DataFrame(), []
    raw["dt"] = _to_local(raw["ts"], "s")

    wide = (raw.set_index("dt").groupby("location")["temp_f"]
               .resample(RESAMPLE).mean().unstack(level=0))

    occ_cols = [c for c in wide.columns if c.startswith(OCCUPIED_PREFIX)]
    res_cols = [c for c in wide.columns if c.startswith(RESERVOIR_PREFIX)]
    th_cols  = [c for c in wide.columns if c.startswith(THERMOSTAT_PREFIX)]

    # The hottest occupied room is what comfort is judged on — an average would
    # hide the one room that's actually uncomfortable.
    wide["occupied"]  = wide[occ_cols].max(axis=1) if occ_cols else np.nan
    wide["reservoir"] = wide[res_cols].min(axis=1) if res_cols else np.nan
    wide["hallway"]   = wide[th_cols].mean(axis=1) if th_cols else np.nan
    wide["living_room"] = wide[LIVING_ROOM] if LIVING_ROOM in wide.columns else np.nan
    wide["gradient"]  = wide["occupied"] - wide["reservoir"]
    # Positive = the room people sit in is hotter than what the Nest measures.
    wide["hall_bias"] = wide["living_room"] - wide["hallway"]

    # Hub downtime — don't credit or blame any state during a gap.
    nest_dts = raw.loc[raw["location"].str.startswith(THERMOSTAT_PREFIX), "dt"]
    outages = find_hub_outages(nest_dts) if len(nest_dts) else []

    wide["fan_on"] = _state_series(conn, "fan_events", {"ON"}, wide.index, outages)
    wide["ac_on"]  = _state_series(conn, "hvac_events", {"COOLING"}, wide.index, outages)
    wide["setpoint_f"] = _setpoint_series(conn, wide.index)
    wide["outdoor_f"]  = _outdoor_series(conn, wide.index)

    wide["zones"] = None
    wide.attrs["occ_cols"] = occ_cols
    wide.attrs["res_cols"] = res_cols
    return wide, outages


def _state_series(conn, table, on_states, index, outages):
    ev = pd.read_sql_query(f"SELECT ts, state FROM {table} ORDER BY ts", conn)
    if ev.empty:
        return pd.Series(np.nan, index=index)
    ev["dt"] = _to_local(ev["ts"], "ms")
    s = ev.set_index("dt")["state"].reindex(index, method="ffill")
    out = s.isin(on_states).astype(float)
    out[s.isna()] = np.nan
    for a, b in outages:
        out[(index >= a) & (index <= b)] = np.nan
    return out


def _setpoint_series(conn, index):
    """thermostat_state is written on change, so carry each target forward."""
    try:
        sp = pd.read_sql_query(
            "SELECT ts, cool_f FROM thermostat_state ORDER BY ts", conn)
    except Exception:
        return pd.Series(np.nan, index=index)
    if sp.empty:
        return pd.Series(np.nan, index=index)
    sp["dt"] = _to_local(sp["ts"], "s")
    return (sp.dropna(subset=["cool_f"]).set_index("dt")["cool_f"]
              .reindex(index, method="ffill"))


def _outdoor_series(conn, index):
    w = pd.read_sql_query("SELECT ts, temp_f FROM outdoor_weather ORDER BY ts", conn)
    if w.empty:
        return pd.Series(np.nan, index=index)
    w["dt"] = _to_local(w["ts"], "s")
    return (w.set_index("dt")["temp_f"].resample(RESAMPLE).mean()
             .reindex(index).interpolate(limit=24))


def load_setpoint_drops(conn):
    """Manual downward adjustments = revealed comfort failures."""
    try:
        sp = pd.read_sql_query(
            "SELECT ts, cool_f FROM thermostat_state ORDER BY ts", conn)
    except Exception:
        return pd.DataFrame(columns=["dt", "prev_f", "cool_f", "drop_f"])
    sp = sp.dropna(subset=["cool_f"])
    if len(sp) < 2:
        return pd.DataFrame(columns=["dt", "prev_f", "cool_f", "drop_f"])
    sp["dt"] = _to_local(sp["ts"], "s")
    sp["prev_f"] = sp["cool_f"].shift()
    sp["drop_f"] = sp["prev_f"] - sp["cool_f"]
    return sp[sp["drop_f"] > 0.4][["dt", "prev_f", "cool_f", "drop_f"]]


# ── Daily metrics ────────────────────────────────────────────────

def daily_summary(df: pd.DataFrame, drops: pd.DataFrame) -> pd.DataFrame:
    step_h = pd.Timedelta(RESAMPLE).total_seconds() / 3600
    g = df.groupby(df.index.date)

    out = pd.DataFrame({
        "ac_hours":        g["ac_on"].sum() * step_h,
        "fan_hours":       g["fan_on"].sum() * step_h,
        "coverage_h":      g["occupied"].count() * step_h,
        "occupied_mean":   g["occupied"].mean(),
        "worst_excursion": g["occupied"].max(),
        "over_cap_hours":  g["occupied"].apply(lambda s: (s > COMFORT_CAP_F).sum()) * step_h,
        "reservoir_mean":  g["reservoir"].mean(),
        "gradient_mean":   g["gradient"].mean(),
        "usable_hours":    g["gradient"].apply(lambda s: (s >= MIN_GRADIENT_F).sum()) * step_h,
        "hall_bias_max":   g["hall_bias"].max(),
        "setpoint_mean":   g["setpoint_f"].mean(),
        "setpoint_min":    g["setpoint_f"].min(),
        "outdoor_high":    g["outdoor_f"].max(),
    })
    out.index = pd.to_datetime(out.index)

    n_drops = drops.groupby(drops["dt"].dt.date).size() if len(drops) else pd.Series(dtype=int)
    n_drops.index = pd.to_datetime(n_drops.index)
    out["setpoint_drops"] = n_drops.reindex(out.index).fillna(0).astype(int)

    out["regime"] = np.where(out.index >= REGIME_START, "comfort", "legacy-delta")
    return out


# ── Reporting ────────────────────────────────────────────────────

def print_report(daily: pd.DataFrame, df: pd.DataFrame, drops: pd.DataFrame):
    print("\n" + "=" * 78)
    print("COMFORT-OBJECTIVE REPORT — displace the compressor, hold the rooms")
    print("=" * 78)

    legacy = daily[daily["regime"] == "legacy-delta"]
    if len(legacy):
        print(f"\n  {len(legacy)} day(s) predate the {REGIME_START:%Y-%m-%d} regime change "
              f"(different sensor layout, setpoint unlogged).")
        print("  Shown for context only — not comparable. Use --days/--all deliberately.")

    cur = daily[daily["regime"] == "comfort"]
    if cur.empty:
        print("\n  No data yet under the comfort objective. Let it run.")
        return

    partial = cur[cur["coverage_h"] < 20]
    full    = cur[cur["coverage_h"] >= 20]

    print(f"\n  Days under the comfort objective: {len(cur)}"
          f"  ({len(full)} full, {len(partial)} partial)")
    if len(partial):
        print("  Partial: " + ", ".join(f"{d:%m/%d} ({h:.1f}h)"
              for d, h in partial["coverage_h"].items()))

    print(f"\n  {'date':<8}{'AC h':>7}{'fan h':>7}{'set °F':>8}{'out °F':>8}"
          f"{'worst':>7}{'>cap h':>8}{'drops':>7}{'usable h':>9}")
    for d, r in cur.iterrows():
        flag = " *" if r["coverage_h"] < 20 else "  "
        print(f"  {d:%m/%d}{flag}{r['ac_hours']:>6.1f}{r['fan_hours']:>7.1f}"
              f"{r['setpoint_mean']:>8.1f}{r['outdoor_high']:>8.1f}"
              f"{r['worst_excursion']:>7.1f}{r['over_cap_hours']:>8.1f}"
              f"{int(r['setpoint_drops']):>7}{r['usable_hours']:>9.1f}")

    if len(full):
        print(f"\n  Averages over {len(full)} full day(s):")
        print(f"    AC compressor      {full['ac_hours'].mean():5.2f} h/day   ← primary metric")
        print(f"    Fan blower         {full['fan_hours'].mean():5.2f} h/day")
        print(f"    Occupied mean      {full['occupied_mean'].mean():5.1f} °F")
        print(f"    Worst excursion    {full['worst_excursion'].max():5.1f} °F "
              f"(cap {COMFORT_CAP_F:.1f})")
        print(f"    Hours over cap     {full['over_cap_hours'].mean():5.2f} h/day")
        print(f"    Setpoint drops     {full['setpoint_drops'].mean():5.2f} /day")

    # The reservoir is the whole premise — say plainly whether it's there.
    print(f"\n  Reservoir availability:")
    print(f"    basement holds     {cur['reservoir_mean'].mean():5.1f} °F")
    print(f"    mean gradient      {cur['gradient_mean'].mean():5.1f} °F")
    print(f"    hours with ≥{MIN_GRADIENT_F:.0f}°F   {cur['usable_hours'].mean():5.1f} h/day")

    # Trap 2 made explicit.
    bias = df["hall_bias"].dropna()
    if len(bias):
        print(f"\n  Hallway blind spot (living room − thermostat):")
        print(f"    mean {bias.mean():+.2f} °F   max {bias.max():+.2f} °F"
              f"   hours >1°F: {(bias > 1).sum() * 5 / 60:.1f}")
        print("    The Nest cools on the lower number, so the living room can sit")
        print("    above the cap while the thermostat reports it's satisfied.")

    if len(drops):
        print(f"\n  Manual setpoint drops (revealed comfort failures):")
        for _, r in drops.iterrows():
            ctx = df.index.asof(r["dt"])
            lr = df.at[ctx, "occupied"] if ctx is not pd.NaT and ctx in df.index else np.nan
            lr_s = f"{lr:.1f}°F" if not pd.isna(lr) else "?"
            print(f"    {r['dt']:%m/%d %H:%M}  {r['prev_f']:.1f} → {r['cool_f']:.1f}°F"
                  f"   (occupied was {lr_s})")

    _print_hourly(df)


def _print_hourly(df: pd.DataFrame):
    """Where in the day is the opportunity? Load vs available cooling."""
    h = df.groupby(df.index.hour).agg(
        ac=("ac_on", "mean"), fan=("fan_on", "mean"),
        occupied=("occupied", "mean"), gradient=("gradient", "mean"))
    if h["ac"].isna().all():
        return
    print(f"\n  Hourly profile:")
    print(f"    {'hr':>3}{'AC':>7}{'fan':>7}{'occupied':>10}{'usable Δ':>10}")
    for hr, r in h.iterrows():
        bar = "█" * int((r["ac"] or 0) * 14)
        print(f"    {hr:>3}{(r['ac'] or 0)*100:>6.0f}%{(r['fan'] or 0)*100:>6.0f}%"
              f"{r['occupied']:>9.1f}°{r['gradient']:>9.1f}°  {bar}")


# ── Plots ────────────────────────────────────────────────────────

def plot_daily(daily: pd.DataFrame, path: str):
    cur = daily[daily["regime"] == "comfort"]
    if cur.empty:
        return
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    x = cur.index

    ax = axes[0]
    ax.bar(x, cur["ac_hours"], color=C_AC, width=.6, label="AC compressor")
    ax.bar(x, cur["fan_hours"], color=C_FAN, width=.3, label="Fan blower")
    ax.set_ylabel("hours/day"); ax.legend(fontsize=8); ax.grid(alpha=.25, axis="y")
    ax.set_title("Runtime — compressor is the metric being minimised")

    ax = axes[1]
    ax.plot(x, cur["worst_excursion"], "o-", color=C_WARN, lw=2, label="Worst occupied reading")
    ax.plot(x, cur["occupied_mean"], "o--", color=C_WARN, lw=1, alpha=.5, label="Occupied mean")
    ax.axhline(COMFORT_CAP_F, color="#898781", ls=":", lw=2, label=f"{COMFORT_CAP_F:.0f}°F cap")
    for d, r in cur.iterrows():
        if r["setpoint_drops"]:
            ax.annotate("↓", (d, r["worst_excursion"]), color=C_AC,
                        fontsize=14, ha="center", va="bottom")
    ax.set_ylabel("°F"); ax.legend(fontsize=8); ax.grid(alpha=.25)
    ax.set_title("Comfort guardrail  (↓ = manual setpoint drop)")

    ax = axes[2]
    ax.bar(x, cur["usable_hours"], color=C_GRAD, width=.6,
           label=f"Hours with ≥{MIN_GRADIENT_F:.0f}°F available")
    ax.set_ylabel("hours/day"); ax.grid(alpha=.25, axis="y")
    # Outdoor high rides as a text annotation rather than a second y-scale —
    # two units on one axis invites misreading.
    for d, r in cur.iterrows():
        if not pd.isna(r["outdoor_high"]):
            ax.annotate(f"{r['outdoor_high']:.0f}°", (d, 0.4), color="#898781",
                        fontsize=7.5, ha="center")
    ax.legend(fontsize=8)
    ax.set_title("Cooling resource available  (grey = outdoor high that day)")

    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    plt.setp(axes[2].xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=8)
    fig.suptitle("Comfort objective — daily", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=130); plt.close(fig)
    print(f"  Saved: {path}")


def plot_hourly(df: pd.DataFrame, path: str, window_label: str = ""):
    h = df.groupby(df.index.hour).agg(
        ac=("ac_on", "mean"), fan=("fan_on", "mean"), gradient=("gradient", "mean"))
    if h["ac"].isna().all():
        return
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.4), sharex=True)
    axes[0].bar(h.index - .2, h["ac"] * 100, width=.4, color=C_AC, label="AC duty")
    axes[0].bar(h.index + .2, h["fan"] * 100, width=.4, color=C_FAN, label="Fan duty")
    axes[0].set_ylabel("% of hour"); axes[0].legend(fontsize=8); axes[0].grid(alpha=.25, axis="y")
    axes[0].set_title("When does the compressor run, and is the fan running then too?")

    axes[1].bar(h.index, h["gradient"], color=C_GRAD, width=.6)
    axes[1].axhline(MIN_GRADIENT_F, color="#898781", ls=":", lw=2,
                    label=f"{MIN_GRADIENT_F:.0f}°F minimum worth moving")
    axes[1].set_ylabel("°F available"); axes[1].set_xlabel("hour of day")
    axes[1].legend(fontsize=8); axes[1].grid(alpha=.25, axis="y")
    axes[1].set_title("Basement reservoir depth by hour")
    axes[1].set_xticks(range(0, 24, 2))
    if window_label:
        fig.suptitle(window_label, fontsize=9, color="#52514e", y=0.995)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)
    print(f"  Saved: {path}")


def plot_hall_bias(df: pd.DataFrame, path: str, window_label: str = ""):
    sub = df.dropna(subset=["hall_bias"])
    if sub.empty:
        return
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(sub.index, sub["living_room"], color=C_WARN, lw=1.5, label="Living room")
    ax.plot(sub.index, sub["hallway"], color=C_FAN, lw=1.5, ls="--", label="Hallway (Nest)")
    ax.axhline(COMFORT_CAP_F, color="#898781", ls=":", lw=2, label=f"{COMFORT_CAP_F:.0f}°F cap")
    ax.fill_between(sub.index, sub["hallway"], sub["living_room"],
                    where=sub["living_room"] > sub["hallway"],
                    color=C_WARN, alpha=.15, label="Living room hotter")
    ax.set_ylabel("°F"); ax.legend(fontsize=8, loc="best"); ax.grid(alpha=.25)
    ax.set_title("Hallway blind spot — the Nest cools on the dashed line"
                 + (f"\n{window_label}" if window_label else ""), fontsize=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d %H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)
    print(f"  Saved: {path}")


# ── Main ─────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="lookback window (may span the regime change)")
    ap.add_argument("--all", action="store_true", help="everything on record")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    if args.all:
        since = 0
    elif args.days:
        since = (pd.Timestamp.now() - pd.Timedelta(days=args.days)).timestamp()
    else:
        since = REGIME_START.timestamp()

    df, outages = load_frame(conn, since)
    if df.empty:
        print("No sensor data in range.")
        return
    drops = load_setpoint_drops(conn)
    drops = drops[drops["dt"] >= df.index.min()] if len(drops) else drops
    conn.close()

    if outages:
        print(f"\n  {len(outages)} hub outage(s) excluded from runtime totals:")
        for a, b in outages:
            print(f"    {a:%m/%d %H:%M} → {b:%m/%d %H:%M} "
                  f"({(b-a).total_seconds()/3600:.1f}h)")

    daily = daily_summary(df, drops)
    print_report(daily, df, drops)

    # State the window on every figure. The daily plot filters to the comfort
    # regime on its own; the hourly/bias plots pool whatever was loaded, so a
    # --all run mixes layouts and the label has to say so.
    n_legacy = int((daily["regime"] == "legacy-delta").sum())
    window_label = (f"{df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}")
    if n_legacy:
        window_label += (f"  ·  pools {n_legacy} pre-{REGIME_START:%b %-d} day(s) "
                         f"from the old sensor layout — mixed regimes")
    else:
        window_label += "  ·  comfort objective only"

    print("\nPlots:")
    plot_daily(daily,   os.path.join(OUT_DIR, "comfort_daily.png"))
    plot_hourly(df,     os.path.join(OUT_DIR, "comfort_hourly.png"), window_label)
    plot_hall_bias(df,  os.path.join(OUT_DIR, "comfort_hall_bias.png"), window_label)

    daily.to_csv(os.path.join(OUT_DIR, "comfort_daily.csv"))
    print(f"  Saved: {OUT_DIR}/comfort_daily.csv")


if __name__ == "__main__":
    main()
