"""Derived metrics and progress/hold/rest flagging, built from logged runs.

No HR data required: uses pace, RPE, manually-counted morning pulse, and
weekly volume trends, per the framework the plan was based on.
"""
from datetime import datetime
from statistics import mean


def _to_date(d: str):
    return datetime.fromisoformat(d).date()


def weekly_summary(runs: list[dict]) -> list[dict]:
    """Group runs into ISO weeks, return distance/time/avg RPE per week, oldest first."""
    weeks: dict[tuple, dict] = {}
    for r in runs:
        d = _to_date(r["date"])
        key = d.isocalendar()[:2]  # (iso_year, iso_week)
        wk = weeks.setdefault(key, {"key": key, "distance_km": 0.0, "duration_sec": 0,
                                      "rpe_values": [], "start": d})
        wk["distance_km"] += r["distance_km"] or 0
        wk["duration_sec"] += r["duration_sec"] or 0
        if r.get("rpe") is not None:
            wk["rpe_values"].append(r["rpe"])
        wk["start"] = min(wk["start"], d)

    result = []
    for wk in sorted(weeks.values(), key=lambda w: w["key"]):
        result.append({
            "week_start": wk["start"].isoformat(),
            "distance_km": round(wk["distance_km"], 2),
            "duration_sec": wk["duration_sec"],
            "avg_rpe": round(mean(wk["rpe_values"]), 1) if wk["rpe_values"] else None,
        })
    return result


def volume_change_pct(weeks: list[dict]) -> float | None:
    """% change in distance between the two most recent weeks."""
    if len(weeks) < 2:
        return None
    prev, curr = weeks[-2]["distance_km"], weeks[-1]["distance_km"]
    if prev == 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def easy_pace_trend(runs: list[dict], easy_rpe_max: int = 4, window: int = 5) -> list[dict]:
    """Pace (sec/km) on easy-effort runs (RPE <= easy_rpe_max) over time.

    This is the HR-free substitute for 'pace at fixed heart rate': pace at a
    fixed *perceived* effort. A downward trend = improving aerobic fitness.
    """
    easy = [r for r in runs if r.get("rpe") is not None and r["rpe"] <= easy_rpe_max
            and r.get("avg_pace_sec_per_km")]
    easy.sort(key=lambda r: r["date"])
    easy = easy[-window:] if window else easy
    return [{"date": r["date"], "pace_sec_per_km": r["avg_pace_sec_per_km"]} for r in easy]


def pulse_baseline(runs: list[dict], window: int = 14) -> float | None:
    """Rolling average morning pulse over the most recent `window` logged runs."""
    pulses = [r["morning_pulse"] for r in runs if r.get("morning_pulse") is not None]
    if not pulses:
        return None
    recent = pulses[-window:]
    return round(mean(recent), 1)


def rpe_trend_flag(runs: list[dict], easy_rpe_max: int = 4, lookback: int = 5) -> str | None:
    """Detects RPE creeping up on runs that started out easy-effort.

    Only fires if the run at the start of the window was genuine easy effort
    (<= easy_rpe_max) but RPE has since drifted upward for the same pace/routine.
    """
    rated = [r for r in runs if r.get("rpe") is not None]
    rated.sort(key=lambda r: r["date"])
    recent = rated[-lookback:]
    if len(recent) < lookback or recent[0]["rpe"] > easy_rpe_max:
        return None
    values = [r["rpe"] for r in recent]
    if values[-1] - values[0] >= 2 and all(x <= y for x, y in zip(values, values[1:])):
        return "RPE has been trending up on recent runs — possible accumulated fatigue."
    return None


def readiness_flag(latest_run: dict, all_runs: list[dict]) -> dict:
    """Per-day progress/hold/rest flag, following the HR-free decision table.

    Returns {"status": "progress"|"hold"|"rest", "reasons": [...]}.
    """
    reasons = []
    status = "progress"

    baseline = pulse_baseline([r for r in all_runs if r["date"] < latest_run["date"]])
    pulse = latest_run.get("morning_pulse")
    if baseline is not None and pulse is not None:
        delta = pulse - baseline
        if delta >= 7:
            status = "rest"
            reasons.append(f"Morning pulse {pulse} bpm is {delta:.0f} above your baseline "
                            f"({baseline} bpm) — take it easy or rest today.")
        elif delta >= 5:
            status = "hold"
            reasons.append(f"Morning pulse {pulse} bpm is {delta:.0f} above baseline "
                            f"({baseline} bpm) — mild fatigue signal.")

    weeks = weekly_summary([r for r in all_runs if r["date"] <= latest_run["date"]])
    change = volume_change_pct(weeks)
    if change is not None and change > 10:
        if status == "progress":
            status = "hold"
        reasons.append(f"Weekly volume jumped {change}% — consider capping growth at ~10%/week.")

    rpe_note = rpe_trend_flag([r for r in all_runs if r["date"] <= latest_run["date"]])
    if rpe_note:
        if status == "progress":
            status = "hold"
        reasons.append(rpe_note)

    soreness = (latest_run.get("soreness_notes") or "").strip()
    if soreness:
        status = "rest"
        reasons.append(f"Soreness/pain noted: \"{soreness}\" — confirm it's normal fatigue, "
                        f"not injury, before running through it.")

    if not reasons:
        reasons.append("No red flags in recent data — clear to progress as planned.")

    return {"status": status, "reasons": reasons}


def cutback_week_due(weeks: list[dict], every_n_weeks: int = 4) -> bool:
    """True if it's been >= every_n_weeks since the last notable volume drop."""
    if len(weeks) < every_n_weeks:
        return False
    recent = weeks[-every_n_weeks:]
    distances = [w["distance_km"] for w in recent]
    # a cutback = a week at least 20% below the preceding week
    for prev, curr in zip(distances, distances[1:]):
        if prev > 0 and (prev - curr) / prev >= 0.2:
            return False
    return True


def format_pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "-"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"
