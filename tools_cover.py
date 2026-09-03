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

"""The submission cover image, drawn from the account's own marks.

A cover made of stock artwork would say nothing about the project. This
one is the open mark of every instrument out of docs/snapshot.json - the
same file the live sheet reads - so the picture on the submission is the
picture of the account. If the snapshot cannot be read the run FAILS
rather than falling back to decoration: a fabricated chart on the cover of
a project whose whole argument is measurement would be the worst possible
thing to ship.

It draws marks rather than the equity curve because the curve, cut back to
levels that survive their checks, currently holds three points. Three
points make a flat line with a cliff on it, which reads as a rendering
fault rather than as a result. 25 bars are the same data honestly.

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
RULE_STRONG = (51, 61, 76)
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

    # GRIDS only. Since the first assignments (2026-09-02) the snapshot's
    # instrument list also carries rows of SHARES the account was handed
    # when a short leg was exercised against it - AMZN -100 at +933, GLD
    # +100 at +348. Drawing those as bars would put four positions nobody
    # opened into a picture of the grid, and their marks are large enough
    # to set the scale for everything else.
    rows = [r for r in snapshot["instruments"] if not r.get("assigned")]
    if not rows:
        raise SystemExit("the snapshot holds no grid rows - nothing to draw")

    # --- the header, on its own band -----------------------------------
    d.text((64, 54), "KANGAROO OPTIONS", font=font("georgiab.ttf", 62),
           fill=INK)
    d.text((64, 134),
           "An options credit-spread grid that trades 25 instruments",
           font=font("georgia.ttf", 27), fill=MUTED)
    d.text((64, 172),
           "and asks a model one question: how much, if any?",
           font=font("georgiai.ttf", 27), fill=ACCENT)
    d.text((64, 232), "ALPACA AI TRADING AGENTS HACKATHON  ·  2026",
           font=font("consolab.ttf", 19), fill=MUTED)
    d.line([(64, 272), (width - 64, 272)], fill=RULE, width=1)

    # --- one bar per instrument, at its mark ----------------------------
    top, bottom = 300, height - 132
    mid = (top + bottom) / 2
    marks = sorted((float(r["unrealized"]) for r in rows), reverse=True)
    scale = max(abs(m) for m in marks) or 1.0
    half = (bottom - top) / 2 - 6

    left, right = 64, width - 64
    slot = (right - left) / len(marks)
    bar = max(6.0, slot * 0.62)
    d.line([(left, mid), (right, mid)], fill=RULE_STRONG, width=1)
    for i, m in enumerate(marks):
        x = left + slot * (i + 0.5)
        h = abs(m) / scale * half
        y0, y1 = (mid - h, mid) if m >= 0 else (mid, mid + h)
        d.rectangle([x - bar / 2, y0, x + bar / 2, y1],
                    fill=GAIN if m >= 0 else LOSS)

    d.text((left, top - 24),
           # "25 instruments" above and "21 grids" here are both true and
           # read as a contradiction side by side: 25 is what the agent
           # runs, 21 is how many are holding a position right now. Say
           # which, rather than leaving a reader to reconcile two numbers.
           f"OPEN MARK PER INSTRUMENT  ·  {len(marks)} OF 25 HOLDING  ·  "
           f"BEST {marks[0]:+,.0f}   WORST {marks[-1]:+,.0f}",
           font=font("consolab.ttf", 14), fill=MUTED)

    # --- the strip of measured numbers ----------------------------------
    pl = float(snapshot["pl_total"])
    tone = GAIN if pl >= 0 else LOSS
    cells = [
        ("EQUITY", f"{float(snapshot['equity']):,.0f}", INK),
        ("P&L", f"{pl:+,.0f}", tone),
        ("REALIZED", f"{float(snapshot['realized']):+,.0f}", INK),
        ("FILLS", f"{int(snapshot['fills'])}", INK),
        ("RESIDUAL", f"{float(snapshot['residual']):+.4f}", ACCENT),
    ]
    y = height - 96
    d.rectangle([0, y - 16, width, height], fill=SURFACE)
    d.line([(0, y - 16), (width, y - 16)], fill=RULE, width=1)
    x = 64
    for label, value, colour in cells:
        d.text((x, y), label, font=font("consolab.ttf", 13), fill=MUTED)
        d.text((x, y + 22), value, font=font("consolab.ttf", 28), fill=colour)
        x += 216

    stamp = str(snapshot["as_of"])[:16].replace("T", " ")
    d.text((width - 64, height - 22),
           f"every figure read back through the Alpaca CLI  ·  "
           f"as of {stamp} ET",
           font=font("consola.ttf", 14), fill=MUTED, anchor="rs")
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
