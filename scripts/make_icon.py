"""Draw the app icon: assets/icon.png (1024 px) and assets/icon.ico. CI builds the .icns."""
import os

from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(__file__), "..")
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=(28, 36, 48, 255))
# a timeline: bars of different lengths, the last one "live"
bars = [(0.30, (90, 120, 150)), (0.55, (90, 120, 150)), (0.42, (90, 120, 150)), (0.70, (46, 160, 67))]
y = 250
for frac, col in bars:
    d.rounded_rectangle((200, y, 200 + int((S - 400) * frac), y + 90), radius=45, fill=col + (255,))
    y += 140
d.ellipse((S - 330, S - 330, S - 190, S - 190), fill=(46, 160, 67, 255))
os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
img.save(os.path.join(ROOT, "assets", "icon.png"))
img.save(os.path.join(ROOT, "assets", "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote assets/icon.png and assets/icon.ico")
