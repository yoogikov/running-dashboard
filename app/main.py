"""Running dashboard: a plain dark window, plus whatever modules are registered
below.

Everything the app can do lives in a module under modules/ — see module.py for
the contract and host.py for how they find each other. This file's whole job is
to make the window and say which modules are in the build. With only the root
module registered, this is a full-window grey surface and nothing else — that
is the staging model, and it is executable.
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import tkinter as tk  # noqa: E402

import db  # noqa: E402
import host  # noqa: E402
from modules import background as background_module  # noqa: E402
from modules import heatmap as heatmap_module  # noqa: E402
from modules import importer as importer_module  # noqa: E402
from modules import root as root_module  # noqa: E402
from modules import topbar as topbar_module  # noqa: E402

APP_BG = "#17140f"  # warm charcoal — visible only for the instant before the
                    # root module's grey frame covers it


def main():
    # className sets WM_CLASS, which is what i3's `assign [class="running"]`
    # rule and ~/.local/bin/running's xdotool duplicate-check both match on.
    root = tk.Tk(className="running")
    root.title("Running Dashboard")
    root.geometry("1280x820")
    root.configure(bg=APP_BG)

    db.init_db()  # creates data/running.db and its tables if they don't exist yet

    h = host.Host(root)
    h.register(root_module.Root)
    h.register(topbar_module.TopBar)
    h.register(background_module.BackgroundModule)
    h.register(heatmap_module.HeatmapTab)
    h.register(importer_module.ImporterModule)
    h.start()

    def on_close():
        h.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
