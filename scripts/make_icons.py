#!/usr/bin/env python3
"""Generate the PWA icons (red field, white ball). Run by the workflow; needs Pillow."""
from pathlib import Path
from PIL import Image, ImageDraw

DOCS = Path(__file__).resolve().parent.parent / "docs"
for s in (192, 512):
    im = Image.new("RGBA", (s, s), "#E4002B")
    d = ImageDraw.Draw(im)
    for y in (0.31, 0.5, 0.69):
        d.line([(s * .12, s * y), (s * .88, s * y)], fill=(255, 255, 255, 90), width=max(2, s // 96))
    d.ellipse([s * 0.22, s * 0.34, s * 0.78, s * 0.66], fill="#FFFFFF")
    for k in (-.06, 0, .06):
        d.line([(s * (.44 + k), s * (.42 + k)), (s * (.56 + k), s * (.54 + k))], fill="#E4002B", width=max(2, s // 64))
    im.save(DOCS / f"icon-{s}.png")
print("icons ok")
