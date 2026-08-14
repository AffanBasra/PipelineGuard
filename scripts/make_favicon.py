"""Raster the PipelineGuard shield for the browser tab.

The inline SVG in `ui.py` is the master; this redraws it with Pillow because
Streamlit's `page_icon` will not take raw SVG markup, and every pure-Python SVG
rasteriser wants cairo DLLs on Windows. Kept as a script rather than a
checked-in binary with no recipe: if the mark changes, change it here too and
rerun.

    venv/Scripts/python.exe scripts/make_favicon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The SVG viewBox, so every coordinate below can be read straight off the path.
VIEW_W, VIEW_H = 44.0, 52.0
SIZE = 256
SS = 4  # supersample then downscale -- Pillow has no antialiased polygon fill

FACE_TOP, FACE_BOTTOM = (0x24, 0x35, 0x4F), (0x0D, 0x16, 0x26)
EDGE_FROM, EDGE_TO = (0x34, 0xD3, 0x99), (0x05, 0x96, 0x69)
RING = (0x10, 0xB9, 0x81)
RING_ALPHA = 84          # stroke-opacity .33
FACET_ALPHA = 13         # fill-opacity .05 on the top-right highlight

SERIF = ["georgia.ttf", "times.ttf", "constan.ttf", "DejaVuSerif.ttf"]


def cubic(p0, p1, p2, p3, steps=64):
    """Sample one cubic bezier, so the shield's curved base comes out smooth."""
    out = []
    for i in range(steps + 1):
        t, u = i / steps, 1 - i / steps
        out.append((
            u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1],
        ))
    return out


def outer() -> list[tuple[float, float]]:
    """M22 1.5 42 8.4 v15.9 c0 12.4-8.2 21.6-20 25.9 C10.2 45.9 2 36.7 2 24.3 V8.4 z"""
    pts = [(22.0, 1.5), (42.0, 8.4), (42.0, 24.3)]
    pts += cubic((42.0, 24.3), (42.0, 36.7), (33.8, 45.9), (22.0, 50.2))
    pts += cubic((22.0, 50.2), (10.2, 45.9), (2.0, 36.7), (2.0, 24.3))
    return pts + [(2.0, 8.4)]


def inner() -> list[tuple[float, float]]:
    """M22 6 37.5 11.3 v12.9 c0 9.9-6.4 17.4-15.5 21 -9.1-3.6-15.5-11.1-15.5-21 V11.3 z"""
    pts = [(22.0, 6.0), (37.5, 11.3), (37.5, 24.2)]
    pts += cubic((37.5, 24.2), (37.5, 34.1), (31.1, 41.6), (22.0, 45.2))
    pts += cubic((22.0, 45.2), (12.9, 41.6), (6.5, 34.1), (6.5, 24.2))
    return pts + [(6.5, 11.3)]


def gradient(size: tuple[int, int], start, end, diagonal: bool) -> Image.Image:
    """A linear ramp, because Pillow fills flat colour only."""
    w, h = size
    img = Image.new("RGBA", size)
    draw = ImageDraw.Draw(img)
    span = (w + h) if diagonal else h
    for i in range(span):
        t = i / max(1, span - 1)
        colour = tuple(int(a + (b - a) * t) for a, b in zip(start, end)) + (255,)
        if diagonal:                      # x1,y1 = 0,0 -> x2,y2 = 1,1
            draw.line([(i, 0), (0, i)], fill=colour)
        else:
            draw.line([(0, i), (w, i)], fill=colour)
    return img


def serif_font(px: int):
    for name in SERIF:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def build() -> Image.Image:
    side = SIZE * SS
    scale = side / VIEW_H              # fit on height; the mark is taller than wide
    dx = (side - VIEW_W * scale) / 2   # centre it in the square favicon
    px = lambda pts: [(x * scale + dx, y * scale) for x, y in pts]  # noqa: E731

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    shape, ring = px(outer()), px(inner())

    face = Image.new("L", (side, side), 0)
    ImageDraw.Draw(face).polygon(shape, fill=255)
    canvas.paste(gradient((side, side), FACE_TOP, FACE_BOTTOM, False), (0, 0), face)

    # The top-right facet, painted before the edge so the stroke sits over it.
    facet = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(facet).polygon(
        px([(22.0, 1.5), (42.0, 8.4), (42.0, 24.3), (40.6, 32.7)]),
        fill=(0xF8, 0xFA, 0xFC, FACET_ALPHA))
    canvas.alpha_composite(Image.composite(
        facet, Image.new("RGBA", (side, side), (0, 0, 0, 0)), face))

    # Edge stroke: drawn into a mask, then the gradient poured through it.
    stroke = Image.new("L", (side, side), 0)
    ImageDraw.Draw(stroke).line(shape + [shape[0]], fill=255,
                                width=round(2.6 * scale), joint="curve")
    canvas.paste(gradient((side, side), EDGE_FROM, EDGE_TO, True), (0, 0), stroke)

    inner_ring = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(inner_ring).line(ring + [ring[0]], fill=RING + (RING_ALPHA,),
                                    width=max(1, round(1.1 * scale)), joint="curve")
    canvas.alpha_composite(inner_ring)

    # The 'P', matching the SVG's Georgia 700 at font-size 25, baseline y=34.
    draw = ImageDraw.Draw(canvas)
    draw.text((22 * scale + dx, 34 * scale), "P", font=serif_font(round(25 * scale)),
              fill=RING + (255,), anchor="ms")

    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    out = (Path(__file__).resolve().parent.parent
           / "src" / "pipelineguard" / "assets" / "shield.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    build().save(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
