"""Watches data/imports/ for dropped GPX/TCX files and imports them automatically.

Polling-based (checks every POLL_INTERVAL_MS via Tk's .after()) rather than an
OS-level file watcher — avoids an extra dependency (watchdog) for something
where a couple seconds of latency is irrelevant. Successfully imported files
are moved to imports/imported/; files that fail to parse go to imports/failed/
so they're not silently lost and don't get retried forever. A file whose run is
already stored goes to imports/duplicates/ without being inserted — see
dedupe.py for what counts as already stored, and why re-importing matters
beyond a duplicate row in the run list.

Each detected file is smoothed (gps_smooth, purely local) before being stored
— see track_processing.process(). After insert, route_graph.add_run() folds the
new run into the merged route graph: every point either merges onto an existing
road already in the graph (raising its weight and nudging its position toward
the average) or becomes a new node. See route_graph.py for the matching rules.
Both run on a background worker thread per file, so neither ever freezes the
UI; only the on_import callback hops back onto the main thread (required,
since it touches Tkinter widgets/canvas).

Manual fields (RPE, pulse, sleep, soreness) are intentionally left blank on
auto-import — a live watcher can pick up many files at once (e.g. bulk-dropping
old exports), so popping a modal per file would be bad UX. Fill them in later
via the run list/edit UI.
"""
import shutil
import threading
from pathlib import Path

import db
import dedupe
import gpx_parser
import track_processing
import route_graph

POLL_INTERVAL_MS = 2000
WATCHED_EXTENSIONS = {".gpx", ".tcx"}


class FolderWatcher:
    def __init__(self, root, folder: Path, on_import=None):
        """
        root: the Tk root, used to schedule polling via .after() and to hop
            worker-thread results back onto the main thread
        folder: the directory to watch (imported/ and failed/ subfolders are
            created inside it and are never themselves re-scanned)
        on_import: optional callback(run_id, parsed_dict) fired on the main
            thread after each successful import, e.g. to refresh the map
        """
        self.root = root
        self.folder = Path(folder)
        self.imported_dir = self.folder / "imported"
        self.failed_dir = self.folder / "failed"
        self.duplicates_dir = self.folder / "duplicates"
        self.on_import = on_import

        self.folder.mkdir(parents=True, exist_ok=True)
        self.imported_dir.mkdir(exist_ok=True)
        self.failed_dir.mkdir(exist_ok=True)
        self.duplicates_dir.mkdir(exist_ok=True)

        self._in_flight: set[Path] = set()
        self._lock = threading.Lock()
        # Serializes the duplicate check with the insert that follows it. Files
        # are imported one worker thread each, so dropping two copies of the
        # same run together would otherwise let both check against a database
        # that contains neither yet, and both would pass.
        self._insert_lock = threading.Lock()
        self._poll_job = None

    def start(self):
        self._poll()

    def stop(self):
        """Stops the polling loop. In-flight imports are NOT waited for: the
        worker threads are daemons, an import interrupted at exit is already
        tolerated (the file simply stays in imports/ and is retried next
        launch), and joining them would block the window from closing."""
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None

    def _poll(self):
        try:
            self._scan_once()
        except Exception:
            pass  # a bad scan should never kill the polling loop
        self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll)

    def _scan_once(self):
        for path in sorted(self.folder.iterdir()):
            if not path.is_file():
                continue  # skips imported/, failed/ and duplicates/ subfolders themselves
            if path.suffix.lower() not in WATCHED_EXTENSIONS:
                continue
            with self._lock:
                if path in self._in_flight:
                    continue  # already being processed by a worker thread
                self._in_flight.add(path)
            threading.Thread(target=self._import_file, args=(path,), daemon=True).start()

    def _import_file(self, path: Path):
        """Runs on a background thread: parsing is cheap, but the density-
        cache update is not (~1-2s against a large history), so this must
        not run on the main/UI thread."""
        try:
            parsed = gpx_parser.parse_file(path)

            if parsed["distance_km"] <= 0 or parsed["duration_sec"] <= 0:
                self._move_unique(path, self.failed_dir)
                return

            processed = track_processing.process(parsed)

            with self._insert_lock:
                if dedupe.find_duplicate(processed) is not None:
                    self._move_unique(path, self.duplicates_dir)
                    return
                run_id = db.insert_run(processed, processed.get("track_points"))
            # Outside the lock: folding a run into the merged route graph takes
            # a second or two, and nothing about it affects duplicate detection.
            route_graph.add_run(run_id)
            self._move_unique(path, self.imported_dir)

            callback = self.on_import
            if callback:
                self.root.after(0, lambda: callback(run_id, processed))
        except Exception:
            try:
                self._move_unique(path, self.failed_dir)
            except Exception:
                pass  # file may have already been moved/removed; nothing more to do
        finally:
            with self._lock:
                self._in_flight.discard(path)

    @staticmethod
    def _move_unique(path: Path, dest_dir: Path):
        """Moves path into dest_dir, disambiguating if a same-named file is
        already there (e.g. re-dropping a file with the same export name)."""
        target = dest_dir / path.name
        if target.exists():
            stem, suffix = path.stem, path.suffix
            n = 1
            while target.exists():
                target = dest_dir / f"{stem}_{n}{suffix}"
                n += 1
        shutil.move(str(path), str(target))
