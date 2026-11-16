#!/usr/bin/env python3
"""Decide whether CI needs Chrome; the build gate always runs once CI is triggered.

Use the event's immutable SHAs, not checkout HEAD (a PR checkout is a merge commit).
Missing history, malformed events and unfamiliar paths all keep the browser gate on.
No network, third-party dependencies, or checkout of a contributor's head is needed.
License: CC0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")

# These are imported by build_site or supply its rendered content. Keep transitive
# dependencies here too: check_pr builds changelog entries; build_readme supplies
# categories, labels and schema validation; experiences renders the field notes.
RESPONSIVE_SCRIPTS = {
    "scripts/build_site.py", "scripts/build_readme.py", "scripts/experiences.py",
    "scripts/check_pr.py", "scripts/check_responsive.py",
}
# A positive allowlist for skipping Chrome: a new script/config defaults to running it.
NON_RESPONSIVE_SCRIPTS = {
    "scripts/build_llms.py", "scripts/check_banner.py", "scripts/check_entry.py",
    "scripts/linkcheck.py", "scripts/pr_linkcheck.py", "scripts/promo_stats.py",
    "scripts/sync_contributors.py", "scripts/sync_experiences.py",
}
NON_RESPONSIVE_FILES = {
    "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "LICENSE",
    ".github/PULL_REQUEST_TEMPLATE.md", ".github/dependabot.yml", ".gitignore",
    "linkcheck-titles.json",
}


@dataclass(frozen=True)
class Decision:
    responsive_needed: bool
    reason: str
    paths: tuple[str, ...] = ()


def classify_paths(paths: list[str]) -> Decision:
    """Skip only paths known not to affect rendered pages; unknown means run."""
    for path in paths:
        parts = PurePosixPath(path).parts
        if not path or path.startswith("/") or ".." in parts or "\\" in path:
            return Decision(True, "unknown path", tuple(paths))
        if (path.startswith(("data/", "schema/", "site/"))
                or path in RESPONSIVE_SCRIPTS or path == "contributors.json"):
            return Decision(True, "page or payload changed", tuple(paths))
        if (path in NON_RESPONSIVE_FILES or path in NON_RESPONSIVE_SCRIPTS
                or path.startswith(("docs/", ".github/ISSUE_TEMPLATE/"))
                or (path.startswith("PROMO-") and "/" not in path and path.endswith(".md"))
                or (path.startswith("tests/") and path.endswith(".py"))):
            continue
        return Decision(True, "unknown path", tuple(paths))
    return Decision(False, "only known non-page paths changed", tuple(paths))


def _sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value) or set(value) == {"0"}:
        raise ValueError("missing or invalid commit SHA")
    return value


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30,
    ).stdout


def changed_paths(event_name: str, event: dict, root: Path = ROOT) -> list[str]:
    """PRs use merge-base..head; pushes use before..after, including forced pushes.

    The default PR checkout's HEAD includes base-branch work. Comparing it to base
    instead of using the event head can hide changes or include unrelated changes.
    Full checkout history normally contains both event SHAs; absent objects fail closed.
    --no-renames makes both sides of a rename visible to the path classifier.
    """
    if event_name == "pull_request":
        pr = event["pull_request"]
        base, head = _sha(pr["base"]["sha"]), _sha(pr["head"]["sha"])
        # Fail on an absent/ambiguous merge base rather than guessing HEAD's parents.
        bases = _git(root, "merge-base", "--all", base, head).decode("ascii").splitlines()
        if len(bases) != 1:
            raise ValueError("missing or ambiguous PR merge base")
        base = _sha(bases[0])
    elif event_name == "push":
        base, head = _sha(event["before"]), _sha(event["after"])
    else:
        raise ValueError("event needs full browser coverage")
    # NUL-delimited paths handle spaces/newlines without parsing git's quoting format.
    raw = _git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--")
    if raw and not raw.endswith(b"\0"):
        raise ValueError("incomplete diff output")
    return [path.decode("utf-8") for path in raw.split(b"\0")[:-1]]


def classify_event(event_name: str, event: dict, root: Path = ROOT) -> Decision:
    try:
        return classify_paths(changed_paths(event_name, event, root))
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return Decision(True, "unknown event or diff; full browser coverage")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    parser.add_argument("--output", default=os.environ.get("GITHUB_OUTPUT", ""))
    args = parser.parse_args(argv)
    try:
        event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
        decision = classify_event(args.event_name, event)
    except (OSError, ValueError):
        decision = Decision(True, "unreadable event; full browser coverage")
    value = str(decision.responsive_needed).lower()
    print(f"responsive_needed={value} ({decision.reason}; {len(decision.paths)} changed paths)")
    if args.output:
        with Path(args.output).open("a", encoding="utf-8") as output:
            output.write(f"responsive_needed={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
