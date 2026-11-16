#!/usr/bin/env python3
"""Turn issue-form submissions into field-note records, for review in a pull request.

The field-notes module has two halves. scripts/experiences.py judges and renders a note;
this script collects one. It watches the repository's issues for submissions made with
.github/ISSUE_TEMPLATE/share-experience.yml, turns each new one into a record in
data/experiences/notes.json, and stops there — it never publishes anything on its own.

.github/workflows/experiences.yml runs it weekly on a branch and opens a pull request whose
body lists every note it carries and what each one will be published as. Merging that PR is
the review, which is also what the record claims: the note says reviewed_by the maintainer
who merged it, so a merge after reading is the honest way to land one. Delete a record before
merging to reject it, and add its issue to data/experiences/skipped.json so the next weekly
run does not propose it again.

Usage:
    python3 scripts/sync_experiences.py --fetch                  # ask the GitHub API
    python3 scripts/sync_experiences.py --from-json issues.json  # offline: a saved API page
    python3 scripts/sync_experiences.py --from-json issues.json --dry-run

Env:
    GITHUB_TOKEN       read access to this repository's issues; required by --fetch
    GITHUB_REPOSITORY  owner/repo override, defaults to the repo in scripts/experiences.py

Stdlib only (Python >= 3.9). License: CC0.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_readme import load_categories  # noqa: E402
from experiences import NOTES_PATH, REPO, check_store, entry_index, load_store  # noqa: E402

SKIPPED_PATH = ROOT / "data" / "experiences" / "skipped.json"
FORM_PATH = ROOT / ".github" / "ISSUE_TEMPLATE" / "share-experience.yml"
ISSUE_TITLE_PREFIX = "[Experience]"
NO_RESPONSE = "_No response_"
API = "https://api.github.com"
MAX_PAGES = 4  # 400 issues scanned; the weekly cadence never gets near it

# The heading each form field renders as, keyed by the name the note uses. A GitHub issue
# form produces "### <label>" sections, so these fragments are the only link between the
# form and the parser. tests/test_sync_experiences.py asserts every one of them is still
# present in the form file, because renaming a label would otherwise stop collection
# silently — the failure mode this module exists to avoid.
FIELDS = {
    "entry_url": "Which resource is this about?",
    "lang": "Language of your write-up",
    "date": "When did you use it?",
    "title": "One line that says what you made",
    "body": "What did you make, and what did you learn?",
    "tags": "Tags (optional",
    "credit_as": "Credit as (optional)",
    "confirmations": "Before you post",
}
LANG_VALUES = {"zh": "zh", "中文": "zh", "chinese": "zh", "en": "en", "english": "en", "英文": "en"}
CHECKED_BOX_RE = re.compile(r"^[ \t]*-[ \t]*\[[xX]\]", re.MULTILINE)
REQUIRED_BOXES = 3
DATE_RE = re.compile(r"^([0-9]{4})[-/.]([0-9]{1,2})(?:[-/.]([0-9]{1,2}))?")
ID_SAFE_RE = re.compile(r"[^a-z0-9]+")


def fail(errors):
    for e in errors:
        print("ERROR: %s" % e, file=sys.stderr)
    sys.exit(1)


def parse_issue_body(body):
    """{field: text} from a rendered issue form, missing keys left out.

    The form's own headings are the only structure GitHub gives us, so this reads them
    back rather than trying to parse a template by position.
    """
    sections, heading = {}, None
    for line in (body or "").splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            sections[heading] = []
        elif heading is not None:
            sections[heading].append(line)
    out = {}
    for field, fragment in FIELDS.items():
        needle = fragment.lower()
        for heading, lines in sections.items():
            if needle in heading.lower():
                value = "\n".join(lines).strip()
                out[field] = "" if value == NO_RESPONSE else value
                break
    return out


def is_submission(issue):
    """A field-note submission, not a resource suggestion and not a pull request.

    The title prefix is what the form ships with; the heading check is there for someone
    who edited it away but still filled the form in.

    The key, not its value: the REST API marks a pull request with a "pull_request"
    object, and testing truthiness would let an empty one through.
    """
    if "pull_request" in issue:
        return False
    if (issue.get("title") or "").strip().startswith(ISSUE_TITLE_PREFIX):
        return True
    return FIELDS["entry_url"].lower() in (issue.get("body") or "").lower()


def confirmations_ticked(value):
    return len(CHECKED_BOX_RE.findall(value or "")) >= REQUIRED_BOXES


def normalize_date(value):
    """YYYY-MM-DD, tolerating YYYY-MM, slashes and dots. None when it cannot be read."""
    match = DATE_RE.match((value or "").strip())
    if not match:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3) or "1"
    return "%s-%02d-%02d" % (year, int(month), int(day))


def normalize_lang(value):
    return LANG_VALUES.get((value or "").strip().lower())


def normalize_tags(value):
    """(tags, dropped). The form asks for a comma-separated list; the store caps at three
    tags of 2-16 characters, so anything outside that is reported rather than stored."""
    tags, dropped = [], []
    for raw in re.split(r"[,，、;；]", value or ""):
        tag = raw.strip().lstrip("#").strip()
        if not tag:
            continue
        if not 2 <= len(tag) <= 16 or "|" in tag:
            dropped.append(tag)
            continue
        if tag in tags:
            continue
        tags.append(tag)
    if len(tags) > 3:
        dropped.extend(tags[3:])
        tags = tags[:3]
    return tags, dropped


def note_id(number, title):
    """A stable, readable slug. The issue number makes it unique even for a CJK title,
    which has no ASCII left to slug."""
    ascii_slug = ID_SAFE_RE.sub("-", (title or "").lower()).strip("-")[:40].strip("-")
    return "issue-%s%s" % (number, ("-" + ascii_slug) if ascii_slug else "")


def submission_to_note(issue, fields, categories, reviewer, today):
    """(note, notes) — a usable record and any warnings, or (None, [reason]).

    The publication rules are not repeated here: the candidate note is judged by
    experiences.check_store(), the same function CI runs, so anything this script accepts
    is something the store's own gate accepts. A rejection reason is that function's
    first error, which is already written for a human.
    """
    if not confirmations_ticked(fields.get("confirmations")):
        return None, ["the three confirmation boxes are not all ticked"]
    when = normalize_date(fields.get("date"))
    if when is None:
        return None, ["the date could not be read (got '%s')" % (fields.get("date") or "").strip()[:40]]
    lang = normalize_lang(fields.get("lang"))
    if lang is None:
        return None, ["the language could not be read (got '%s')" % (fields.get("lang") or "").strip()[:40]]
    author = (fields.get("credit_as") or "").strip().lstrip("@")
    author = author or ((issue.get("user") or {}).get("login") or "")
    tags, dropped = normalize_tags(fields.get("tags"))
    note = {
        "id": note_id(issue.get("number"), fields.get("title")),
        "entry_url": (fields.get("entry_url") or "").strip(),
        "author": author,
        "lang": lang,
        "date": when,
        "title": (fields.get("title") or "").strip(),
        "body": " ".join((fields.get("body") or "").split()),
        "evidence": issue.get("html_url") or "",
        "reviewed_by": reviewer,
        "reviewed_on": today.isoformat(),
    }
    if tags:
        note["tags"] = tags
    errors, _ = check_store({"version": 1, "notes": [note]}, categories, today=today)
    if errors:
        return None, [errors[0]]
    warnings = ["dropped tag(s) outside the 2-16 character rule: %s" % ", ".join(dropped)] if dropped else []
    return note, warnings


def sync(issues, store, categories, reviewer, today, ignored=None):
    """(added, skipped, already) for a list of issues. Pure — no network, no writes.

    added entries are {"note", "issue", "entry_name", "warnings"}; skipped is
    [(issue, reason)]; already is the issues whose evidence is in the store already, so
    re-running the job (or a maintainer merging a hand-written note) is idempotent.
    """
    ignored = ignored or {}
    index = entry_index(categories)
    existing = {n.get("evidence") for n in store.get("notes", [])}
    used_ids = {n.get("id") for n in store.get("notes", [])}
    added, skipped, already = [], [], []
    for issue in issues:
        if not is_submission(issue):
            continue
        url = issue.get("html_url") or ""
        if url in existing:
            already.append(issue)
            continue
        if url in ignored:
            skipped.append((issue, "on the ignore list: %s" % ignored[url]))
            continue
        note, reasons = submission_to_note(issue, parse_issue_body(issue.get("body")),
                                           categories, reviewer, today)
        if note is None:
            skipped.append((issue, reasons[0]))
            continue
        base, suffix = note["id"], 1
        while note["id"] in used_ids:
            suffix += 1
            note["id"] = "%s-%d" % (base, suffix)
        used_ids.add(note["id"])
        hit = index.get(note["entry_url"])
        added.append({"note": note, "issue": issue,
                      "entry_name": hit[1]["name"] if hit else None, "warnings": reasons})
    return added, skipped, already


def check_skipped(data):
    """(errors, {url: reason}). A broken ignore list is an error, not a silent no-op: the
    only reason the file exists is that a rejected submission stays rejected."""
    errors = []
    if not isinstance(data, dict):
        return ["the ignore list must be a JSON object"], {}
    for key in sorted(set(data) - {"version", "skipped"}):
        errors.append("unknown top-level key '%s'" % key)
    if data.get("version") != 1:
        errors.append("version must be 1 (got %r)" % (data.get("version"),))
    entries = data.get("skipped")
    if not isinstance(entries, list):
        return errors + ["'skipped' must be a list"], {}
    out = {}
    for i, entry in enumerate(entries):
        label = "skip #%d" % (i + 1)
        if not isinstance(entry, dict):
            errors.append("%s: must be an object" % label)
            continue
        for key in sorted(set(entry) - {"url", "reason"}):
            errors.append("%s: unknown field '%s'" % (label, key))
        url, reason = entry.get("url"), entry.get("reason")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            errors.append("%s: url must be a github.com thread" % label)
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append("%s: a reason is required" % label)
            continue
        if url in out:
            errors.append("%s: %s is listed twice" % (label, url))
            continue
        out[url] = reason.strip()
    return errors, out


def load_skipped(path=None):
    path = Path(path) if path else SKIPPED_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        fail(["%s: invalid JSON (%s)" % (path, e)])
    errors, out = check_skipped(data)
    if errors:
        fail(["%s: %s" % (path, e) for e in errors])
    return out


def load_issues_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        fail(["%s: %s" % (path, e)])
    if not isinstance(data, list):
        fail(["%s: expected a JSON list of issue objects" % path])
    return data


def fetch_issues(repo, token, pages=MAX_PAGES):
    """Every issue in the repository, newest first, pull requests dropped.

    The list endpoint rather than search: it is not rate-limited the same way, it paginates
    predictably, and a weekly job never needs more than the first pages.
    """
    issues = []
    for page in range(1, pages + 1):
        url = ("%s/repos/%s/issues?state=all&per_page=100&page=%d&sort=created&direction=desc"
               % (API, repo, page))
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": repo,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                batch = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            fail(["GET %s failed: HTTP %s %s" % (url, e.code, e.read().decode("utf-8", "replace")[:200])])
        except urllib.error.URLError as e:
            fail(["GET %s failed: %s" % (url, e.reason)])
        if not isinstance(batch, list):
            fail(["GET %s returned %s, not a list" % (url, type(batch).__name__)])
        issues.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
    return issues


def cell(value):
    """One table cell from user text: titles are already pipe-free, but a newline would
    still break the row, and this report is written before anything else validates it."""
    return " ".join(str(value or "").split()).replace("|", "/")[:120]


def render_report(added, skipped, already, reviewer):
    """The pull request body. It has one job: make the merge a review, not a rubber stamp."""
    lines = []
    a = lines.append
    a("Collects [field-note submissions](.github/ISSUE_TEMPLATE/share-experience.yml) made since the last run.")
    a("")
    if added:
        a("**Merging this pull request publishes the notes below.** Every record in the diff already")
        a("carries reviewed_by @%s, so merge it after reading them: delete a record first to reject it" % reviewer)
        a("and add its issue url to data/experiences/skipped.json, so the next run does not propose it again.")
        a("")
        a("| note | about | issue | experience date |")
        a("| --- | --- | --- | --- |")
        for item in added:
            note, issue = item["note"], item["issue"]
            a("| %s | %s | [#%s](%s) | %s |" % (cell(note["title"]), cell(item.get("entry_name") or note["entry_url"]),
                                                issue.get("number"), note["evidence"], note["date"]))
        for item in added:
            for warning in item.get("warnings") or []:
                a("")
                a("> #%s: %s" % (item["issue"].get("number"), cell(warning)))
        a("")
    else:
        a("No new submissions to publish this week.")
        a("")
    if skipped:
        a("### Left out this week (%d)" % len(skipped))
        a("")
        a("Nothing from these is in the diff — fix the submission on the issue, or add the thread to")
        a("data/experiences/skipped.json if it should never be proposed again.")
        a("")
        for issue, reason in skipped:
            a("- [#%s](%s) — %s" % (issue.get("number"), issue.get("html_url") or "", cell(reason)))
        a("")
    if already:
        a("%d submission(s) already in data/experiences/notes.json were left untouched." % len(already))
        a("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--fetch", action="store_true", help="read issues from the GitHub API (needs GITHUB_TOKEN)")
    source.add_argument("--from-json", help="read a saved API page instead of the network")
    source.add_argument("--check", action="store_true",
                        help="validate the ignore list and the store, then stop (the CI gate)")
    ap.add_argument("--repo", help="owner/repo (default: $GITHUB_REPOSITORY, else the repo in scripts/experiences.py)")
    ap.add_argument("--reviewer", help="handle recorded as reviewed_by (default: the repository owner)")
    ap.add_argument("--notes", default=str(NOTES_PATH), help="store to append to")
    ap.add_argument("--skipped", default=str(SKIPPED_PATH), help="ignore list to respect")
    ap.add_argument("--report", help="write the pull request body here")
    ap.add_argument("--today", help="YYYY-MM-DD override, for tests and dry runs")
    ap.add_argument("--dry-run", action="store_true", help="print the report and write nothing")
    args = ap.parse_args()

    if args.check:
        categories = load_categories()
        store = load_store(args.notes)
        errors, ignored = check_skipped(json.loads(Path(args.skipped).read_text(encoding="utf-8")))
        if errors:
            fail(["%s: %s" % (args.skipped, e) for e in errors])
        errors, _ = check_store(store, categories)
        if errors:
            fail(["%s: %s" % (args.notes, e) for e in errors])
        print("Validated %d field note(s) and %d ignored submission(s)." % (len(store["notes"]), len(ignored)))
        return
    if not (args.fetch or args.from_json):
        fail(["pass --check, --fetch or --from-json"])

    today = date.fromisoformat(args.today) if args.today else date.today()
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or REPO
    reviewer = (args.reviewer or repo.split("/")[0]).lstrip("@")
    categories = load_categories()
    store = load_store(args.notes)
    ignored = load_skipped(args.skipped)

    if args.fetch:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            fail(["--fetch needs GITHUB_TOKEN (or GH_TOKEN) with read access to %s's issues" % repo])
        issues = fetch_issues(repo, token)
    else:
        issues = load_issues_json(args.from_json)

    added, skipped, already = sync(issues, store, categories, reviewer, today, ignored)
    report = render_report(added, skipped, already, reviewer)
    print("Scanned %d issue(s): %d new note(s), %d left out, %d already stored."
          % (len(issues), len(added), len(skipped), len(already)))
    for issue, reason in skipped:
        print("  left out #%s: %s" % (issue.get("number"), reason))
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
    if args.dry_run:
        print()
        print(report)
        return
    if not added:
        return
    store["notes"] = list(store.get("notes", [])) + [item["note"] for item in added]
    errors, _ = check_store(store, categories, today=today)
    if errors:
        fail(errors)
    Path(args.notes).write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Wrote %d note(s) to %s" % (len(added), args.notes))


if __name__ == "__main__":
    main()
