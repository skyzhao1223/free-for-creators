#!/usr/bin/env python3
"""Render and validate the banner using the original CI recipe. License: CC0.

Requires Chrome, cwebp, Pillow and numpy; Linux also requires fontconfig and the
reviewed DejaVu/Noto fonts (install them in the calling workflow). Counts must
already be stamped by build_readme.py. This does not commit or stamp a manifest.

    python3 scripts/render_banner.py --chrome /path/to/chrome
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTBOARD = (1280, 640)
RENDER_SIZE = (2560, 1280)


def check_linux_fonts():
    """Keep the CI's generic sans-serif assumption, without imposing it on macOS."""
    if not sys.platform.startswith("linux"):
        return
    resolved = subprocess.run(
        ["fc-match", "-f", "%{family[0]}", "sans-serif"], check=True,
        capture_output=True, text=True).stdout.strip()
    print("fc-match sans-serif -> " + resolved)
    if resolved != "DejaVu Sans":
        raise ValueError("sans-serif resolves to %r, expected 'DejaVu Sans'. Install the "
                         "expected font or re-review the render and update this check." % resolved)
    for family in ("DejaVu Sans", "Noto Sans", "Noto Sans CJK SC", "Liberation Sans", "Noto Color Emoji"):
        result = subprocess.run(["fc-match", family], check=True, capture_output=True, text=True)
        print('font "%s": %s' % (family, result.stdout.strip()))
    faces = subprocess.run(["fc-list"], check=True, capture_output=True, text=True).stdout.lower()
    for name in ("noto color emoji", "noto sans cjk"):
        count = sum(name in line for line in faces.splitlines())
        print("%s faces: %d" % (name, count))
        if count == 0:
            raise ValueError("Missing %s fonts; refusing a tofu render." % name)


def check_pixels(assets):
    """Preserve the CI colour, text-region, ink and saturation thresholds."""
    from PIL import Image
    import numpy as np

    failures = []
    for name, want in (("banner.png", RENDER_SIZE), ("banner.webp", RENDER_SIZE),
                       ("social-preview.png", ARTBOARD)):
        with Image.open(assets / name) as source:
            image = source.convert("RGB")
        if image.size != want:
            failures.append("%s: size %s, want %s" % (name, image.size, want))
            continue
        cols = len(set(image.resize((160, 80)).getdata()))
        print("  %s %s: %d distinct colours (160x80 sample)"
              % ("ok  " if cols > 800 else "FAIL", name, cols))
        if cols <= 800:
            failures.append("%s: too few distinct colours" % name)
    with Image.open(assets / "banner.png") as source:
        image = source.convert("RGB")
    for name, box in (("title band", (120, 300, 2000, 520)),
                      ("count pill", (120, 700, 1600, 900))):
        cols = len(set(image.crop(box).resize((120, 40)).getdata()))
        print("  %s %s: %d distinct colours" % ("ok  " if cols > 200 else "FAIL", name, cols))
        if cols <= 200:
            failures.append("%s: too few distinct colours" % name)
    gray = np.asarray(image.convert("L"), dtype=np.int16)
    background = int(np.median(gray))
    ink = float((np.abs(gray - background) > 28).mean() * 100)
    rgb = np.asarray(image, dtype=np.int16)
    saturation = float(((rgb.max(axis=2) - rgb.min(axis=2)) > 40).mean() * 100)
    print("  %s ink coverage: %.2f%% (want 10-25%%)"
          % ("ok  " if 10.0 <= ink <= 25.0 else "FAIL", ink))
    print("  %s saturated pixels: %.2f%% (want >=15%%, emoji+category dots)"
          % ("ok  " if saturation >= 15.0 else "FAIL", saturation))
    if not 10.0 <= ink <= 25.0:
        failures.append("ink coverage outside 10-25%")
    if saturation < 15.0:
        failures.append("saturated pixels below 15%")
    if failures:
        raise ValueError("Render pixel checks failed: " + "; ".join(failures))


def render(chrome, root=ROOT):
    """Render into a temporary directory and publish only fully checked images."""
    from PIL import Image
    import check_banner

    root = Path(root).resolve()
    html = root / "assets" / "social-preview.html"
    if not html.is_file():
        raise ValueError("Missing artwork: " + str(html))
    check_linux_fonts()
    subprocess.run([chrome, "--version"], check=True)
    subprocess.run(["cwebp", "-version"], check=True)
    with tempfile.TemporaryDirectory(prefix="banner-render-") as temporary:
        assets = Path(temporary)
        screenshot = assets / "social-preview@2x.png"
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=2", "--window-size=1280,640",
             "--virtual-time-budget=8000", "--screenshot=" + str(screenshot), html.as_uri()],
            check=True, timeout=120)
        if not screenshot.is_file() or screenshot.stat().st_size == 0:
            raise ValueError("render produced an empty file")
        with Image.open(screenshot) as image:
            print("2x render:", image.size, image.mode)
            if image.size != RENDER_SIZE:
                raise ValueError("unexpected 2x size %s" % (image.size,))
            image.resize(ARTBOARD, Image.LANCZOS).save(assets / "social-preview.png", optimize=True)
        shutil.copyfile(screenshot, assets / "banner.png")
        # Sharp YUV protects saturated edges from WebP's 4:2:0 subsampling.
        subprocess.run(["cwebp", "-quiet", "-q", "90", "-sharp_yuv",
                        str(assets / "banner.png"), "-o", str(assets / "banner.webp")], check=True)
        check_pixels(assets)
        expected = check_banner.category_count(root / "data")
        metrics = check_banner.measure(html, chrome)
        failures, notes = check_banner.evaluate(metrics, expected)
        print("Banner layout: %d categories, artboard %dx%d" % (expected, *ARTBOARD))
        for note in notes:
            print("  " + note)
        if failures:
            raise ValueError("Banner layout checks failed: " + "; ".join(failures))
        print("Chip row fits the artwork's budget.")
        for name in ("social-preview.png", "banner.png", "banner.webp"):
            shutil.copyfile(assets / name, root / "assets" / name)
            print("  %d %s" % ((assets / name).stat().st_size, name))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chrome", required=True, help="Chrome/Chromium binary")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args(argv)
    try:
        return render(args.chrome, args.root)
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        print("Banner render: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
