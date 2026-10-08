"""Tray / menu-bar icons, drawn at run time so no image files are needed for them."""
from PIL import Image, ImageDraw

COLORS = {"recording": (46, 160, 67), "paused": (150, 150, 150), "problem": (210, 120, 30)}


def tray_image(state="recording", size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = COLORS.get(state, COLORS["recording"])
    pad = size // 8
    d.ellipse((pad, pad, size - pad, size - pad), outline=c + (255,), width=max(2, size // 10))
    if state == "paused":
        w = size // 9
        x0 = size // 2 - w * 2
        d.rectangle((x0, size * 0.32, x0 + w, size * 0.68), fill=c + (255,))
        d.rectangle((x0 + w * 3, size * 0.32, x0 + w * 4, size * 0.68), fill=c + (255,))
    else:
        r = size // 6
        d.ellipse((size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r), fill=c + (255,))
    return img
