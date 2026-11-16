#!/usr/bin/env python3
"""Commit only owned generated files, never rebase stale outputs onto newer main.

A concurrent human push makes this run fail closed. The newer publication event
will rebuild from main; neither a force push nor a blind rebase is safe for images.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


class MainAdvanced(RuntimeError):
    """Only this failure is eligible for a bounded latest-main publication retry."""


def owned_paths(root, kind):
    paths = ["README.md", "README.zh-CN.md"]
    if kind == "linkcheck":
        paths += ["linkcheck-status.json", "linkcheck-titles.json"]
    elif kind == "publish":
        paths += ["contributors.json", "assets/social-preview.html", "assets/social-preview.png",
                  "assets/banner.png", "assets/banner.webp", "assets/banner-inputs.json"]
        paths += [str(p.relative_to(root)) for p in sorted((root / "data").glob("*.json"))]
    else:
        raise ValueError("unknown generated-file owner: " + kind)
    return [p for p in paths if (root / p).is_file()]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True, timeout=120).stdout.strip()


def commit_generated(root=ROOT, kind="publish", message=None):
    root = Path(root).resolve()
    paths = owned_paths(root, kind)
    git(root, "fetch", "--no-tags", "origin", "main")
    base = git(root, "rev-parse", "HEAD")
    if base != git(root, "rev-parse", "FETCH_HEAD"):
        raise MainAdvanced("main advanced during generation; refusing to commit or rebase stale outputs. "
                           "Retry publication from latest main.")
    if paths:
        git(root, "add", "--", *paths)
    staged = set(filter(None, git(root, "diff", "--cached", "--name-only").splitlines()))
    if not staged:
        print("Generated files already match latest main.")
        return False
    unexpected = staged - set(paths)
    if unexpected:
        raise RuntimeError("Refusing unexpected staged files: " + ", ".join(sorted(unexpected)))
    git(root, "config", "user.name", "github-actions[bot]")
    git(root, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git(root, "commit", "-m", message or "chore: refresh publication assets and credits [skip ci]")
    # A push after the fetch above is still protected by Git's normal fast-forward check.
    try:
        git(root, "push", "origin", "HEAD:main")
    except subprocess.CalledProcessError:
        # Distinguish a real competing push from permission/network failures. The
        # latter must not dispatch an endless sequence of identically failing runs.
        git(root, "fetch", "--no-tags", "origin", "main")
        if git(root, "rev-parse", "FETCH_HEAD") != base:
            raise MainAdvanced("main advanced before the generated-file push; retry from latest main.")
        raise
    print("Committed generated files and pushed without rebasing or forcing.")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=["publish", "linkcheck"], default="publish")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--message")
    args = ap.parse_args()
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("main_advanced=false\n")
    try:
        commit_generated(args.root, args.kind, args.message)
    except MainAdvanced as exc:
        if output:
            with open(output, "a", encoding="utf-8") as stream:
                stream.write("main_advanced=true\n")
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
