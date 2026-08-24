"""Modal dialogs: GPX/TCX file picker and the post-import manual-fields form."""
import tkinter as tk
from tkinter import filedialog, ttk


def pick_gpx_file(parent) -> str | None:
    path = filedialog.askopenfilename(
        parent=parent,
        title="Import run",
        filetypes=[("GPX/TCX files", "*.gpx *.tcx"), ("GPX files", "*.gpx"),
                   ("TCX files", "*.tcx"), ("All files", "*.*")],
    )
    return path or None


def ask_manual_fields(parent, run_summary: dict) -> dict | None:
    """Modal popup shown right after import. Returns a dict of manual fields, or None if skipped."""
    result = {}

    win = tk.Toplevel(parent)
    win.title("Run details")
    win.transient(parent)
    win.grab_set()
    win.resizable(False, False)

    padx, pady = 12, 6

    def grid_row(widget, r, col, sticky):
        widget.grid(row=r, column=col, sticky=sticky, padx=padx, pady=pady)

    header = tk.Label(
        win,
        text=f"{run_summary['date']}  •  {run_summary['distance_km']:.2f} km  •  "
             f"{run_summary['duration_sec'] // 60} min",
        font=("TkDefaultFont", 10, "bold"),
    )
    header.grid(row=0, column=0, columnspan=2, sticky="w", padx=padx, pady=pady)

    row = 1
    grid_row(tk.Label(win, text="RPE (1-10, effort felt)"), row, 0, "w")
    rpe_var = tk.IntVar(value=4)
    grid_row(tk.Spinbox(win, from_=1, to=10, textvariable=rpe_var, width=6), row, 1, "w")
    row += 1

    grid_row(tk.Label(win, text="Morning pulse (bpm)"), row, 0, "w")
    pulse_var = tk.StringVar()
    grid_row(tk.Entry(win, textvariable=pulse_var, width=8), row, 1, "w")
    row += 1

    grid_row(tk.Label(win, text="Sleep (hours)"), row, 0, "w")
    sleep_hours_var = tk.StringVar()
    grid_row(tk.Entry(win, textvariable=sleep_hours_var, width=8), row, 1, "w")
    row += 1

    grid_row(tk.Label(win, text="Sleep quality (1-5)"), row, 0, "w")
    sleep_quality_var = tk.IntVar(value=3)
    grid_row(tk.Spinbox(win, from_=1, to=5, textvariable=sleep_quality_var, width=6), row, 1, "w")
    row += 1

    grid_row(tk.Label(win, text="Soreness / pain notes"), row, 0, "nw")
    soreness_text = tk.Text(win, width=28, height=3)
    grid_row(soreness_text, row, 1, "w")
    row += 1

    grid_row(tk.Label(win, text="Notes"), row, 0, "nw")
    notes_text = tk.Text(win, width=28, height=3)
    grid_row(notes_text, row, 1, "w")
    row += 1

    def on_save():
        def parse_float(s):
            try:
                return float(s)
            except (TypeError, ValueError):
                return None

        def parse_int(s):
            try:
                return int(s)
            except (TypeError, ValueError):
                return None

        result["rpe"] = rpe_var.get()
        result["morning_pulse"] = parse_int(pulse_var.get())
        result["sleep_hours"] = parse_float(sleep_hours_var.get())
        result["sleep_quality"] = sleep_quality_var.get()
        result["soreness_notes"] = soreness_text.get("1.0", "end").strip()
        result["notes"] = notes_text.get("1.0", "end").strip()
        win.destroy()

    def on_skip():
        result.clear()
        win.destroy()

    btn_frame = tk.Frame(win)
    btn_frame.grid(row=row, column=0, columnspan=2, pady=(4, 12))
    ttk.Button(btn_frame, text="Skip for now", command=on_skip).pack(side="left", padx=6)
    ttk.Button(btn_frame, text="Save", command=on_save).pack(side="left", padx=6)

    win.wait_window()
    return result if result else None
