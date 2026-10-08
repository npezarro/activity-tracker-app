"""Screen scaling for pixel sizes.

Windows builds declare themselves DPI-aware (crisp text), so Tk scales fonts but every
size given in pixels (window size, wrap widths, column widths, the dictation pill)
must be scaled by hand, or a 150-300 % display gets huge text in a tiny window.
macOS scales Tk apps itself, so the factor there is 1."""
import sys

SCALE = 1.0


def init(root):
    global SCALE
    if sys.platform == "darwin":
        SCALE = 1.0
        return SCALE
    try:
        SCALE = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        SCALE = 1.0
    return SCALE


def px(n):
    return int(round(n * SCALE))
