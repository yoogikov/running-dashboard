"""Watches data/imports/ for dropped GPX/TCX files and announces what lands.

The point of this module is the last two lines: FolderWatcher has exactly one
callback slot, which is why main.py could only ever do one thing on import.
Turning that single slot into a bus event means anything can react to a new run
— the map redrawing today, a stats panel or run list later — without this file
or folder_watch.py changing again.
"""
import folder_watch
import module
import paths

IMPORTS_FOLDER = paths.DATA_DIR / "imports"


class ImporterModule(module.Module):
    id = "imports"
    surface = module.NONE
    title = "Imports"

    def on_start(self):
        self.watcher = folder_watch.FolderWatcher(
            self.host.root, IMPORTS_FOLDER, on_import=self._on_import
        )
        self.watcher.start()

    def on_stop(self):
        self.watcher.stop()

    def _on_import(self, run_id, processed):
        # folder_watch already hops onto the main thread via root.after(0, ...)
        # before calling this, so the plain synchronous publish is correct.
        self.host.publish("run.imported", run_id=run_id, run=processed)
