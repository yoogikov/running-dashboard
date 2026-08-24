"""Persistent, size-capped SQLite cache for map tiles fetched by tkintermapview.

tkintermapview's `database_path` option only *reads* from an existing tiles
table — it's designed for pre-downloaded offline regions via its OfflineLoader
helper, and never writes tiles fetched during normal browsing back to disk.
Left as-is, every tile is re-fetched from the network on every launch. This
module patches in write-through caching (every fetched tile gets saved) plus
a size cap: once the cache exceeds `max_bytes`, the least-recently-used tiles
are evicted first, so the file never grows without bound.
"""
import io
import sqlite3
import time
import types

import requests
from PIL import Image, ImageTk

DEFAULT_MAX_BYTES = 300 * 1024 * 1024  # 300 MB


def init_cache_db(path: str):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")  # reduces lock contention across the 25 tile-fetch threads
    conn.execute("PRAGMA busy_timeout=3000;")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tiles (
               zoom INTEGER NOT NULL,
               x INTEGER NOT NULL,
               y INTEGER NOT NULL,
               server VARCHAR(300) NOT NULL,
               tile_image BLOB NOT NULL,
               last_used INTEGER NOT NULL,
               PRIMARY KEY (zoom, x, y, server)
           );"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_last_used ON tiles(last_used);")
    conn.commit()
    conn.close()


def cache_size_bytes(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT COALESCE(SUM(LENGTH(tile_image)), 0) FROM tiles").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def prune_cache(path: str, max_bytes: int = DEFAULT_MAX_BYTES):
    """Deletes the least-recently-used tiles until the cache is back under max_bytes."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=3000;")
    try:
        total = conn.execute("SELECT COALESCE(SUM(LENGTH(tile_image)), 0) FROM tiles").fetchone()[0]
        if total <= max_bytes:
            return

        rows = conn.execute("SELECT rowid, LENGTH(tile_image) FROM tiles ORDER BY last_used ASC").fetchall()
        to_delete = []
        for rowid, size in rows:
            if total <= max_bytes:
                break
            to_delete.append((rowid,))
            total -= size

        if to_delete:
            conn.executemany("DELETE FROM tiles WHERE rowid = ?", to_delete)
            conn.commit()
            conn.execute("VACUUM;")
    finally:
        conn.close()


def _cached_request_image(self, zoom: int, x: int, y: int, db_cursor=None):
    """Drop-in replacement for TkinterMapView.request_image (same read path as
    the original) that additionally writes freshly-fetched tiles back to the
    cache database with a last_used timestamp, so eviction can be LRU-based.
    """
    if db_cursor is not None:
        try:
            db_cursor.execute(
                "SELECT t.tile_image FROM tiles t WHERE t.zoom=? AND t.x=? AND t.y=? AND t.server=?;",
                (zoom, x, y, self.tile_server),
            )
            result = db_cursor.fetchone()
            if result is not None:
                image = Image.open(io.BytesIO(result[0]))
                image_tk = ImageTk.PhotoImage(image)
                self.tile_image_cache[f"{zoom}{x}{y}"] = image_tk
                # best-effort LRU touch; a failed/slow update never blocks the tile render
                try:
                    db_cursor.execute(
                        "UPDATE tiles SET last_used=? WHERE zoom=? AND x=? AND y=? AND server=?;",
                        (int(time.time()), zoom, x, y, self.tile_server),
                    )
                    db_cursor.connection.commit()
                except Exception:
                    pass
                return image_tk
            elif self.use_database_only:
                return self.empty_tile_image
        except sqlite3.OperationalError:
            if self.use_database_only:
                return self.empty_tile_image
        except Exception:
            return self.empty_tile_image

    try:
        url = self.tile_server.replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(zoom))
        response = requests.get(url, headers={"User-Agent": "TkinterMapView"}, timeout=10)
        content = response.content
        image = Image.open(io.BytesIO(content))

        if not self.running:
            return self.empty_tile_image
        image_tk = ImageTk.PhotoImage(image)
        self.tile_image_cache[f"{zoom}{x}{y}"] = image_tk

        if db_cursor is not None:
            try:
                db_cursor.execute(
                    "INSERT OR REPLACE INTO tiles (zoom, x, y, server, tile_image, last_used) "
                    "VALUES (?, ?, ?, ?, ?, ?);",
                    (zoom, x, y, self.tile_server, content, int(time.time())),
                )
                db_cursor.connection.commit()
            except Exception:
                pass  # caching is best-effort; never let it break tile rendering

        return image_tk

    except Exception:
        self.tile_image_cache[f"{zoom}{x}{y}"] = self.empty_tile_image
        return self.empty_tile_image


def enable_write_through_cache(map_widget, db_path: str, max_bytes: int = DEFAULT_MAX_BYTES):
    """Wires up persistent, size-capped tile caching on an already-constructed
    TkinterMapView. Call once, right after construction and before any panning.
    """
    init_cache_db(db_path)
    prune_cache(db_path, max_bytes)  # trim any pre-existing cache before new writes start
    map_widget.request_image = types.MethodType(_cached_request_image, map_widget)
