#!/usr/bin/env python3
"""Regenerate the PWA icons in web/ from web/logo.png (needs Pillow, dev only).

    python tools/make_icons.py

icon-192.png / icon-512.png are the logo squared on transparency; the maskable
one keeps the artwork inside the 80% safe circle on an opaque ground, and the
apple-touch icon is opaque too because iOS paints transparency black.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
GROUND = (252, 252, 252, 255)  # --background, hsl(0 0% 99%)


def squared(logo):
    """The logo centred on a transparent square canvas."""
    side = max(logo.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))
    return canvas


def on_ground(art, side, fraction):
    """The artwork, scaled to `fraction` of the canvas, on an opaque ground."""
    canvas = Image.new("RGBA", (side, side), GROUND)
    art = art.crop(art.getchannel("A").getbbox())
    art.thumbnail((round(side * fraction), round(side * fraction)), Image.LANCZOS)
    canvas.alpha_composite(art, ((side - art.width) // 2, (side - art.height) // 2))
    return canvas


def main():
    logo = Image.open(WEB / "logo.png").convert("RGBA")
    square = squared(logo)
    for size in (192, 512):
        square.resize((size, size), Image.LANCZOS).save(WEB / f"icon-{size}.png")
    # 0.72 keeps the artwork inside the 40%-radius maskable safe circle
    on_ground(square, 512, 0.72).convert("RGB").save(WEB / "icon-maskable.png")
    on_ground(square, 180, 0.82).convert("RGB").save(WEB / "icon-180.png")
    print("wrote web/icon-192.png web/icon-512.png web/icon-maskable.png web/icon-180.png")


if __name__ == "__main__":
    main()
