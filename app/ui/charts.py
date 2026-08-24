"""Small matplotlib charts embedded in Tkinter cards (dark-themed to match the map overlay)."""
import tkinter as tk
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ui import cards

FIG_BG = cards.CARD_BG
AXIS_FG = "#9c8f7c"  # warm muted gray, not stark #c8c8c8
LINE_COLOR = cards.ACCENT
BAR_COLOR = cards.SLATE  # cool counterpoint to the orange line chart, less matchy
MARKER_COLOR = cards.GOLD


def _style_axes(ax):
    ax.set_facecolor(FIG_BG)
    ax.tick_params(colors=AXIS_FG, labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#444444")
    ax.xaxis.label.set_color(AXIS_FG)
    ax.yaxis.label.set_color(AXIS_FG)
    ax.title.set_color(AXIS_FG)


def make_pace_trend_chart(parent: tk.Widget, points: list[dict]) -> tk.Widget:
    """points: [{"date": iso, "pace_sec_per_km": float}, ...] from metrics.easy_pace_trend"""
    fig = Figure(figsize=(3.2, 1.8), dpi=100, facecolor=FIG_BG)
    ax = fig.add_subplot(111)
    _style_axes(ax)
    ax.set_title("Easy pace trend", fontsize=9)

    if points:
        xs = list(range(len(points)))
        ys = [p["pace_sec_per_km"] / 60 for p in points]  # min/km
        ax.plot(xs, ys, color=LINE_COLOR, marker="o", markersize=3, linewidth=1.5,
                markerfacecolor=MARKER_COLOR, markeredgecolor=MARKER_COLOR)
        ax.set_ylabel("min/km", fontsize=7)
        ax.invert_yaxis()  # faster (lower) pace reads as "up" on the chart
    else:
        ax.text(0.5, 0.5, "No easy runs logged yet", ha="center", va="center",
                 color=AXIS_FG, fontsize=8, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])

    fig.tight_layout()
    canvas = FigureCanvasTkAgg(fig, master=parent)
    canvas.draw()
    return canvas.get_tk_widget()


def make_weekly_mileage_chart(parent: tk.Widget, weeks: list[dict]) -> tk.Widget:
    """weeks: from metrics.weekly_summary"""
    fig = Figure(figsize=(3.2, 1.8), dpi=100, facecolor=FIG_BG)
    ax = fig.add_subplot(111)
    _style_axes(ax)
    ax.set_title("Weekly distance", fontsize=9)

    if weeks:
        recent = weeks[-8:]
        xs = list(range(len(recent)))
        ys = [w["distance_km"] for w in recent]
        ax.bar(xs, ys, color=BAR_COLOR)
        ax.set_ylabel("km", fontsize=7)
    else:
        ax.text(0.5, 0.5, "No runs logged yet", ha="center", va="center",
                 color=AXIS_FG, fontsize=8, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])

    fig.tight_layout()
    canvas = FigureCanvasTkAgg(fig, master=parent)
    canvas.draw()
    return canvas.get_tk_widget()
