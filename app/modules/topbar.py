"""The top bar: a fixed-height strip across the top of the window, where
tab/nav buttons will eventually live. Empty for now — no buttons yet.

surface=NONE because TAB isn't implemented (see module.py); it builds
itself directly onto the root module's frame instead of waiting for a
surface kind that doesn't exist yet.
"""
import tkinter as tk

import module as mod

BG = "#1f1f1f"   # slightly darker than the root grey, so the strip reads
                 # as a distinct bar rather than blending into the window
HEIGHT = 40


class TopBar(mod.Module):
    id = "topbar"
    requires = ("root",)
    surface = mod.NONE

    def on_deps(self, deps: dict) -> None:
        self.root_mod = deps["root"]

    def build(self, parent) -> None:
        self.frame = tk.Frame(self.root_mod.frame, bg=BG, height=HEIGHT)
        self.frame.pack(side=tk.TOP, fill=tk.X)
        self.frame.pack_propagate(False)  # keep the height even with nothing in it
