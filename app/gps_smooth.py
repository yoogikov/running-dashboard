"""GPS track cleanup: drops implausible points (GPS 'teleport' outliers) and
smooths the remainder with a small moving average, before a run is stored or
sent for road-snapping. Pure local computation, no network involved.
"""
import math
from datetime import datetime

MAX_SPEED_MPS = 8.0  # ~29 km/h; generous for sprinting, catches GPS jump artifacts
SMOOTH_WINDOW = 2    # points on each side to average over (5-point moving average)


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _parse_time(t):
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def remove_outliers(points: list[dict]) -> list[dict]:
    """Drops points that imply an impossible speed from the last *kept* point,
    so a single bad fix doesn't also poison comparisons against later points."""
    if len(points) < 3:
        return points

    cleaned = [points[0]]
    for p in points[1:]:
        prev = cleaned[-1]
        if prev.get("lat") is None or p.get("lat") is None:
            cleaned.append(p)
            continue

        dist = _haversine_m(prev["lat"], prev["lon"], p["lat"], p["lon"])
        t1, t2 = _parse_time(prev.get("time")), _parse_time(p.get("time"))
        if t1 and t2:
            dt = (t2 - t1).total_seconds()
            if dt > 0 and (dist / dt) > MAX_SPEED_MPS:
                continue  # drop: implies an impossible speed, likely a GPS glitch

        cleaned.append(p)
    return cleaned


def smooth(points: list[dict], window: int = SMOOTH_WINDOW) -> list[dict]:
    """Centered moving-average smoothing on lat/lon to reduce GPS jitter.
    Elevation and time are carried through unchanged."""
    n = len(points)
    if n < 3:
        return points

    smoothed = []
    for i, p in enumerate(points):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        neighborhood = [pt for pt in points[lo:hi] if pt.get("lat") is not None]
        if not neighborhood:
            smoothed.append(p)
            continue
        avg_lat = sum(pt["lat"] for pt in neighborhood) / len(neighborhood)
        avg_lon = sum(pt["lon"] for pt in neighborhood) / len(neighborhood)
        smoothed.append({**p, "lat": avg_lat, "lon": avg_lon})
    return smoothed


def clean_track(points: list[dict]) -> list[dict]:
    """Full pipeline: drop GPS teleport outliers, then smooth remaining jitter."""
    return smooth(remove_outliers(points))
