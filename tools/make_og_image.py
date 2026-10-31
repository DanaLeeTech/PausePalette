#!/usr/bin/env python3
"""Regenerate the link-preview image in web/ from web/logo.png (needs Pillow, dev only).

    python tools/make_og_image.py

og-image.png is the logo centred on the app's light background at the 1200x630
that og:image and twitter:summary_large_image expect. No wordmark: the site
fonts come from a CDN, so there is no matching TTF to draw one with.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
GROUND = (252, 252, 252, 255)  # --background, hsl(0 0% 99%)
CANVAS = (1200, 630)
LOGO_WIDTH = 400


def main():
    logo = Image.open(WEB / "logo.png").convert("RGBA")
    logo = logo.crop(logo.getchannel("A").getbbox())
    logo = logo.resize((LOGO_WIDTH, round(logo.height * LOGO_WIDTH / logo.width)), Image.LANCZOS)
    canvas = Image.new("RGBA", CANVAS, GROUND)
    canvas.alpha_composite(logo, ((CANVAS[0] - logo.width) // 2, (CANVAS[1] - logo.height) // 2))
    canvas.convert("RGB").save(WEB / "og-image.png")
    print("wrote web/og-image.png")


if __name__ == "__main__":
    main()
