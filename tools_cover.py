# MIT License
# Copyright (c) 2026 Heinrich Munz
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""The submission cover image, drawn from the account's own curve.

A cover made of stock artwork would say nothing about the project. This
one is the real equity and balance series out of docs/snapshot.json - the
same file the live sheet reads - so the picture on the submission is the
picture of the account. If the snapshot cannot be read the run FAILS
rather than falling back to a decorative curve: a fabricated line on the
cover of a project whose whole argument is measurement would be the worst
possible thing to ship.

Usage:
    python tools_cover.py --snapshot docs/snapshot.json --out docs/cover.png
"""

import argparse
import datetime as dt
import json
import sys

from PIL import Image, ImageDraw, ImageFont

# The dark palette of the live sheet, so the cover and the page it links
# to are recognisably one thing (perf_page.tpl.html, dark block).
GROUND = (12, 15, 20)
SURFACE = (20, 25, 34)
INK = (232, 236, 242)
MUTED = (138, 148, 165)
RULE = (36, 44, 56)
ACCENT = (138, 166, 239)
EQUITY = (46, 95, 191)
EQUITY_LIT = (110, 150, 235)
BALANCE = (194, 118, 26)
GAIN = (15, 168, 140)
LOSS = (224, 106, 78)

FONTS = "C:/Windows/Fonts/"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONTS + name, size)


def series(curve: list[dict], key: str) -> list[float]:
    return [float(p[key]) for p in curve]


def build(snapshot: dict, width: int = 1200, height: int = 630) -> Image.Image:
    img = Image.new("RGB", (width, height), GROUND)
    d = ImageDraw.Draw(img)

    curve = snapshot["curve"]
    if len(curve) < 2:
        raise SystemExit(f"curve has {len(curve)} points - nothing to draw")

    # --- the chart, bled across the lower two thirds --------------------
    top, bottom = 300, height - 96
    left, right = 0, width
    eq = series(curve, "equity")
    bal = series(curve, "balance")
    lo = min(min(eq), min(bal))
    hi = max(max(eq), max(bal))
    span = (hi - lo) or 1.0
    # A little headroom so neither line touches an edge.
    lo, hi = lo - span * 0.12, hi + span * 0.12
    span = hi - lo

    def xy(values):
        n = len(values) - 1
        return [(left + (right - left) * i / n,
                 bottom - (v - lo) / span * (bottom - top))
                for i, v in enumerate(values)]

    start = float(snapshot["start_equity"])
    y_start = bottom - (start - lo) / span * (bottom - top)
    d.line([(0, y_start), (width, y_start)], fill=RULE, width=2)

    eq_pts = xy(eq)
    # The area under equity, so the line reads as a body rather than a wire.
    d.polygon([(0, bottom)] + eq_pts + [(width, bottom)], fill=(17, 24, 38))
    d.line(xy(bal), fill=BALANCE, width=3, joint="curve")
    d.line(eq_pts, fill=EQUITY_LIT, width=4, joint="curve")
    d.ellipse([eq_pts[-1][0] - 7, eq_pts[-1][1] - 7,
               eq_pts[-1][0] + 7, eq_pts[-1][1] + 7], fill=EQUITY_LIT)

    # --- the header, on a solid band so type never sits on the curve ----
    d.rectangle([0, 0, width, 300], fill=GROUND)
    d.line([(0, 300), (width, 300)], fill=RULE, width=1)

    d.text((64, 58), "KANGAROO OPTIONS", font=font("georgiab.ttf", 62),
           fill=INK)
    d.text((64, 138),
           "An options credit-spread grid that trades 25 instruments",
           font=font("georgia.ttf", 27), fill=MUTED)
    d.text((64, 176),
           "and asks a model one question: how much, if any?",
           font=font("georgiai.ttf", 27), fill=ACCENT)

    d.text((64, 240), "ALPACA AI TRADING AGENTS HACKATHON  ·  2026",
           font=font("consolab.ttf", 19), fill=MUTED)

    # --- the strip of measured numbers ----------------------------------
    pl = float(snapshot["pl_total"])
    tone = GAIN if pl >= 0 else LOSS
    cells = [
        ("EQUITY", f"{float(snapshot['equity']):,.0f}", INK),
        ("P&L", f"{pl:+,.0f}", tone),
        ("INSTRUMENTS", f"{int(snapshot['instruments_open'])}", INK),
        ("FILLS", f"{int(snapshot['fills'])}", INK),
        ("RESIDUAL", f"{float(snapshot['residual']):+.4f}", ACCENT),
    ]
    y = height - 74
    d.rectangle([0, y - 14, width, height], fill=SURFACE)
    d.line([(0, y - 14), (width, y - 14)], fill=RULE, width=1)
    x = 64
    for label, value, colour in cells:
        d.text((x, y), label, font=font("consolab.ttf", 13), fill=MUTED)
        d.text((x, y + 20), value, font=font("consolab.ttf", 27), fill=colour)
        x += 224

    stamp = str(snapshot["as_of"])[:16].replace("T", " ")
    d.text((width - 64, y + 26), f"as of {stamp} ET",
           font=font("consola.ttf", 15), fill=MUTED, anchor="rs")
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="docs/snapshot.json")
    ap.add_argument("--out", default="docs/cover.png")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=630)
    args = ap.parse_args()

    with open(args.snapshot, "r", encoding="utf-8") as fh:
        snapshot = json.load(fh)
    img = build(snapshot, args.width, args.height)
    img.save(args.out)
    print(f"{args.out}  ({args.width}x{args.height}, "
          f"{len(snapshot['curve'])} curve points, "
          f"snapshot as of {snapshot['as_of']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
