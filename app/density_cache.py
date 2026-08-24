"""Keeps each track point's cached heatmap density (db.track_points.nearby_count)
up to date, incrementally.

The insight: two runs that have never been compared against each other only
need comparing once — that relationship never changes afterward. So instead
of recomputing every point's density from scratch on every launch/import
(map_heat.compute_point_densities on a real multi-thousand-point history
takes tens of seconds — see the module docstring there), adding one run only
requires:
  1. computing that new run's own density against the existing set (cheap:
     few new points x candidates from the existing index), and
  2. checking which EXISTING points just gained a new neighbor from the new
     run (cheap: many existing points x candidates from the new run's own,
     much smaller index) and bumping their cached count by 1.
Existing runs are never re-compared against each other.

backfill_all() rebuilds the cache from scratch. It does NOT just call add_run()
per run in a loop — every run already sits in the DB for a historical
backfill (unlike a real new import), so add_run()'s "existing = everything
else in the DB" query would treat every step as a full comparison against
the whole history regardless of chronological order, which is exactly the
slow all-at-once computation this module exists to avoid. Instead it builds
up the "existing" set in memory, run by run, in chronological order — the
same core step, just fed data it already has rather than re-querying the DB
each time.
"""
import db
import map_heat


def _incremental_step(new_run_id, new_points: list[tuple], existing_indexed_by_run: dict):
    """Pure computation, no DB access: given one run's raw (point_id, lat, lon)
    points and an already-built {run_id: indexed_points} for everything it
    should be compared against, returns:
      new_indexed: this run's own indexed points (for the caller to fold into
        a growing existing set, e.g. during backfill)
      new_run_counts: {point_id: density} for the new run's own points
      bumped: set of existing point_ids that gained a new neighbor
    """
    new_indexed = map_heat.build_indexed_points(new_points)

    if not existing_indexed_by_run:
        return new_indexed, {pid: 1 for pid, _, _, _ in new_indexed}, set()

    existing_index = map_heat.build_combined_index(existing_indexed_by_run)
    new_run_counts = map_heat.density_against_index(new_indexed, new_run_id, existing_index)

    new_run_index = map_heat.build_combined_index({new_run_id: new_indexed})
    all_existing_indexed = [p for pts in existing_indexed_by_run.values() for p in pts]
    bumped = map_heat.find_matches_in_index(all_existing_indexed, new_run_index)

    return new_indexed, new_run_counts, bumped


def add_run(run_id: int):
    """Incrementally updates the density cache for a newly-inserted run.
    Call this once, right after the run (and its track points) exist in the
    DB. Safe to call on a run with zero existing points elsewhere (density
    for everything stays 1)."""
    new_points = db.get_points_for_density(run_id)
    if not new_points:
        return

    existing_by_run = db.get_all_points_for_density_except(exclude_run_id=run_id)
    existing_indexed_by_run = {
        rid: map_heat.build_indexed_points(pts) for rid, pts in existing_by_run.items()
    }

    _, new_run_counts, bumped = _incremental_step(run_id, new_points, existing_indexed_by_run)
    db.set_nearby_counts(new_run_counts)
    db.bump_nearby_counts(bumped, delta=1)


def backfill_all():
    """Rebuilds the cache from scratch by replaying every run in chronological
    order against an in-memory 'existing set so far' — needed once for runs
    imported before this cache existed. Writes incrementally per run (not
    all at the end), so a partial run leaves partial progress rather than
    none."""
    runs = [dict(r) for r in db.get_all_runs()]  # ordered by date ASC

    with db.get_conn() as conn:
        conn.execute("UPDATE track_points SET nearby_count = 1")

    processed_indexed_by_run: dict = {}
    for r in runs:
        run_id = r["id"]
        points = db.get_points_for_density(run_id)
        if not points:
            continue

        new_indexed, new_run_counts, bumped = _incremental_step(
            run_id, points, processed_indexed_by_run
        )
        db.set_nearby_counts(new_run_counts)
        db.bump_nearby_counts(bumped, delta=1)

        processed_indexed_by_run[run_id] = new_indexed
