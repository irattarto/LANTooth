"""
Regenerate the Android launcher bitmaps from assets/lantooth_icon.png.

  python assets/make_icons.py

Writes android/app/src/main/res/mipmap-*/ic_launcher_foreground.png — the
foreground layer of the adaptive icon (108dp canvas). The artwork is scaled to
a 64dp square in the centre so launcher masks (circle, squircle, ...) crop only
its outer glow. The PC icons (.ico, tray, window) are generated from the same PNG at
build/run time — see pc/lantooth.spec and pc/gui.py.
"""

import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "lantooth_icon.png")
RES = os.path.join(HERE, "..", "android", "app", "src", "main", "res")

CANVAS_DP = 108
ART_DP = 64
DENSITIES = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}


def main() -> None:
    art = Image.open(SRC).convert("RGBA")
    art = art.crop(art.getbbox())  # trim transparent margins
    for name, scale in DENSITIES.items():
        canvas_px = round(CANVAS_DP * scale)
        art_px = round(ART_DP * scale)
        a = art.copy()
        a.thumbnail((art_px, art_px), Image.LANCZOS)
        out = Image.new("RGBA", (canvas_px, canvas_px), (0, 0, 0, 0))
        out.alpha_composite(a, ((canvas_px - a.width) // 2, (canvas_px - a.height) // 2))
        d = os.path.join(RES, f"mipmap-{name}")
        os.makedirs(d, exist_ok=True)
        out.save(os.path.join(d, "ic_launcher_foreground.png"), optimize=True)
        print(f"{name}: {canvas_px}px")


if __name__ == "__main__":
    main()
