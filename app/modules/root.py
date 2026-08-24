"""The base root module: owns the whole window and puts a plain grey
surface behind everything else. No content of its own — it exists so
every other module has a stable, already-built parent to sit on top of,
and so the window isn't blank/uninitialized before anything else loads.
"""
import tkinter as tk

import module as mod

GREY = "#2b2b2b"


class Root(mod.Module):
    id = "root"
    surface = mod.ROOT

    def build(self, parent: tk.Tk) -> None:
        self.frame = tk.Frame(parent, bg=GREY)
        self.frame.pack(fill=tk.BOTH, expand=True)
