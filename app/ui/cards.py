"""Floating 'card' panels overlaid on top of the full-bleed map widget."""
import tkinter as tk
from typing import Literal

# Strava-inspired but softened: warm charcoal base (not pure black), desaturated
# orange/gold accents (not saturated Strava orange), plus a muted slate-blue and
# sage/brick for status colors — a wider, lower-contrast palette than pure black/
# #fc4c02/white while keeping the same identity.
APP_BG = "#17140f"       # warm charcoal, app/canvas background
CARD_BG = "#1e1a15"      # tk Frames have no alpha channel, so cards are solid warm panels
CARD_FG = "#d8cdbe"      # warm off-white body text, not stark white
CARD_MUTED = "#b89a5e"   # muted gold for section labels
# A neutral, barely-there lift off CARD_BG — not an accent color. Accent color on
# every card's outline read as a hard box; the border's only job now is to keep
# a card legible against the map, not to draw the eye.
CARD_BORDER = "#332c22"
ACCENT = "#c96f3c"       # desaturated terracotta-orange (primary accent/buttons)
ACCENT_HOVER = "#d99a5c"
GOLD = "#c9a35a"         # muted gold (was a bright #ffc72c)
SLATE = "#5f7a82"        # cool complementary accent (secondary charts, "once-run" heat tier)
SAGE = "#7d9670"         # muted green — readiness "progress"
AMBER = "#c9973f"        # muted amber — readiness "hold"
BRICK = "#b25b4e"        # muted brick red — readiness "rest"


Anchor = Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"]


def make_card(parent, relx, rely, relwidth, relheight, anchor: Anchor = "nw") -> tk.Frame:
    """Creates a floating card frame placed proportionally over the parent (the map)."""
    outer = tk.Frame(parent, bg=CARD_BORDER, bd=0)
    outer.place(relx=relx, rely=rely, relwidth=relwidth, relheight=relheight, anchor=anchor)

    inner = tk.Frame(outer, bg=CARD_BG)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return inner


def card_title(card: tk.Frame, text: str) -> tk.Label:
    lbl = tk.Label(card, text=text, bg=CARD_BG, fg=CARD_MUTED,
                    font=("TkDefaultFont", 9, "bold"), anchor="w")
    lbl.pack(fill="x", padx=10, pady=(8, 2))
    return lbl
