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

"""Render the performance sheet: template + snapshot -> docs/index.html.

The template holds the whole design and reads its numbers from one
embedded json block, so refreshing the published page is
tools_perf_snapshot.py followed by this - the layout is never touched to
move a figure.

The page ships with the snapshot it was built from AND, when --live-url is
given, re-fetches that snapshot at open time. That second source is what
makes the published page keep up between builds; the built-in copy is what
makes it correct when the fetch fails. Point --live-url at
raw.githubusercontent.com rather than at the Pages path itself: measured
2026-08-31, Pages answers `Cache-Control: max-age=600` and ignores the
query string (a fresh `?v=` returned the same `Age: 117` twice), while
raw answers the same file with `max-age=300` and
`Access-Control-Allow-Origin: *`.

Usage:
    python tools_perf_page.py --snapshot docs/snapshot.json \
        --template perf_page.tpl.html --out docs/index.html \
        --live-url https://raw.githubusercontent.com/<owner>/<repo>/<branch>/docs/snapshot.json
"""

import argparse
import json
import sys

PLACEHOLDER = "/*__BOOT__*/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--live-url", default="",
                    help="raw URL of the published snapshot.json; empty "
                         "leaves the page on its built-in copy")
    ap.add_argument("--repo-url", default="https://github.com/Hanky27/KangarooOptions")
    args = ap.parse_args()

    with open(args.snapshot, "r", encoding="utf-8") as fh:
        snapshot = json.load(fh)

    with open(args.template, "r", encoding="utf-8") as fh:
        html = fh.read()

    if PLACEHOLDER not in html:
        raise SystemExit(f"{args.template} has no {PLACEHOLDER} to fill")

    boot = {
        "snapshot": snapshot,
        "live_url": args.live_url,
        "repo_url": args.repo_url,
    }

    # The json goes inside a <script> element, where the HTML parser ends the
    # script at the first "</script" it sees no matter where that sits - a
    # value containing that text would truncate the page. Escaping the slash
    # keeps the json valid and the parser out of it.
    payload = json.dumps(boot, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace(PLACEHOLDER, payload)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"{args.out}  ({len(html):,} bytes, snapshot as of {snapshot['as_of']}, "
          f"live source: {args.live_url or 'none - built-in copy only'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
