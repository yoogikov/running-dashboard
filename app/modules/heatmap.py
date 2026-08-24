"""The heatmap tab: just the tab in the top bar for now. No panel, no
click behavior, no heatmap — those come once there's something to
switch to.

surface=NONE because TAB isn't implemented (see module.py); it packs
itself into the topbar module's frame directly, the same way topbar
builds onto root's.
"""
import tkinter as tk

import module as mod

BG = "#2a2a2a"       # a touch lighter than the bar, so the tab reads as
                     # a distinct control sitting on it
FG = "#cfcfcf"
PADX = 14
PADY = 8


class HeatmapTab(mod.Module):
    id = "heatmap"
    requires = ("topbar",)
    surface = mod.NONE

    def on_deps(self, deps: dict) -> None:
        self.topbar = deps["topbar"]

    def build(self, parent) -> None:
        self.label = tk.Label(
            self.topbar.frame, text="Heatmap", bg=BG, fg=FG,
            padx=PADX, pady=PADY, cursor="hand2",
        )
        self.label.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), pady=4)
