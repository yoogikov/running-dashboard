"""Minimal GPX/TCX parsing using only the standard library.

Extracts what the dashboard needs: date, distance, duration, elevation gain,
and a track-point list (lat/lon/elevation/time) for possible future mapping.
No external dependency (e.g. gpxpy) required.
"""
import math
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
TCX_NS = "{http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2}"


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def parse_gpx(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    ns = GPX_NS if root.tag.startswith(GPX_NS) else ""

    points = []
    for trkpt in root.iter(f"{ns}trkpt"):
        lat_attr, lon_attr = trkpt.get("lat"), trkpt.get("lon")
        if lat_attr is None or lon_attr is None:
            continue
        lat, lon = float(lat_attr), float(lon_attr)
        ele_el = trkpt.find(f"{ns}ele")
        time_el = trkpt.find(f"{ns}time")
        points.append({
            "lat": lat,
            "lon": lon,
            "elevation_m": float(ele_el.text) if ele_el is not None and ele_el.text else None,
            "time": time_el.text if time_el is not None else None,
        })

    return summarize_points(points, source_file=path.name)


def parse_tcx(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    ns = TCX_NS if root.tag.startswith(TCX_NS) else ""

    points = []
    for trkpt in root.iter(f"{ns}Trackpoint"):
        pos = trkpt.find(f"{ns}Position")
        lat = lon = None
        if pos is not None:
            lat_el = pos.find(f"{ns}LatitudeDegrees")
            lon_el = pos.find(f"{ns}LongitudeDegrees")
            lat = float(lat_el.text) if lat_el is not None and lat_el.text else None
            lon = float(lon_el.text) if lon_el is not None and lon_el.text else None
        ele_el = trkpt.find(f"{ns}AltitudeMeters")
        time_el = trkpt.find(f"{ns}Time")
        points.append({
            "lat": lat,
            "lon": lon,
            "elevation_m": float(ele_el.text) if ele_el is not None and ele_el.text else None,
            "time": time_el.text if time_el is not None else None,
        })

    return summarize_points(points, source_file=path.name)


def summarize_points(points: list[dict], source_file: str) -> dict:
    timed_points = [p for p in points if p.get("time")]
    dist_km = 0.0
    elevation_gain = 0.0

    prev = None
    for p in points:
        if prev and p["lat"] is not None and prev["lat"] is not None:
            dist_km += _haversine_km(prev["lat"], prev["lon"], p["lat"], p["lon"])
        if prev and p.get("elevation_m") is not None and prev.get("elevation_m") is not None:
            delta = p["elevation_m"] - prev["elevation_m"]
            if delta > 0:
                elevation_gain += delta
        prev = p

    duration_sec = 0
    date_str = None
    if timed_points:
        times = [_parse_iso(p["time"]) for p in timed_points]
        times = [t for t in times if t is not None]
        if times:
            start, end = min(times), max(times)
            duration_sec = int((end - start).total_seconds())
            date_str = start.date().isoformat()

    if not date_str:
        date_str = datetime.now().date().isoformat()

    avg_pace = (duration_sec / dist_km) if dist_km > 0 and duration_sec > 0 else None

    return {
        "date": date_str,
        "source_file": source_file,
        "distance_km": round(dist_km, 3),
        "duration_sec": duration_sec,
        "avg_pace_sec_per_km": round(avg_pace, 1) if avg_pace else None,
        "elevation_gain_m": round(elevation_gain, 1),
        "track_points": points,
    }


def parse_file(path: str | Path) -> dict:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".gpx":
        return parse_gpx(path)
    elif suffix == ".tcx":
        return parse_tcx(path)
    else:
        raise ValueError(f"Unsupported file type: {suffix} (expected .gpx or .tcx)")
