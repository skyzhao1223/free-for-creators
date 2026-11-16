#!/usr/bin/env python3
"""Check the banner's layout, not just its pixels. Stdlib only. License: CC0.

render_banner.py rejects a blank or tofu render by colour count, ink coverage and
saturation. None of that can see the failure mode this file exists for: the banner draws
one chip per category, and the chips are a flex row with a fixed max-width. Adding a
category wraps the row onto another line — the pixels stay colourful, the ink coverage
barely moves, and the render passes every check that looks at colour. What changes is
geometry: the row grows downward, and it grows every time somebody adds a category.

So this drives the same headless Chrome, reads the laid-out boxes out of the DOM, and
fails when the chips no longer fit the artwork's budget or collide with the table card.
Raising MAX_CHIP_ROWS is meant to be a deliberate act: it says somebody looked at the
picture after a category was added.

Usage:
    python3 scripts/check_banner.py --chrome /path/to/chrome
    python3 scripts/check_banner.py --chrome ... --html assets/social-preview.html

Exit codes: 0 = the layout is within budget, 1 = it is not (or Chrome could not run).
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "assets" / "social-preview.html"
DATA_DIR = ROOT / "data"

#: The artboard the social preview is rendered at.
ARTBOARD = (1280, 640)
#: How many wrapped lines the chip row may use. 11 categories use 3. Raising this is the
#: point at which a human is expected to have looked at the rendered banner.
MAX_CHIP_ROWS = 3
#: Room the artwork leaves below the left column before the bottom edge.
BOTTOM_MARGIN = 12
#: Sub-pixel slack when comparing measured boxes.
TOLERANCE = 2.0
PROBE_MARK = "BANNER_PROBE "

PROBE = """
<script>
(function () {
  function box(sel) {
    var el = document.querySelector(sel);
    if (!el) return null;
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    return {
      top: r.top, bottom: r.bottom, left: r.left, right: r.right,
      width: r.width, height: r.height,
      clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
      rowGap: parseFloat(cs.rowGap) || 0
    };
  }
  function measure() {
    var chips = box('.chips');
    var chip = box('.chip');
    var card = box('.table-card');
    return {
      artboard: {width: window.innerWidth, height: window.innerHeight},
      chips: chips,
      chipHeight: chip ? chip.height : 0,
      chipCount: document.querySelectorAll('.chips .chip').length,
      card: card
    };
  }
  function publish() {
    try {
      document.title = "" + 'BANNER_PROBE ' + JSON.stringify(measure());
    } catch (e) {
      document.title = 'BANNER_PROBE_ERROR ' + e;
    }
  }
  window.addEventListener('load', function () { setTimeout(publish, 300); });
})();
</script>
</body>"""
PROBE = PROBE.replace('"" + ', "")


def category_count(data_dir=DATA_DIR):
    """One chip is drawn per category, so this is what the artwork must match."""
    return len(sorted(Path(data_dir).glob("*.json")))


def rgb_intersect(a, b):
    return not (a["right"] <= b["left"] or b["right"] <= a["left"]
                or a["bottom"] <= b["top"] or b["bottom"] <= a["top"])


def evaluate(metrics, expected_chips, artboard=ARTBOARD, max_rows=MAX_CHIP_ROWS,
             bottom_margin=BOTTOM_MARGIN):
    """Pure rules over the measured boxes. Returns (failures, notes)."""
    failures, notes = [], []
    chips = metrics.get("chips")
    if not chips:
        return ["the artwork has no .chips block, so nothing draws the category chips"], notes

    chip_height = float(metrics.get("chipHeight") or 0)
    row_gap = float(chips.get("rowGap") or 0)
    rows = 0
    if chip_height > 0:
        rows = int(round((float(chips["height"]) + row_gap) / (chip_height + row_gap)))
    notes.append("chips: %d across %d row(s), %.0fx%.0f px, %.0f px wide box"
                 % (metrics.get("chipCount", 0), rows, chips["width"], chips["height"],
                    chips["clientWidth"]))

    if metrics.get("chipCount") != expected_chips:
        failures.append("the artwork draws %s chip(s) but data/ has %d categories — add the "
                        "missing chip to assets/social-preview.html and re-check the layout"
                        % (metrics.get("chipCount"), expected_chips))
    if rows > max_rows:
        failures.append("the chips now wrap to %d rows (budget %d): open the artwork in a "
                        "browser, adjust it, then raise MAX_CHIP_ROWS deliberately"
                        % (rows, max_rows))
    if float(chips["scrollWidth"]) > float(chips["clientWidth"]) + TOLERANCE:
        failures.append("the chip row overflows its box: scrollWidth %.0f > clientWidth %.0f"
                        % (chips["scrollWidth"], chips["clientWidth"]))
    if float(chips["bottom"]) > artboard[1] - bottom_margin:
        failures.append("the chip row ends at y=%.0f, past the %.0f px artboard's %d px "
                        "bottom margin" % (chips["bottom"], artboard[1], bottom_margin))
    if float(chips["right"]) > artboard[0] or float(chips["left"]) < 0:
        failures.append("the chip row leaves the artboard horizontally: x %.0f..%.0f"
                        % (chips["left"], chips["right"]))

    card = metrics.get("card")
    if not card:
        failures.append("the artwork has no .table-card block to compare the chips against")
    elif rgb_intersect(chips, card):
        failures.append("the chip row and the table card overlap (chips x %.0f..%.0f y %.0f..%.0f, "
                        "card x %.0f..%.0f y %.0f..%.0f)"
                        % (chips["left"], chips["right"], chips["top"], chips["bottom"],
                           card["left"], card["right"], card["top"], card["bottom"]))
    else:
        notes.append("gap to the table card: %.0f px" % (card["left"] - chips["right"]))
    return failures, notes


def measure(html_path, chrome):
    """Render the artwork with a probe injected and read the boxes back out of the DOM."""
    html = Path(html_path).read_text(encoding="utf-8")
    if "</body>" not in html:
        raise SystemExit("%s has no </body> to inject the probe before" % html_path)
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "probe.html"
        probe_path.write_text(html.replace("</body>", PROBE, 1), encoding="utf-8")
        result = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--virtual-time-budget=4000", "--window-size=%d,%d" % ARTBOARD,
             "--dump-dom", probe_path.as_uri()],
            capture_output=True, text=True, timeout=120,
        )
    dom = result.stdout
    if PROBE_MARK not in dom:
        raise SystemExit("the probe did not run (Chrome exit %s). stderr: %s"
                         % (result.returncode, result.stderr.strip()[:300]))
    payload = dom.split(PROBE_MARK, 1)[1]
    payload = payload.split("</title>", 1)[0]
    for entity, char in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        payload = payload.replace(entity, char)
    return json.loads(payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chrome", help="path to a Chrome/Chromium binary (default: found on PATH)")
    ap.add_argument("--html", default=str(HTML), help="the artwork to measure")
    args = ap.parse_args()

    chrome = args.chrome
    if not chrome:
        sys.path.insert(0, str(ROOT / "scripts"))
        from check_responsive import find_chrome  # one Chrome probe in this repo, not two
        chrome = find_chrome(None)
    if not chrome:
        print("No Chrome/Chromium binary found; pass --chrome.", file=sys.stderr)
        return 2

    expected = category_count()
    metrics = measure(args.html, chrome)
    failures, notes = evaluate(metrics, expected)

    print("Banner layout: %d categories, artboard %dx%d"
          % (expected, ARTBOARD[0], ARTBOARD[1]))
    for note in notes:
        print("  %s" % note)
    for failure in failures:
        print("  FAIL %s" % failure)
    if failures:
        print("%d layout problem(s)." % len(failures))
        return 1
    print("Chip row fits the artwork's budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
