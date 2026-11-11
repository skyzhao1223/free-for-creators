#!/usr/bin/env python3
"""Validate data/experiences/notes.json — the "creator field notes" store.

A field note is a first-hand write-up by a creator about one resource in this
directory: what they actually made with it, what the license turned out to mean in
practice, what went wrong. The site renders the ones a maintainer has reviewed, and
every note links back to the public issue / discussion / PR it came from, so nothing
reaches the site without a thread a reader can check.

Why this is not data/*.json: build_readme.load_categories() globs data/*.json and
requires every file it finds to be a category whose slug is in CATEGORY_ORDER, and
check_pr.py reads the same directory as the entry diff. A subdirectory is invisible to
both, which is what a second, differently-shaped store needs.

The rules enforced here (schema/experience.schema.json is the editor aid):
  * ids are unique slugs; every date is real and never in the future
  * entry_url is the verbatim url of an entry in data/*.json — the note's anchor into
    the directory, so a renamed or removed entry fails CI instead of orphaning a card
  * evidence is an issue / discussion / PR in this repository
  * title and body have floors and ceilings: a note is a story, not a tweet or an essay
  * reviewed_by / reviewed_on name the human who let it onto the site, and review cannot
    predate the experience itself

Usage:
    python3 scripts/experiences.py --check   # CI gate: validate the store
    python3 scripts/experiences.py --json    # print the validated store

Stdlib only (Python >= 3.9). License: CC0.
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES_PATH = ROOT / "data" / "experiences" / "notes.json"

# Mirrors build_readme.REPO (and site/index.html's own const REPO) rather than importing
# it: build_readme imports this module to render the notes, so this module imports
# build_readme only inside main(). tests/test_experiences.py asserts the two agree.
REPO = "skyzhao1223/free-for-creators"
ISSUE_FORM = "https://github.com/%s/issues/new?template=share-experience.yml" % REPO
DISCUSSIONS = "https://github.com/%s/discussions" % REPO

LANGS = ("en", "zh")
MIN_TITLE, MAX_TITLE = 6, 90
MIN_BODY, MAX_BODY = 80, 800
MAX_TAGS, MIN_TAG, MAX_TAG = 3, 2, 16
REQUIRED_FIELDS = ("id", "entry_url", "author", "lang", "date", "title", "body",
                   "evidence", "reviewed_by", "reviewed_on")
OPTIONAL_FIELDS = ("tags",)
TOP_LEVEL_FIELDS = ("version", "notes")

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
EVIDENCE_RE = re.compile(
    r"^https://github[.]com/%s/(?:issues|pull|discussions)/[0-9]+$" % re.escape(REPO))


def fail(errors):
    for e in errors:
        print("ERROR: %s" % e, file=sys.stderr)
    sys.exit(1)


def parse_date(value):
    """A real YYYY-MM-DD date, or None. Rejects 2026-02-30 and 2026-13-01."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def entry_index(categories):
    """{url: (category, entry)} keyed on the url exactly as data/ writes it.

    Exact match is the point: a note either points at a row of this directory or it
    does not. Normalising here would let a note attach to a url the directory never
    published, which is the one thing the anchor is supposed to prevent.
    """
    index = {}
    for c in categories:
        for e in c["entries"]:
            index.setdefault(e["url"], (c, e))
    return index


def check_store(store, categories, today=None):
    """Validate a parsed store. Returns (errors, warnings) — never exits."""
    today = today or date.today()
    errors, warnings = [], []
    if not isinstance(store, dict):
        return ["the store must be a JSON object"], warnings
    for key in sorted(set(store) - set(TOP_LEVEL_FIELDS)):
        errors.append("unknown top-level key '%s'" % key)
    if store.get("version") != 1:
        errors.append("version must be 1 (got %r)" % (store.get("version"),))
    notes = store.get("notes")
    if not isinstance(notes, list):
        return errors + ["'notes' must be a list"], warnings

    index = entry_index(categories)
    seen_ids, seen_pairs = set(), set()
    for i, n in enumerate(notes):
        label = "#%d" % (i + 1)
        if not isinstance(n, dict):
            errors.append("note %s: must be an object" % label)
            continue
        label = "note %s (%s)" % (label, n.get("id", "?"))
        for key in sorted(set(n) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)):
            errors.append("%s: unknown field '%s'" % (label, key))
        for field in REQUIRED_FIELDS:
            if not n.get(field):
                errors.append("%s: missing or empty '%s'" % (label, field))
        if not isinstance(n.get("title", ""), str) or not isinstance(n.get("body", ""), str):
            continue

        note_id = n.get("id")
        if isinstance(note_id, str):
            if not ID_RE.match(note_id):
                errors.append("%s: id must be a lower-kebab slug" % label)
            elif note_id in seen_ids:
                errors.append("%s: duplicate id" % label)
            seen_ids.add(note_id)

        entry_url = n.get("entry_url")
        if entry_url and entry_url not in index:
            errors.append("%s: entry_url is not a url in data/*.json — copy it verbatim" % label)

        for field in ("author", "reviewed_by"):
            value = n.get(field)
            if value and (not isinstance(value, str) or not HANDLE_RE.match(value)):
                errors.append("%s: %s must be a bare GitHub username" % (label, field))

        if n.get("lang") and n["lang"] not in LANGS:
            errors.append("%s: lang must be one of %s" % (label, " / ".join(LANGS)))

        when = parse_date(n.get("date"))
        if n.get("date") and when is None:
            errors.append("%s: date must be a real YYYY-MM-DD date" % label)
        elif when and when > today:
            errors.append("%s: date %s is in the future" % (label, when))

        title = n.get("title", "")
        if title:
            if not MIN_TITLE <= len(title) <= MAX_TITLE:
                errors.append("%s: title must be %d-%d characters (%d)" % (label, MIN_TITLE, MAX_TITLE, len(title)))
            if "|" in title:
                errors.append("%s: title must not contain '|' (it breaks the README list)" % label)

        body = n.get("body", "")
        if body:
            if not MIN_BODY <= len(body) <= MAX_BODY:
                errors.append("%s: body must be %d-%d characters (%d) — say what you made and what you learned"
                              % (label, MIN_BODY, MAX_BODY, len(body)))
            if chr(10) in body or chr(13) in body:
                errors.append("%s: body must be a single paragraph (no line breaks)" % label)
            if body != body.strip():
                errors.append("%s: body must not start or end with whitespace" % label)

        tags = n.get("tags", [])
        if tags:
            if not isinstance(tags, list):
                errors.append("%s: tags must be a list" % label)
            elif len(tags) > MAX_TAGS:
                errors.append("%s: at most %d tags" % (label, MAX_TAGS))
            else:
                for tag in tags:
                    if not isinstance(tag, str) or not MIN_TAG <= len(tag) <= MAX_TAG:
                        errors.append("%s: each tag must be %d-%d characters" % (label, MIN_TAG, MAX_TAG))
                    elif "|" in tag:
                        errors.append("%s: tags must not contain '|'" % label)
                if len(set(tags)) != len(tags):
                    errors.append("%s: duplicate tags" % label)

        evidence = n.get("evidence")
        if evidence and not EVIDENCE_RE.match(evidence):
            errors.append("%s: evidence must be an issue, discussion or PR in github.com/%s" % (label, REPO))

        reviewed = parse_date(n.get("reviewed_on"))
        if n.get("reviewed_on") and reviewed is None:
            errors.append("%s: reviewed_on must be a real YYYY-MM-DD date" % label)
        elif reviewed and today and reviewed > today:
            errors.append("%s: reviewed_on %s is in the future" % (label, reviewed))
        elif reviewed and when and reviewed < when:
            errors.append("%s: reviewed_on %s precedes the experience date %s" % (label, reviewed, when))

        pair = (n.get("author"), entry_url)
        if all(pair):
            if pair in seen_pairs:
                warnings.append("%s: same author already wrote about this resource" % label)
            seen_pairs.add(pair)
    return errors, warnings


def load_store(path=None):
    """Read the store; exits on a missing or malformed file."""
    path = Path(path) if path else NOTES_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(["%s does not exist" % path])
    except json.JSONDecodeError as e:
        fail(["%s: invalid JSON (%s)" % (path, e)])


def load_notes(categories, path=None):
    """The validated notes for a loaded directory. Exits on any error."""
    store = load_store(path)
    errors, warnings = check_store(store, categories)
    for w in warnings:
        print("WARNING: data/experiences/notes.json: %s" % w, file=sys.stderr)
    if errors:
        fail(["data/experiences/notes.json: %s" % e for e in errors])
    return store["notes"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate the store (default)")
    ap.add_argument("--json", action="store_true", help="print the validated store")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / "scripts"))
    from build_readme import load_categories  # imported here: build_readme imports this module

    categories = load_categories()
    notes = load_notes(categories)
    if args.json:
        print(json.dumps({"version": 1, "notes": notes}, ensure_ascii=False, indent=2))
        return
    total = sum(len(c["entries"]) for c in categories)
    print("Validated %d field note(s) against %d resources." % (len(notes), total))


if __name__ == "__main__":
    main()
