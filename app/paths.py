"""Where things live on disk. Split out so modules can find data/ without each
one recomputing it from its own __file__ (modules/ is a directory deeper than
app/, so the old `Path(__file__).parent.parent` idiom would land in the wrong
place)."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR.parent / "data"
