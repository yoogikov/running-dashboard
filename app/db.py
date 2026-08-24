"""SQLite storage layer for the running dashboard."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "running.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,              -- ISO date, e.g. 2026-08-15
    source_file TEXT,                -- original GPX/TCX filename, if imported
    name TEXT,                       -- user-assigned label, set/renamed from Run Analyzer
    distance_km REAL NOT NULL,
    duration_sec INTEGER NOT NULL,
    avg_pace_sec_per_km REAL,        -- derived: duration_sec / distance_km
    elevation_gain_m REAL,
    -- manual / subjective fields
    rpe INTEGER,                     -- 1-10
    morning_pulse INTEGER,           -- bpm, manually counted
    sleep_hours REAL,
    sleep_quality INTEGER,           -- 1-5
    soreness_notes TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS track_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,            -- ordering within the run
    lat REAL,
    lon REAL,
    elevation_m REAL,
    time TEXT,                       -- ISO timestamp, if present
    nearby_count INTEGER NOT NULL DEFAULT 1  -- cached heatmap density; see density_cache.py
);

CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(date);
CREATE INDEX IF NOT EXISTS idx_track_points_run ON track_points(run_id);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """CREATE TABLE IF NOT EXISTS won't add new columns to an already-existing
    table, so schema additions to track_points/runs need an explicit migration."""
    tp_cols = {row[1] for row in conn.execute("PRAGMA table_info(track_points)").fetchall()}
    if "nearby_count" not in tp_cols:
        conn.execute("ALTER TABLE track_points ADD COLUMN nearby_count INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "name" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN name TEXT")
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_run(run: dict, track_points: list[dict] | None = None) -> int | None:
    """Insert a run and its optional track points. Returns new run id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO runs
               (date, source_file, distance_km, duration_sec, avg_pace_sec_per_km,
                elevation_gain_m, rpe, morning_pulse, sleep_hours, sleep_quality,
                soreness_notes, notes)
               VALUES (:date, :source_file, :distance_km, :duration_sec,
                       :avg_pace_sec_per_km, :elevation_gain_m, :rpe, :morning_pulse,
                       :sleep_hours, :sleep_quality, :soreness_notes, :notes)""",
            {
                "date": run["date"],
                "source_file": run.get("source_file"),
                "distance_km": run["distance_km"],
                "duration_sec": run["duration_sec"],
                "avg_pace_sec_per_km": run.get("avg_pace_sec_per_km"),
                "elevation_gain_m": run.get("elevation_gain_m"),
                "rpe": run.get("rpe"),
                "morning_pulse": run.get("morning_pulse"),
                "sleep_hours": run.get("sleep_hours"),
                "sleep_quality": run.get("sleep_quality"),
                "soreness_notes": run.get("soreness_notes"),
                "notes": run.get("notes"),
            },
        )
        run_id = cur.lastrowid
        if track_points:
            conn.executemany(
                """INSERT INTO track_points (run_id, seq, lat, lon, elevation_m, time)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (run_id, i, p.get("lat"), p.get("lon"), p.get("elevation_m"), p.get("time"))
                    for i, p in enumerate(track_points)
                ],
            )
        return run_id


def update_run_fields(run_id: int, fields: dict):
    """Update a subset of a run's columns (used for manual field edits)."""
    if not fields:
        return
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    fields = dict(fields, id=run_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = :id", fields)


def delete_run(run_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))


def get_run_identities() -> list[sqlite3.Row]:
    """(id, date, distance_km, duration_sec, start_time) for every stored run,
    for duplicate detection on import — see dedupe.py.

    start_time is the earliest track-point timestamp, or NULL for runs with no
    timed points (treadmill activities, exports with <time> stripped). MIN() on
    the ISO strings is a lexicographic minimum, which is the true earliest only
    because every exporter here writes a single UTC "...Z" offset per file; it
    would be wrong for a track mixing offsets, which none of these do.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.id, r.date, r.distance_km, r.duration_sec,
                      (SELECT MIN(tp.time) FROM track_points tp
                        WHERE tp.run_id = r.id AND tp.time IS NOT NULL
                          AND tp.time != '') AS start_time
                 FROM runs r"""
        ).fetchall()


def get_run_start_points() -> list[tuple]:
    """[(run_id, lat, lon), ...] — the first located reading of each run.

    Enough to say which part of the world a run belongs to, without reading all
    ~95k track points, which is what route_graph._clusters needs it for.
    """
    with get_conn() as conn:
        return [(r["run_id"], r["lat"], r["lon"]) for r in conn.execute(
            """SELECT tp.run_id, tp.lat, tp.lon FROM track_points tp
                 JOIN (SELECT run_id, MIN(seq) AS s FROM track_points
                        WHERE lat IS NOT NULL GROUP BY run_id) f
                   ON f.run_id = tp.run_id AND f.s = tp.seq"""
        ).fetchall()]


def get_all_runs() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM runs ORDER BY date ASC").fetchall()


def get_run(run_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def get_track_points(run_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM track_points WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()


# ---------- density cache accessors (see density_cache.py) ----------

def get_points_for_density(run_id: int) -> list[tuple]:
    """[(point_id, lat, lon), ...] for one run, in seq order — what
    map_heat.build_indexed_points needs to compute bearings correctly."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, lat, lon FROM track_points WHERE run_id = ? AND lat IS NOT NULL ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
        return [(r["id"], r["lat"], r["lon"]) for r in rows]


def get_points_for_graph(run_id: int) -> list[tuple]:
    """[(lat, lon, time), ...] for one run, in seq order — what route_graph needs.

    Same rows as get_points_for_density but carrying the timestamp instead of the
    point id, because the graph builder has to be able to tell a one-second step
    along a road from a step that spans a paused recording. See route_graph's
    GAP_MAX_SEC. time is the raw ISO string or None; parsing is the caller's job.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT lat, lon, time FROM track_points WHERE run_id = ? AND lat IS NOT NULL ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
        return [(r["lat"], r["lon"], r["time"]) for r in rows]


def get_all_points_for_density_except(exclude_run_id: int | None = None) -> dict:
    """{run_id: [(point_id, lat, lon), ...]} for every run except
    exclude_run_id, each in seq order. Used to build the 'existing points'
    index/subject-list for an incremental density update."""
    with get_conn() as conn:
        if exclude_run_id is None:
            rows = conn.execute(
                "SELECT id, run_id, lat, lon FROM track_points WHERE lat IS NOT NULL ORDER BY run_id, seq ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, run_id, lat, lon FROM track_points "
                "WHERE lat IS NOT NULL AND run_id != ? ORDER BY run_id, seq ASC",
                (exclude_run_id,),
            ).fetchall()
        result: dict = {}
        for r in rows:
            result.setdefault(r["run_id"], []).append((r["id"], r["lat"], r["lon"]))
        return result


def set_nearby_counts(counts: dict):
    """counts: {point_id: nearby_count}. Bulk-sets the cached density value."""
    if not counts:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE track_points SET nearby_count = ? WHERE id = ?",
            [(count, pid) for pid, count in counts.items()],
        )


def bump_nearby_counts(point_ids, delta: int = 1):
    """Increments nearby_count by delta for the given track_point ids."""
    point_ids = list(point_ids)
    if not point_ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE track_points SET nearby_count = nearby_count + ? WHERE id = ?",
            [(delta, pid) for pid in point_ids],
        )


def get_track_points_with_density(run_id: int) -> list[sqlite3.Row]:
    """(lat, lon, nearby_count) in seq order — what the map overlay needs to render."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT lat, lon, nearby_count FROM track_points "
            "WHERE run_id = ? AND lat IS NOT NULL ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
