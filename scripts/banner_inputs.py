#!/usr/bin/env python3
"""Fingerprint banner render inputs without rendering. Stdlib only. License: CC0.

Run after build_readme.py has stamped the HTML. Data changes that leave that HTML
unchanged do not invalidate the banner. Only a successful renderer may be followed
by --stamp; this command checks image presence and hashes, not whether a render succeeded.

    python3 scripts/banner_inputs.py --check --github-output "$GITHUB_OUTPUT"
    python3 scripts/banner_inputs.py --check --force
    python3 scripts/banner_inputs.py --stamp
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "assets/banner-inputs.json"
# The publisher owns dependency/Chrome/font installation, so its recipe is an input too.
INPUTS = ("assets/social-preview.html", "scripts/render_banner.py", "scripts/check_banner.py",
          ".github/workflows/pages.yml")
IMAGES = ("assets/social-preview.png", "assets/banner.png", "assets/banner.webp")
# Bump when external render assumptions change (fonts, Chrome recipe, encoder).
RENDER_CONFIG_VERSION = "chrome-2x-1280x640-v1-pillow-lanczos-webp-q90-sharp-yuv-linux-fonts"
SCHEMA_VERSION = 1


def fingerprint(inputs, config_version=RENDER_CONFIG_VERSION):
    """Pure, order-independent fingerprint over named input bytes and config."""
    hashes = {name: hashlib.sha256(content).hexdigest()
              for name, content in sorted(inputs.items())}
    payload = {"schema_version": SCHEMA_VERSION,
               "render_config_version": config_version, "inputs": hashes}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return dict(payload, fingerprint=hashlib.sha256(encoded).hexdigest())


def current_inputs(root=ROOT):
    root = Path(root)
    return fingerprint({name: (root / name).read_bytes() for name in INPUTS})


def evaluate(current, manifest, missing_images=(), force=False):
    """Pure gate returning (needs_render, reasons); invalid manifests fail closed."""
    reasons = []
    if force:
        reasons.append("forced")
    if missing_images:
        reasons.append("missing or empty images: " + ", ".join(missing_images))
    if not isinstance(manifest, dict):
        reasons.append("missing or invalid input manifest")
    else:
        recorded_inputs = {key: value for key, value in manifest.items() if key != "images"}
        current_fingerprint = {key: value for key, value in current.items() if key != "images"}
        if recorded_inputs != current_fingerprint:
            reasons.append("render inputs changed")
        recorded_images = manifest.get("images")
        if not isinstance(recorded_images, dict) or set(recorded_images) != set(IMAGES):
            reasons.append("missing or invalid image hashes in manifest")
        elif recorded_images != current.get("images"):
            reasons.append("rendered image contents changed")
    return bool(reasons), reasons


def missing_images(root=ROOT):
    root = Path(root)
    return [name for name in IMAGES
            if not (root / name).is_file() or (root / name).stat().st_size == 0]


def current_manifest(root=ROOT):
    """Input fingerprint plus separate image integrity hashes, not fingerprint inputs."""
    root = Path(root)
    images = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
              for name in IMAGES if (root / name).is_file() and (root / name).stat().st_size > 0}
    return dict(current_inputs(root), images=images)


def check(root=ROOT, force=False):
    root = Path(root)
    current = current_manifest(root)
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    needed, reasons = evaluate(current, manifest, missing_images(root), force)
    return needed, reasons, current


def stamp(root=ROOT):
    """Record current inputs after successful rendering, with stable JSON bytes."""
    root = Path(root)
    missing = missing_images(root)
    if missing:
        raise ValueError("Cannot stamp missing or empty images: " + ", ".join(missing))
    current = current_manifest(root)
    text = json.dumps(current, sort_keys=True, indent=2) + "\n"
    path = root / MANIFEST
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return current


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report whether rendering is needed; exit 0")
    mode.add_argument("--stamp", action="store_true", help="record inputs ONLY after a successful render")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--force", action="store_true", help="force needs_render=true in check mode")
    parser.add_argument("--github-output", nargs="?", const=os.environ.get("GITHUB_OUTPUT", ""),
                        help="append needs_render to PATH (default: GITHUB_OUTPUT)")
    args = parser.parse_args(argv)
    if args.stamp and (args.force or args.github_output is not None):
        parser.error("--force and --github-output apply only to --check")
    if args.github_output == "":
        parser.error("--github-output requires PATH or GITHUB_OUTPUT")
    try:
        if args.stamp:
            current = stamp(args.root)
            print("Stamped banner inputs: " + current["fingerprint"])
        else:
            needed, reasons, _ = check(args.root, args.force)
            output = "needs_render=" + str(needed).lower()
            print(output)
            print("Banner inputs: " + ("; ".join(reasons) if reasons else "unchanged; all images present"))
            if args.github_output is not None:
                with Path(args.github_output).open("a", encoding="utf-8") as stream:
                    stream.write(output + "\n")
    except (OSError, ValueError) as exc:
        print("Banner inputs: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
