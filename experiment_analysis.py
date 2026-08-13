#!/usr/bin/env python3
"""
SUPERSEDED 2026-08-12 by comfort_analysis.py — kept for reproducing the
Jun 8 – Jul 11 arm comparison, which concluded (duty_cycle won).

It answers a question the project no longer asks. Everything below assumes the
old sensor layout (4 basement + 1 main floor) and optimises the floor-to-floor
delta. Three of those sensors moved upstairs on 2026-08-12 and the objective
became "hold the occupied rooms under a comfort cap while the compressor runs
less", which makes delta a *resource* rather than the outcome. Run this only
against data from before that date.

Two-week experiment analysis: compare the four fan-control arms.

Arms:
  control          — original logic: fan ON when Δ > 3 °F, all day
  higher_threshold — fan ON only when Δ > 6 °F, all day
  burst            — fan ON only in first 120 min after quiet hours end (~6:30–8:30 AM)
  duty_cycle       — fan alternates 15 min ON / 15 min OFF all day (when Δ > 3 °F)

Metrics computed per arm-day during "active hours" (06:30–22:00 local):
  fan_hours     — total hours fan was ON
  delta_mean    — mean floor-to-floor Δ temp (°F)
  delta_max     — peak Δ temp (°F)
  basement_mean — mean basement avg temp (°F)
  nest_mean     — mean Nest/hallway temp (°F)
  outdoor_high  — daily high outdoor temp (°F) — weather covariate

Usage:
    python experiment_analysis.py
"""
import os
import sqlite3
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

from analyze import _to_local, find_hub_outages, NEST_LOCATION, LOCAL_TZ

DB_PATH   = os.getenv("DB_PATH", "nest.db")
OUT_DIR   = "analysis"
RESAMPLE  = "5min"
QUIET_START_H = 22      # 10:00 PM
QUIET_END_H   = 6       # 06:00 AM
QUIET_END_M   = 30      # 06:30 AM

ARM_COLORS = {
    "control":          "#1a73e8",
    "higher_threshold": "#e67700",
    "burst":            "#2e7d32",
    "duty_cycle":       "#7b1fa2",
}
ARM_LABELS = {
    "control":          "Control (Δ > 3 °F)",
    "higher_threshold": "Higher threshold (Δ > 6 °F)",
    "burst":            "Morning burst (first 2 h)",
    "duty_cycle":       "Duty cycle (15/15 min)",
}


# ── Data loading ─────────────────────────────────────────────

def load_experiment_log(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT date, arm FROM experiment_log ORDER BY date", conn
    )
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_sensor_frame(conn) -> pd.DataFrame:
    """5-min resampled wide frame with basement_avg, nest, delta_f columns."""
    raw = pd.read_sql_query(
        "SELECT ts, location, temp_f FROM sensor_readings", conn
    )
    raw["dt"] = _to_local(raw["ts"], "s")
    wide = (
        raw.set_index("dt")
           .groupby("location")["temp_f"]
           .resample(RESAMPLE).mean()
           .unstack(level=0)
    )
    basement_cols = [c for c in wide.columns if c.startswith("basement")]
    wide["basement_avg"] = wide[basement_cols].mean(axis=1)
    wide["nest"]         = wide.get(NEST_LOCATION)
    wide["delta_f"]      = wide["nest"] - wide["basement_avg"]
    return wide.dropna(subset=["nest", "basement_avg"])


def load_fan_frame(conn, wide_index) -> pd.Series:
    """Fan state forward-filled onto the wide sensor frame's index."""
    raw = pd.read_sql_query("SELECT ts, state FROM fan_events ORDER BY ts", conn)
    raw["dt"] = _to_local(raw["ts"], "ms")
    nest_dts = pd.read_sql_query(
        f"SELECT ts FROM sensor_readings WHERE location='{NEST_LOCATION}'", conn
    )
    nest_dts["dt"] = _to_local(nest_dts["ts"], "s")
    outages = find_hub_outages(nest_dts["dt"])
    fan = raw.set_index("dt")["state"].reindex(wide_index, method="ffill")
    for s, e in outages:
        fan[(wide_index >= s) & (wide_index <= e)] = np.nan
    return fan.map({"ON": True, "OFF": False})


def load_outdoor_frame(conn) -> pd.DataFrame:
    """Hourly outdoor weather, local time."""
    df = pd.read_sql_query("SELECT ts, temp_f FROM outdoor_weather ORDER BY ts", conn)
    df["dt"] = _to_local(df["ts"], "s")
    df = df.set_index("dt")["temp_f"].resample("1h").mean()
    return df.to_frame("outdoor_f")


# ── Per-day metric computation ───────────────────────────────

def active_mask(index: pd.DatetimeIndex) -> pd.Series:
    """True during active (non-quiet) hours: 06:30–22:00 local."""
    mins = index.hour * 60 + index.minute
    start = QUIET_END_H * 60 + QUIET_END_M    # 390
    end   = QUIET_START_H * 60                 # 1320
    return pd.Series((mins >= start) & (mins < end), index=index)


def day_metrics(date, wide, fan, outdoor) -> dict | None:
    """Compute metrics for one calendar date."""
    day_start = pd.Timestamp(date, tz=None)
    day_end   = day_start + pd.Timedelta(days=1)

    sub = wide[(wide.index >= day_start) & (wide.index < day_end)].copy()
    if len(sub) < 10:
        return None

    sub_fan = fan[(fan.index >= day_start) & (fan.index < day_end)]
    act = active_mask(sub.index)

    # Outdoor high for the date
    od = outdoor[(outdoor.index >= day_start) & (outdoor.index < day_end)]
    outdoor_high = od["outdoor_f"].max() if len(od) else np.nan
    outdoor_mean = od["outdoor_f"].mean() if len(od) else np.nan

    # Fan hours during active hours
    fan_active = sub_fan[act]
    step_h = pd.Timedelta(RESAMPLE).total_seconds() / 3600
    fan_hours = fan_active.dropna().sum() * step_h   # True counts as 1

    # Thermal metrics during active hours
    active_sub = sub[act]
    if active_sub.empty:
        return None

    # Total active hours with data (partial last day will be < 15.5 h)
    active_hours = len(active_sub) * step_h

    return {
        "fan_hours":     fan_hours,
        "fan_pct":       100 * fan_hours / active_hours if active_hours else np.nan,
        "delta_mean":    active_sub["delta_f"].mean(),
        "delta_max":     active_sub["delta_f"].max(),
        "delta_p90":     active_sub["delta_f"].quantile(0.9),
        "basement_mean": active_sub["basement_avg"].mean(),
        "nest_mean":     active_sub["nest"].mean(),
        "outdoor_high":  outdoor_high,
        "outdoor_mean":  outdoor_mean,
        "active_hours":  active_hours,
    }


# ── Analysis ─────────────────────────────────────────────────

def build_results(exp_log, wide, fan, outdoor) -> pd.DataFrame:
    rows = []
    for _, row in exp_log.iterrows():
        m = day_metrics(row["date"], wide, fan, outdoor)
        if m is None:
            continue
        rows.append({"date": row["date"], "arm": row["arm"], **m})
    return pd.DataFrame(rows)


def print_summary(results: pd.DataFrame):
    print("\n" + "═" * 78)
    print("EXPERIMENT SUMMARY — two-week A/B test  (Jun 8 – Jun 20, 2026)")
    print("═" * 78)

    today = datetime.now().date()
    partial = results[results["active_hours"] < 14]
    if not partial.empty:
        dates = ", ".join(str(d) for d in partial["date"])
        print(f"  Note: {dates} — partial day (< 14 active hours), included in stats\n")

    grp = results.groupby("arm")
    metrics = [
        ("fan_hours",    "Fan ON  (h/day)",   ".1f"),
        ("fan_pct",      "Fan ON  (%)",        ".0f"),
        ("delta_mean",   "Mean Δ  (°F)",       ".2f"),
        ("delta_max",    "Peak Δ  (°F)",       ".1f"),
        ("delta_p90",    "p90  Δ  (°F)",       ".2f"),
        ("basement_mean","Basement mean (°F)", ".1f"),
        ("nest_mean",    "Nest mean (°F)",     ".1f"),
        ("outdoor_high", "Outdoor high (°F)",  ".1f"),
    ]

    arms = ["control", "higher_threshold", "burst", "duty_cycle"]
    arms_present = [a for a in arms if a in results["arm"].values]

    header = f"  {'Metric':<24}" + "".join(f"  {ARM_LABELS[a][:18]:<20}" for a in arms_present)
    print(header)
    print("  " + "-" * (22 + 22 * len(arms_present)))

    for col, label, fmt in metrics:
        row_str = f"  {label:<24}"
        for arm in arms_present:
            arm_data = grp.get_group(arm)[col] if arm in grp.groups else pd.Series()
            n = len(arm_data.dropna())
            if n == 0:
                row_str += f"  {'—':<20}"
            else:
                mean = arm_data.mean()
                std  = arm_data.std() if n > 1 else np.nan
                val  = format(mean, fmt)
                s    = f"±{std:{fmt[1:]}}" if not np.isnan(std) else ""
                row_str += f"  {val + ' ' + s:<20}"
        print(row_str)

    print("\n  n (days per arm):")
    for arm in arms_present:
        n = len(grp.get_group(arm)) if arm in grp.groups else 0
        print(f"    {arm:<22} {n} day(s)")

    print()

    # Quick interpretation
    ctrl = grp.get_group("control") if "control" in grp.groups else None
    print("  Key findings:")
    for arm in [a for a in arms_present if a != "control"]:
        arm_df = grp.get_group(arm) if arm in grp.groups else pd.DataFrame()
        if arm_df.empty or ctrl is None:
            continue
        d_fan   = arm_df["fan_hours"].mean() - ctrl["fan_hours"].mean()
        d_delta = arm_df["delta_mean"].mean() - ctrl["delta_mean"].mean()
        d_base  = arm_df["basement_mean"].mean() - ctrl["basement_mean"].mean()
        sign_fan = "less" if d_fan < 0 else "more"
        sign_delta = "lower" if d_delta < 0 else "higher"
        print(f"    {ARM_LABELS[arm]:<40}  "
              f"{abs(d_fan):.1f} h/day {sign_fan} fan  |  "
              f"Δ {abs(d_delta):.2f}°F {sign_delta}  |  "
              f"basement {d_base:+.1f}°F")
    print()


# ── Plotting ─────────────────────────────────────────────────

def plot_arm_comparison(results: pd.DataFrame, path: str):
    arms = ["control", "higher_threshold", "burst", "duty_cycle"]
    arms_present = [a for a in arms if a in results["arm"].values]

    metrics = [
        ("fan_hours",    "Fan ON (h/day active)",   None),
        ("delta_mean",   "Mean floor Δ (°F)",        None),
        ("delta_p90",    "p90 floor Δ (°F)",         None),
        ("basement_mean","Basement mean temp (°F)",  None),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    axes = axes.flatten()

    for ax, (col, ylabel, ylim) in zip(axes, metrics):
        grp = results.groupby("arm")[col]
        means  = [grp.get_group(a).mean() if a in grp.groups else np.nan for a in arms_present]
        stds   = [grp.get_group(a).std()  if a in grp.groups else np.nan for a in arms_present]
        colors = [ARM_COLORS[a] for a in arms_present]
        ns     = [len(grp.get_group(a)) if a in grp.groups else 0 for a in arms_present]

        xs = range(len(arms_present))
        bars = ax.bar(xs, means, color=colors, alpha=0.75, zorder=3, width=0.5)

        # ±1 SD error bars
        for i, (m, s) in enumerate(zip(means, stds)):
            if not np.isnan(s):
                ax.errorbar(i, m, yerr=s, fmt='none', color='black',
                            capsize=5, linewidth=1.5, zorder=4)

        # Individual day dots
        for i, arm in enumerate(arms_present):
            if arm not in results["arm"].values:
                continue
            vals = results.loc[results["arm"] == arm, col].dropna()
            ax.scatter([i] * len(vals), vals, color=colors[i], s=40,
                       edgecolors='white', linewidths=0.8, zorder=5)

        ax.set_xticks(list(xs))
        ax.set_xticklabels([ARM_LABELS[a].split(" (")[0] for a in arms_present],
                           rotation=12, ha='right', fontsize=8)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(ylabel, fontsize=10, fontweight='bold')
        ax.grid(axis='y', alpha=0.3, zorder=0)

        # n labels
        for i, n in enumerate(ns):
            ax.text(i, ax.get_ylim()[0] - 0.03 * (ax.get_ylim()[1] - ax.get_ylim()[0]),
                    f"n={n}", ha='center', fontsize=7, color='#666')

        if ylim:
            ax.set_ylim(ylim)

    fig.suptitle("A/B Experiment: Fan-control arm comparison  (Jun 8–20, 2026)\n"
                 "Bars = mean ± 1 SD · dots = individual days · outdoor temp not yet adjusted",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_weather_adjusted(results: pd.DataFrame, path: str):
    """Scatter: fan_hours and delta_mean vs outdoor_high, colored by arm."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    for ax, (col, ylabel) in zip(axes, [
        ("fan_hours",  "Fan ON (h/day active)"),
        ("delta_mean", "Mean floor Δ (°F)"),
    ]):
        for arm, grp in results.groupby("arm"):
            ax.scatter(grp["outdoor_high"], grp[col],
                       color=ARM_COLORS.get(arm, "gray"),
                       label=ARM_LABELS.get(arm, arm),
                       s=60, edgecolors='white', linewidths=0.8, zorder=3)
            # Trend line if ≥ 3 points
            if len(grp) >= 3:
                m, b = np.polyfit(grp["outdoor_high"], grp[col], 1)
                x = np.linspace(grp["outdoor_high"].min(), grp["outdoor_high"].max(), 50)
                ax.plot(x, m * x + b, color=ARM_COLORS.get(arm, "gray"),
                        lw=1.2, alpha=0.6, linestyle='--')

        ax.set_xlabel("Daily outdoor high (°F)", fontsize=9)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(ylabel, fontsize=10, fontweight='bold')
        ax.legend(fontsize=7.5, loc='best')
        ax.grid(alpha=0.3)

    fig.suptitle("Weather sensitivity: does outdoor temp explain the arm differences?",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_timeseries(results: pd.DataFrame, wide: pd.DataFrame,
                    fan: pd.Series, path: str):
    """Daily timeline colored by arm — shows the full 2-week run."""
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)

    # Limit to experiment period
    exp_start = pd.Timestamp("2026-06-08")
    exp_end   = pd.Timestamp("2026-06-21")
    mask = (wide.index >= exp_start) & (wide.index < exp_end)
    w  = wide[mask]
    f  = fan[mask]

    # Arm background shading
    for _, row in results.iterrows():
        d = pd.Timestamp(row["date"])
        color = ARM_COLORS.get(row["arm"], "gray")
        for ax in axes:
            ax.axvspan(d, d + pd.Timedelta(days=1), alpha=0.08, color=color, zorder=0)

    # Panel 1: Floor delta
    axes[0].plot(w.index, w["delta_f"], color="#333", lw=0.7, alpha=0.8)
    axes[0].axhline(3.0, color="#1a73e8", lw=1, ls="--", alpha=0.7, label="3°F threshold")
    axes[0].axhline(6.0, color="#e67700", lw=1, ls="--", alpha=0.7, label="6°F threshold")
    axes[0].set_ylabel("Floor Δ (°F)")
    axes[0].set_title("Floor delta (Nest − basement avg)")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[0].grid(alpha=0.2)

    # Panel 2: Basement & Nest temps
    axes[1].plot(w.index, w["basement_avg"], color="#e8711a", lw=0.7, alpha=0.85, label="Basement avg")
    axes[1].plot(w.index, w["nest"],         color="#1a73e8", lw=0.7, alpha=0.85, label="Nest/hallway")
    axes[1].set_ylabel("Temperature (°F)")
    axes[1].set_title("Indoor temperatures")
    axes[1].legend(loc="upper right", fontsize=8)
    axes[1].grid(alpha=0.2)

    # Panel 3: Fan state
    fan_bool = f.fillna(False).astype(float)
    axes[2].fill_between(w.index, 0, fan_bool, step="post",
                         color="#43a047", alpha=0.55, label="Fan ON")
    axes[2].set_ylabel("Fan")
    axes[2].set_yticks([0, 1]); axes[2].set_yticklabels(["OFF", "ON"])
    axes[2].set_title("Fan state")
    axes[2].set_ylim(-0.05, 1.15)
    axes[2].grid(alpha=0.2)

    # Arm legend patches
    from matplotlib.patches import Patch
    legend_handles = [Patch(facecolor=ARM_COLORS[a], alpha=0.4, label=ARM_LABELS[a])
                      for a in ARM_COLORS if a in results["arm"].values]
    fig.legend(handles=legend_handles, loc="lower center", ncol=4,
               fontsize=8, title="Background color = active arm",
               bbox_to_anchor=(0.5, -0.01))

    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    axes[2].xaxis.set_major_locator(mdates.DayLocator())
    plt.setp(axes[2].xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=8)

    fig.suptitle("Two-week experiment run  (Jun 8–20, 2026)  — colored by active arm",
                 fontsize=11)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.1)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_daily_heatmap(results: pd.DataFrame, path: str):
    """One-row-per-day heatmap of key metrics, ordered by date."""
    df = results.sort_values("date").copy()
    df["day"] = df["date"].apply(lambda d: d.strftime("%-m/%-d"))

    fig, axes = plt.subplots(1, 4, figsize=(13, max(4, len(df) * 0.42 + 1.5)))

    panels = [
        ("fan_hours",     "Fan h/day", "Blues"),
        ("delta_mean",    "Mean Δ °F", "Oranges"),
        ("delta_max",     "Peak Δ °F", "Reds"),
        ("outdoor_high",  "Outdoor\nhigh °F", "YlOrBr"),
    ]

    for ax, (col, title, cmap) in zip(axes, panels):
        vals = df[col].values.reshape(-1, 1)
        im = ax.imshow(vals, cmap=cmap, aspect="auto")
        ax.set_xticks([0]); ax.set_xticklabels([title], fontsize=8.5)
        ax.set_yticks(range(len(df)))
        ax.set_yticklabels([
            f"{row.day}  {row.arm[:3].upper()}"
            for _, row in df.iterrows()
        ], fontsize=8)
        for i, v in enumerate(df[col]):
            ax.text(0, i, f"{v:.1f}", ha='center', va='center',
                    fontsize=7.5, color='white' if v > (vals.max() * 0.6) else 'black')
        plt.colorbar(im, ax=ax, shrink=0.6, pad=0.02)

    fig.suptitle("Day-by-day summary  (arm abbreviation: CON / HIG / BUR / DUT)",
                 fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  Saved: {path}")


# ── Main ─────────────────────────────────────────────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    exp_log = load_experiment_log(conn)
    print(f"Loaded {len(exp_log)} experiment days  "
          f"({exp_log['arm'].value_counts().to_dict()})")

    print("Building sensor frame...")
    wide = load_sensor_frame(conn)
    fan  = load_fan_frame(conn, wide.index)
    outdoor = load_outdoor_frame(conn)
    conn.close()

    print("Computing per-day metrics...")
    results = build_results(exp_log, wide, fan, outdoor)

    print_summary(results)

    print("Generating plots...")
    plot_timeseries(results, wide, fan,
                    os.path.join(OUT_DIR, "experiment_timeseries.png"))
    plot_arm_comparison(results,
                        os.path.join(OUT_DIR, "experiment_arm_comparison.png"))
    plot_weather_adjusted(results,
                          os.path.join(OUT_DIR, "experiment_weather.png"))
    plot_daily_heatmap(results,
                       os.path.join(OUT_DIR, "experiment_heatmap.png"))

    results.to_csv(os.path.join(OUT_DIR, "experiment_results.csv"), index=False)
    print(f"\n  Saved: {OUT_DIR}/experiment_results.csv")


if __name__ == "__main__":
    main()
