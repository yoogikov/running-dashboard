"""Turns logged GPS routes into a 'heat' effect on the map.

Density is computed with a real distance query, not grid-binning: for every
point on every run, count how many *other* runs have at least one point
within K_METERS of it *and* are travelling in roughly the same direction
(within MAX_ANGLE_DIFF_DEG — opposite direction counts as "the same", since
a street is the same path whichever way you ran it; only a genuinely
different-angle crossing street gets excluded). That count (plus 1, for the
run itself) drives the color. A spatial hash — bucket size >= K_METERS —
turns the distance check from an all-pairs comparison into a neighbor
lookup, so it stays fast with many runs/points while still being an exact
"within k meters" check rather than a quantized "same grid cell" one.

Tkinter's Canvas (which tkintermapview draws on) has no real alpha
compositing, so literal stacked-translucent-lines don't actually blend —
this density-driven coloring is the substitute: well-worn streets read hot,
one-off routes read cool, without relying on canvas alpha blending.
"""
import math

K_METERS = 30              # radius within which another run's point counts as "nearby" — tune this
MAX_ANGLE_DIFF_DEG = 30    # max direction-of-travel difference to still count as "the same
                           # path" (opposite direction folds to 0° — see _orientation_diff)
BUCKET_DEG = 0.00035   # spatial hash bucket size (~35-40m); kept >= K_METERS so a 3x3 bucket
                       # neighborhood always fully covers the K_METERS radius around any point
SEGMENT_CHUNK = 4      # points per colored sub-segment, for a smooth-ish gradient

# Single-hue orange ramp (r, g, b): dark/muted -> vivid, brightness only, no hue
# shift into yellow. Density is signaled by brightness + dithering (stipple),
# not by changing hue.
_GRADIENT = [
    (140, 70, 30),    # dark/muted orange: no other runs nearby
    (185, 100, 35),   # orange: a few other runs nearby
    (230, 130, 40),   # bright/vivid orange: many other runs nearby, well-worn route
]


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bucket(lat: float, lon: float) -> tuple:
    return (round(lat / BUCKET_DEG), round(lon / BUCKET_DEG))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, in degrees, 0-360."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _orientation_diff(b1: float, b2: float) -> float:
    """Smallest angle between two bearings, 0-90, treating exactly-opposite
    (180° apart) as identical — a street's the same path either direction."""
    diff = abs(b1 - b2) % 180
    return min(diff, 180 - diff)


def compute_directions(points: list[tuple]) -> list[float | None]:
    """Direction of travel at each point (0-360°), estimated from the segment
    straddling it (previous point -> next point). None where undeterminable
    (a run with a single point, or two identical consecutive points)."""
    n = len(points)
    directions: list[float | None] = []
    for i in range(n):
        a = points[i - 1] if i > 0 else points[i]
        b = points[i + 1] if i < n - 1 else points[i]
        if a == b:
            directions.append(None)
        else:
            directions.append(_bearing_deg(a[0], a[1], b[0], b[1]))
    return directions


def build_spatial_index(runs_points: dict, runs_bearings: dict) -> dict:
    """runs_points: {run_id: [(lat, lon), ...]}, runs_bearings: {run_id: [bearing, ...]}
    (see compute_directions). Returns {bucket: [(run_id, lat, lon, bearing), ...]}."""
    index: dict = {}
    for run_id, points in runs_points.items():
        for (lat, lon), bearing in zip(points, runs_bearings[run_id]):
            index.setdefault(_bucket(lat, lon), []).append((run_id, lat, lon, bearing))
    return index


def count_nearby_runs(
    lat: float, lon: float, own_bearing: float | None, own_run_id, index: dict,
    k_meters: float = K_METERS, max_angle_diff: float = MAX_ANGLE_DIFF_DEG,
) -> int:
    """Counts distinct OTHER runs with at least one point within k_meters of
    (lat, lon) that's also travelling within max_angle_diff of own_bearing
    (opposite direction counts as matching). If either bearing is undeterminable,
    the angle check is skipped (distance alone decides)."""
    bx, by = _bucket(lat, lon)
    found = set()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for run_id, plat, plon, pbearing in index.get((bx + dx, by + dy), ()):
                if run_id == own_run_id or run_id in found:
                    continue
                if _haversine_m(lat, lon, plat, plon) > k_meters:
                    continue
                if own_bearing is not None and pbearing is not None:
                    if _orientation_diff(own_bearing, pbearing) > max_angle_diff:
                        continue
                found.add(run_id)
    return len(found)


def compute_point_densities(
    runs_points: dict, k_meters: float = K_METERS, max_angle_diff: float = MAX_ANGLE_DIFF_DEG,
) -> dict:
    """Full from-scratch computation: O(all points x all points nearby).
    Correct but far too slow for a real-sized run history recomputed on every
    launch/import (tens of seconds for tens of thousands of points, see
    density_cache.py) — kept only as what density_cache.py's backfill calls
    internally, run by run, never as a single all-at-once call in practice.

    Returns {run_id: [density, ...]}, aligned with each run's point list.
    density = 1 (for the run itself) + number of other runs passing within
    k_meters while travelling in roughly the same (or exactly opposite)
    direction."""
    runs_bearings = {run_id: compute_directions(points) for run_id, points in runs_points.items()}
    index = build_spatial_index(runs_points, runs_bearings)
    return {
        run_id: [
            1 + count_nearby_runs(lat, lon, bearing, run_id, index, k_meters, max_angle_diff)
            for (lat, lon), bearing in zip(points, runs_bearings[run_id])
        ]
        for run_id, points in runs_points.items()
    }


# ---------- incremental density (see density_cache.py for the full picture) ----------
#
# The insight that makes this fast at real scale: two runs that don't involve
# each other never need to be re-compared. So instead of recomputing every
# point's density from scratch on every launch/import, each point's count is
# computed once and cached (in the DB — see density_cache.py), and adding a
# new run only requires checking IT against the (much larger) existing set —
# never re-checking existing runs against each other, since that relationship
# never changes once computed.


def build_indexed_points(points_by_id: list[tuple]) -> list[tuple]:
    """points_by_id: [(point_id, lat, lon), ...] for ONE run, in seq order
    (bearing needs sequential context). Returns [(point_id, lat, lon, bearing), ...]."""
    coords = [(lat, lon) for _, lat, lon in points_by_id]
    bearings = compute_directions(coords)
    return [(pid, lat, lon, b) for (pid, lat, lon), b in zip(points_by_id, bearings)]


def build_combined_index(indexed_points_by_run: dict) -> dict:
    """indexed_points_by_run: {run_id: [(point_id, lat, lon, bearing), ...]}
    (see build_indexed_points). Returns {bucket: [(run_id, point_id, lat, lon, bearing), ...]}."""
    index: dict = {}
    for run_id, points in indexed_points_by_run.items():
        for pid, lat, lon, bearing in points:
            index.setdefault(_bucket(lat, lon), []).append((run_id, pid, lat, lon, bearing))
    return index


def density_against_index(
    query_points: list[tuple], own_run_id, index: dict,
    k_meters: float = K_METERS, max_angle_diff: float = MAX_ANGLE_DIFF_DEG,
) -> dict:
    """query_points: [(point_id, lat, lon, bearing), ...] — typically a single
    new run's indexed points. `index` should NOT contain own_run_id's own
    points (build it from the existing set before inserting the new run).

    Returns {point_id: density}, density = 1 + count of distinct OTHER run_ids
    in `index` within k_meters and travelling the same/opposite direction.

    Note: this searches `index`'s 5-tuple (run_id, point_id, lat, lon, bearing)
    entries (see build_combined_index) — a different shape from
    build_spatial_index's 4-tuples, so it can't reuse count_nearby_runs."""
    result = {}
    for pid, lat, lon, bearing in query_points:
        bx, by = _bucket(lat, lon)
        found_runs = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for run_id, _pid2, plat, plon, pbearing in index.get((bx + dx, by + dy), ()):
                    if run_id == own_run_id or run_id in found_runs:
                        continue
                    if _haversine_m(lat, lon, plat, plon) > k_meters:
                        continue
                    if bearing is not None and pbearing is not None:
                        if _orientation_diff(bearing, pbearing) > max_angle_diff:
                            continue
                    found_runs.add(run_id)
        result[pid] = 1 + len(found_runs)
    return result


def find_matches_in_index(
    subject_points: list[tuple], index: dict,
    k_meters: float = K_METERS, max_angle_diff: float = MAX_ANGLE_DIFF_DEG,
) -> set:
    """subject_points: [(point_id, lat, lon, bearing), ...] from any/all
    existing runs. `index` is typically just the newly-added run's own points
    (small, cheap to query against repeatedly) — since `index` always comes
    from a different run than any subject point by construction, no self-run
    exclusion is needed here (unlike count_nearby_runs/density_against_index).

    Returns the set of point_ids from subject_points that have at least one
    match in `index` — i.e. existing points that just gained a new nearby
    run and whose cached density should be bumped by 1."""
    matched = set()
    for pid, lat, lon, bearing in subject_points:
        bx, by = _bucket(lat, lon)
        found = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for _run_id, _pid2, plat, plon, pbearing in index.get((bx + dx, by + dy), ()):
                    if _haversine_m(lat, lon, plat, plon) > k_meters:
                        continue
                    if bearing is not None and pbearing is not None:
                        if _orientation_diff(bearing, pbearing) > max_angle_diff:
                            continue
                    matched.add(pid)
                    found = True
                    break
                if found:
                    break
            if found:
                break
    return matched


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def _normalized_density(count: int, max_count: int) -> float:
    if max_count <= 1:
        t = 0.0
    else:
        t = (count - 1) / (max_count - 1)
    return max(0.0, min(1.0, t))


def density_color(count: int, max_count: int) -> str:
    t = _normalized_density(count, max_count)

    if t <= 0.5:
        c0, c1, local_t = _GRADIENT[0], _GRADIENT[1], t / 0.5
    else:
        c0, c1, local_t = _GRADIENT[1], _GRADIENT[2], (t - 0.5) / 0.5

    r = _lerp(c0[0], c1[0], local_t)
    g = _lerp(c0[1], c1[1], local_t)
    b = _lerp(c0[2], c1[2], local_t)
    return f"#{r:02x}{g:02x}{b:02x}"


# Alternative to stippling: fade the low end of the gradient toward the map's
# own background color, instead of dithering, so a lone route visually
# recedes into the map rather than looking dotted/textured.
MAP_BG_RGB = (23, 20, 15)  # matches cards.APP_BG / the map canvas bg in main.py
FADE_FLOOR = 0.35  # below this normalized density, blend toward MAP_BG_RGB; at
                    # t=0 the line is exactly the background color (fully "gone")


def density_color_fade_to_bg(count: int, max_count: int) -> str:
    t = _normalized_density(count, max_count)

    if t < FADE_FLOOR:
        # blend from MAP_BG_RGB (t=0) up to the gradient's low-end color (t=FADE_FLOOR)
        local_t = t / FADE_FLOOR if FADE_FLOOR else 1.0
        r = _lerp(MAP_BG_RGB[0], _GRADIENT[0][0], local_t)
        g = _lerp(MAP_BG_RGB[1], _GRADIENT[0][1], local_t)
        b = _lerp(MAP_BG_RGB[2], _GRADIENT[0][2], local_t)
        return f"#{r:02x}{g:02x}{b:02x}"

    return density_color(count, max_count)


# Tk's built-in dither-pattern bitmaps, sparsest (most see-through) to densest.
# Canvas has no real alpha compositing, but stippling a line with one of these
# genuinely makes it look faded against the map, not just differently colored.
_STIPPLE_LEVELS = ["gray12", "gray25", "gray50", "gray75", ""]  # "" = fully solid

# Fixed pattern used everywhere when route_segments_with_color skips the
# per-density stipple lookup (see the "Experiment" comment below) — sparse
# enough that multiple stacked/near-overlapping strokes can visibly layer
# into something denser, rather than starting near-solid already.
UNIFORM_STIPPLE = "gray25"


def density_stipple(count: int, max_count: int) -> str:
    t = _normalized_density(count, max_count)
    idx = min(len(_STIPPLE_LEVELS) - 1, int(t * len(_STIPPLE_LEVELS)))
    return _STIPPLE_LEVELS[idx]


def route_segments_with_color(
    points: list[tuple], densities: list[int], max_count: int, mode: str = "stipple",
) -> list[dict]:
    """Chunks a run's points into short segments, each colored by the average
    density of the points in it. `points` and `densities` must be the same
    length and order (see compute_point_densities).

    mode="stipple" (default): full-saturation color + a Tk dither pattern that
    genuinely fades low-density segments against the map.
    mode="fade": no dithering; the color itself blends toward the map's
    background color at low density instead.

    Returns [{"points": [(lat, lon), ...], "color": "#rrggbb", "stipple": "grayNN"|""}, ...]
    """
    if len(points) < 2:
        return []

    segments = []
    for i in range(0, len(points) - 1, SEGMENT_CHUNK):
        chunk_pts = points[i:i + SEGMENT_CHUNK + 1]  # +1 to overlap endpoints, keep line continuous
        chunk_den = densities[i:i + SEGMENT_CHUNK + 1]
        if len(chunk_pts) < 2:
            chunk_pts = points[max(0, i - 1):i + 2]
            chunk_den = densities[max(0, i - 1):i + 2]
        if len(chunk_pts) < 2:
            continue
        avg_density = round(sum(chunk_den) / len(chunk_den))
        if mode == "fade":
            color = density_color_fade_to_bg(avg_density, max_count)
            stipple = ""
        else:
            color = density_color(avg_density, max_count)
            # Experiment: use the SAME dither pattern everywhere instead of a
            # density-computed level, and let overlapping-but-not-pixel-
            # identical GPS traces stack their stipple masks into a denser-
            # looking result on their own. Old density-driven line kept below,
            # commented out, in case this doesn't look right.
            # stipple = density_stipple(avg_density, max_count)
            stipple = UNIFORM_STIPPLE
        segments.append({"points": chunk_pts, "color": color, "stipple": stipple})
    return segments


def bounding_box(all_points: list[tuple]) -> tuple | None:
    """all_points: flat list of (lat, lon). Returns (top_left, bottom_right) or None."""
    clean = [(lat, lon) for lat, lon in all_points if lat is not None and lon is not None]
    if not clean:
        return None
    lats = [p[0] for p in clean]
    lons = [p[1] for p in clean]
    pad_lat = max((max(lats) - min(lats)) * 0.1, 0.002)
    pad_lon = max((max(lons) - min(lons)) * 0.1, 0.002)
    top_left = (max(lats) + pad_lat, min(lons) - pad_lon)
    bottom_right = (min(lats) - pad_lat, max(lons) + pad_lon)
    return top_left, bottom_right
