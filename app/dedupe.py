"""Rejects re-imports of a run that is already stored.

Re-dropping the same export is easy to do by accident — Strava's per-activity
GPX filenames come from the activity *title*, so two different downloads of the
same run can arrive under different names, and a bulk archive contains every
run you have ever logged, including the ones already imported. Without a check,
each re-import inserts a second copy of the run and folds its points into the
merged route graph a second time, inflating that road's weight (and so its
brightness) on evidence that isn't independent.

IDENTITY IS THE START TIMESTAMP. Two genuinely different runs never begin in
the same second, and the timestamp is untouched by the import pipeline:
gps_smooth.remove_outliers always keeps points[0], and smooth() carries `time`
through unchanged, so an identical file re-imported yields a bit-identical
start time. STARTED_TOL_SEC exists only for the case where the *same* activity
is exported twice in different shapes (GPX vs TCX, or a Strava export format
change) and the leading sample differs slightly.

Some runs have no usable timestamp at all — treadmill activities carry no GPS,
and a few exporters strip <time>. Those fall back to matching the run's shape:
same calendar date, plus distance and duration that agree within a tolerance.
That is deliberately weaker than the timestamp rule and is the reason the
tolerances are proportional rather than absolute — a 400 m interval session and
a 20 km long run should not need the same slack to be told apart.
"""
from datetime import datetime

import db

STARTED_TOL_SEC = 90.0   # same activity, slightly different export; not two runs
DIST_TOL_FRAC = 0.01     # 1% of distance...
DIST_TOL_KM = 0.05       # ...or 50 m, whichever is looser
DUR_TOL_FRAC = 0.01      # 1% of duration...
DUR_TOL_SEC = 30.0       # ...or 30 s, whichever is looser


def _parse(ts):
    """ISO timestamp -> aware/naive datetime, or None if absent/unparseable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def start_time(points) -> str | None:
    """Earliest timestamp in a parsed track, as an ISO string."""
    times = [p.get("time") for p in (points or []) if p.get("time")]
    if not times:
        return None
    parsed = [(t, _parse(t)) for t in times]
    parsed = [(t, d) for t, d in parsed if d is not None]
    if not parsed:
        return None
    return min(parsed, key=lambda td: td[1])[0]


def _same_instant(a: str | None, b: str | None) -> bool:
    da, dbb = _parse(a), _parse(b)
    if da is None or dbb is None:
        return False
    # Comparing an aware datetime against a naive one raises; treat a missing
    # offset as UTC, which is what every exporter here actually writes.
    if (da.tzinfo is None) != (dbb.tzinfo is None):
        from datetime import timezone
        da = da.replace(tzinfo=timezone.utc) if da.tzinfo is None else da
        dbb = dbb.replace(tzinfo=timezone.utc) if dbb.tzinfo is None else dbb
    return abs((da - dbb).total_seconds()) <= STARTED_TOL_SEC


def _close(a, b, frac, floor) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(floor, frac * max(abs(a), abs(b)))


def find_duplicate(candidate: dict, existing=None) -> int | None:
    """Returns the id of the stored run `candidate` duplicates, else None.

    `candidate` is a parsed+processed run dict (as handed to db.insert_run),
    and must still carry its "track_points" for the timestamp rule to apply.
    `existing` is injectable for testing; it defaults to every stored run.
    """
    if existing is None:
        existing = db.get_run_identities()

    cand_start = start_time(candidate.get("track_points"))

    for row in existing:
        row_start = row["start_time"] if "start_time" in row.keys() else None
        if cand_start and row_start:
            # Both sides timestamped: this rule alone decides, in both
            # directions. A shape match cannot overrule a timestamp mismatch,
            # or two genuine repeats of the same loop would collapse into one.
            if _same_instant(cand_start, row_start):
                return row["id"]
            continue
        if candidate.get("date") != row["date"]:
            continue
        if _close(candidate.get("distance_km"), row["distance_km"],
                  DIST_TOL_FRAC, DIST_TOL_KM) and \
           _close(candidate.get("duration_sec"), row["duration_sec"],
                  DUR_TOL_FRAC, DUR_TOL_SEC):
            return row["id"]
    return None
