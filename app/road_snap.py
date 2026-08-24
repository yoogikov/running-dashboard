"""Snaps a cleaned GPS track to the nearest road/path network using the
public OSRM demo server, `foot` profile (matches footpaths/sidewalks, not
just roads — much better fit for running routes than the `car` profile).

Best-effort: on any failure (network error, timeout, no match found) the
original points for that piece are returned unchanged, so a run's data is
never lost or blocked on this step.

Two things learned empirically against the real public server (not
documented anywhere reliable): raw GPS traces sampled every ~1s pack points
only 1-2m apart, which adds request volume for no matching benefit, so the
trace is decimated by distance first. And the server's actual per-request
point cap is *much* lower than commonly-cited numbers (as low as ~10 for a
dense urban trace, not ~100) and isn't fixed — so instead of a hardcoded
chunk size, a chunk that gets rejected as "too big" is bisected and retried,
adapting to whatever the real limit turns out to be for that request.

Fair-use note: router.project-osrm.org is a free shared demo service, not
meant for bulk/heavy traffic. This runs once per imported run — well within
reasonable personal use — never in a loop over many runs at once.
"""
import math
from datetime import datetime

import requests

OSRM_MATCH_URL = "https://router.project-osrm.org/match/v1/foot/{coords}"
MIN_POINT_SPACING_M = 8     # decimate dense GPS traces to at least this spacing first
INITIAL_CHUNK_SIZE = 20     # starting guess per request; shrinks via bisection if rejected
MIN_CHUNK_SIZE = 2          # below this, give up on matching and keep the original points
REQUEST_TIMEOUT_S = 10


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


def _decimate(points: list[dict], min_spacing_m: float = MIN_POINT_SPACING_M) -> list[dict]:
    if len(points) < 2:
        return points
    kept = [points[0]]
    for p in points[1:]:
        last = kept[-1]
        if _haversine_m(last["lat"], last["lon"], p["lat"], p["lon"]) >= min_spacing_m:
            kept.append(p)
    if kept[-1] is not points[-1]:
        kept.append(points[-1])  # always keep the true endpoint
    return kept


def _request_match(points: list[dict]):
    """One HTTP request attempt. Returns (status, snapped_points_or_None)."""
    coords = ";".join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in points)
    url = OSRM_MATCH_URL.format(coords=coords)
    params = {"geometries": "geojson", "overview": "full"}

    times = [_parse_time(p.get("time")) for p in points]
    valid_times = [t for t in times if t is not None]
    if len(valid_times) == len(times):
        base = valid_times[0]
        params["timestamps"] = ";".join(
            str(int((t - base).total_seconds())) for t in valid_times
        )

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
    except Exception:
        return None, None

    if resp.status_code != 200:
        try:
            code = resp.json().get("code")
        except Exception:
            code = None
        return code, None

    try:
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("matchings"):
            return data.get("code"), None
        snapped = []
        for matching in data["matchings"]:
            for lon, lat in matching["geometry"]["coordinates"]:
                snapped.append({"lat": lat, "lon": lon, "elevation_m": None, "time": None})
        return "Ok", (snapped or None)
    except Exception:
        return None, None


def _match_adaptive(points: list[dict]) -> list[dict] | None:
    """Matches a chunk, bisecting and retrying if the server rejects it as
    too large. Returns None (caller falls back to originals) if it can't be
    matched even at MIN_CHUNK_SIZE."""
    if len(points) < 2:
        return None

    code, result = _request_match(points)
    if result:
        return result

    if code == "TooBig" and len(points) > MIN_CHUNK_SIZE:
        mid = len(points) // 2
        left = _match_adaptive(points[:mid])
        right = _match_adaptive(points[mid:])
        if left is None and right is None:
            return None
        return (left or points[:mid]) + (right or points[mid:])

    return None  # NoMatch or any other failure: not fixable by resizing


def snap_to_road(points: list[dict]) -> list[dict]:
    """Attempts to snap points to the road/path network. Falls back to the
    original (decimated) points wherever matching fails."""
    usable = [p for p in points if p.get("lat") is not None and p.get("lon") is not None]
    if len(usable) < 2:
        return points

    decimated = _decimate(usable)

    result = []
    any_success = False
    for i in range(0, len(decimated), INITIAL_CHUNK_SIZE):
        chunk = decimated[i:i + INITIAL_CHUNK_SIZE]
        matched = _match_adaptive(chunk)
        if matched:
            any_success = True
            result.extend(matched)
        else:
            result.extend(chunk)

    return result if any_success else points
