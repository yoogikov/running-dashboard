"""The heatmap tab. Clicking it shows the background module's map — the
merged route graph rendered as a heat-colored overlay (see
modules/background.py and heat_overlay.py) — instead of an empty
placeholder.

surface=NONE because TAB isn't implemented (see module.py); the tab
packs into the topbar module's frame the same way topbar builds onto
root's.
"""
import tkinter as tk

import module as mod

TAB_BG = "#2a2a2a"    # a touch lighter than the bar, so the tab reads as
                      # a distinct control sitting on it
TAB_FG = "#cfcfcf"
TAB_PADX = 14
TAB_PADY = 8


class HeatmapTab(mod.Module):
    id = "heatmap"
    requires = ("topbar", "background")
    surface = mod.NONE

    def on_deps(self, deps: dict) -> None:
        self.topbar = deps["topbar"]
        self.background = deps["background"]

    def build(self, parent) -> None:
        self.label = tk.Label(
            self.topbar.frame, text="Heatmap", bg=TAB_BG, fg=TAB_FG,
            padx=TAB_PADX, pady=TAB_PADY, cursor="hand2",
        )
        self.label.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), pady=4)
        self.label.bind("<Button-1>", self._on_click)

    def _on_click(self, _event) -> None:
        self.background.show()
        self.topbar.frame.lift()  # the bar stays on top of whatever view is open
