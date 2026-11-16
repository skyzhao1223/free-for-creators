#!/usr/bin/env python3
"""Credit the people whose pull requests actually landed, automatically.

The README's Contributors section is the project's only credits list, and it used to be
derived from the submitted_by field alone — which meant a merged pull request earned a
credit only if its author, or the maintainer landing it, remembered to write the field in.
This script removes that memory requirement: it reads the repository's merged pull requests,
records each non-bot author once in contributors.json together with the pull request that
first earned the credit, and build_readme.py renders the union of that file and whatever
submitted_by values still exist.

submitted_by does not go away. A suggestion that arrives as an issue and is landed by a
maintainer never has a pull request authored by the suggester, so the field remains the only
way to credit that person next to the entry they suggested.

Usage:
    python3 scripts/sync_contributors.py --fetch                  # ask the GitHub API
    python3 scripts/sync_contributors.py --from-json pulls.json   # offline: a saved API page
    python3 scripts/sync_contributors.py --check                  # the CI gate
    python3 scripts/sync_contributors.py --fetch --dry-run

Env:
    GITHUB_TOKEN / GH_TOKEN   read access to this repository's pull requests (--fetch)

Stdlib only (Python >= 3.9). License: CC0.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRIBUTORS_PATH = ROOT / "contributors.json"
DATA_DIR = ROOT / "data"

# Mirrors build_readme.REPO rather than importing it: build_readme imports this module to
# render the credits, so a top-level import back would be a cycle. tests assert equality.
REPO = "skyzhao1223/free-for-creators"
API = "https://api.github.com"
BOT_SUFFIX = "[bot]"  # GitHub's marker for app accounts (dependabot[bot], github-actions[bot])
MAX_PAGES = 10  # up to 1000 closed pull requests; this repository will not reach that soon
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
PULL_RE = re.compile(r"^https://github\.com/%s/pull/[0-9]+$" % re.escape(REPO))
FIELDS = ("handle", "first", "url")


def fail(errors):
    for e in errors:
        print("ERROR: %s" % e, file=sys.stderr)
    sys.exit(1)


def parse_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def check_contributors(data, today=None):
    """(errors, entries). The file is generated and committed, so a malformed one is a CI
    failure rather than something to paper over: a wrong credit is worse than a missing one."""
    today = today or date.today()
    if not isinstance(data, dict):
        return ["the credits file must be a JSON object"], []
    errors = []
    for key in sorted(set(data) - {"version", "contributors"}):
        errors.append("unknown top-level key '%s'" % key)
    if data.get("version") != 1:
        errors.append("version must be 1 (got %r)" % (data.get("version"),))
    raw = data.get("contributors")
    if not isinstance(raw, list):
        return errors + ["'contributors' must be a list"], []
    entries, seen = [], set()
    for i, entry in enumerate(raw):
        label = "contributor #%d" % (i + 1)
        if not isinstance(entry, dict):
            errors.append("%s: must be an object" % label)
            continue
        bad = False
        for key in sorted(set(entry) - set(FIELDS)):
            errors.append("%s: unknown field '%s'" % (label, key))
            bad = True
        for key in FIELDS:
            if not entry.get(key):
                errors.append("%s: missing '%s'" % (label, key))
                bad = True
        handle = entry.get("handle")
        if handle:
            if isinstance(handle, str) and handle.endswith(BOT_SUFFIX):
                errors.append("%s: '%s' is an app account, not a contributor" % (label, handle))
                bad = True
            elif not isinstance(handle, str) or not HANDLE_RE.match(handle):
                errors.append("%s: handle is not a bare GitHub username" % label)
                bad = True
            elif handle.lower() in seen:
                errors.append("%s: '%s' is credited twice" % (label, handle))
                bad = True
            else:
                seen.add(handle.lower())
        first = entry.get("first")
        when = parse_date(first)
        if first and when is None:
            errors.append("%s: first must be a real YYYY-MM-DD date" % label)
            bad = True
        elif when and when > today:
            errors.append("%s: first %s is in the future" % (label, first))
            bad = True
        url = entry.get("url")
        if url and (not isinstance(url, str) or not PULL_RE.match(url)):
            errors.append("%s: url must be the pull request that earned the credit (github.com/%s/pull/N)" % (label, REPO))
            bad = True
        if not bad:
            entries.append({"handle": handle, "first": first, "url": url})
    return errors, entries


def load_contributors(path=None):
    """The credited pull-request authors. A checkout without the file simply has none yet."""
    path = Path(path) if path else CONTRIBUTORS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        fail(["%s: invalid JSON (%s)" % (path, e)])
    errors, entries = check_contributors(data)
    if errors:
        fail(["%s: %s" % (path, e) for e in errors])
    return entries


def handles(entries):
    return [e["handle"] for e in entries]


def fetch_merged_pulls(repo, token, pages=MAX_PAGES):
    """Every closed pull request, newest activity first. The list endpoint, not search: it
    paginates predictably and a sync never needs more than the first pages."""
    pulls = []
    for page in range(1, pages + 1):
        url = ("%s/repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=%d"
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
        pulls.extend(p for p in batch if p.get("merged_at"))
        if len(batch) < 100:
            break
    return pulls


def eligible_author(pull, exclude=()):
    """The login this pull request credits, or None when it credits nobody.

    One place decides it, so the Credits list and the entry stamp can never disagree about
    who a pull request belongs to: it has to be merged, it must have an author, that author
    must not be an app account, and it must not be on the exclude list (the repository owner
    by default — a maintainer is not a contributor to their own list).
    """
    if not pull.get("merged_at"):
        return None
    login = ((pull.get("user") or {}).get("login") or "").strip()
    if not login or login.endswith(BOT_SUFFIX):
        return None
    if login.lower() in {h.lstrip("@").lower() for h in exclude}:
        return None
    return login


def credit_from_pulls(pulls, exclude=()):
    """[{handle, first, url}] in merge order, bots and the excluded handles left out.

    The pull request is the evidence: the file keeps the url that first earned each credit,
    so a reader can check it and a mistaken credit can be traced back to its cause.
    """
    out, seen = [], set()
    for pull in sorted(pulls, key=lambda p: (p.get("merged_at") or "", p.get("number") or 0)):
        login = eligible_author(pull, exclude)
        if not login or login.lower() in seen:
            continue
        seen.add(login.lower())
        out.append({"handle": login, "first": (pull.get("merged_at") or "")[:10], "url": pull.get("html_url") or ""})
    return out


def merge_contributors(existing, found):
    """(merged, added) — existing credits are never rewritten, new authors are appended in
    merge order, and the result is sorted by first credit then handle so the committed file
    only ever changes on the lines that actually changed."""
    merged = [dict(e) for e in existing]
    seen = {e["handle"].lower() for e in merged}
    added = []
    for entry in found:
        if entry["handle"].lower() in seen:
            continue
        seen.add(entry["handle"].lower())
        merged.append(dict(entry))
        added.append(entry)
    merged.sort(key=lambda e: (e["first"], e["handle"].lower()))
    return merged, added


def render_file(entries):
    return json.dumps({"version": 1, "contributors": entries}, ensure_ascii=False, indent=2) + "\n"


def load_pulls_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        fail(["%s: %s" % (path, e)])
    if not isinstance(data, list):
        fail(["%s: expected a JSON list of pull request objects" % path])
    return data


def entries_added_by(merge_sha):
    """The entries a pull request added: resources that are not in its first parent at all.

    The merge commit is the only ref guaranteed to exist here for a fork's pull request, and
    for a squash merge (this repository's habit) it is exactly the pull request's change; a
    merge commit diffs the same way against its first parent. A rebase-merged pull request
    with several commits would only show its last one, and a ref that is not in this checkout
    credits nobody rather than guessing.

    Membership is decided on the resource, not on the diff shape: check_pr.describe_changes()
    reports a correction as an add+remove pair once a commit touches more than one entry, so
    using it here would credit the author of a licence-date fix as a suggester. Resource
    identity is check_pr.same_resource() — the same name or the same url.
    """
    import check_pr  # local: check_pr imports build_readme, which imports this module
    try:
        before = check_pr.categories_at(merge_sha + "^")
        after = check_pr.categories_at(merge_sha)
    except check_pr.ContractError:
        return []
    return resources_added(before, after)


def resources_added(before, after):
    """Entries in the after-tree whose resource was not in the before-tree at all.

    before/after are {filename: document}, as check_pr.categories_at() returns them. Identity
    is check_pr.same_resource() — the same name or the same url — so an entry that merely
    changed (a licence date, a description, a corrected url) is not credited to whoever
    touched it last.
    """
    import check_pr  # local: check_pr imports build_readme, which imports this module
    old = [e for doc in before.values() for e in doc.get("entries", [])]
    return [e for doc in after.values() for e in doc.get("entries", [])
            if not any(check_pr.same_resource(e, o) for o in old)]


def plan_stamps(pulls, added, exclude=()):
    """[(handle, entry, pull)] in merge order: who to write beside which entry.

    Earliest pull request wins when two of them added the same url, a bot or an excluded
    author is skipped, and a correction is not a suggestion — describe_changes() only reports
    an add when an entry appears that was not there before.
    """
    from build_readme import url_key  # local: build_readme imports this module
    seen, plan = set(), []
    for pull in sorted(pulls, key=lambda p: (p.get("merged_at") or "", p.get("number") or 0)):
        login = eligible_author(pull, exclude)
        if not login:
            continue
        for entry in added.get(pull.get("merge_commit_sha") or "", []):
            url = entry.get("url") or ""
            # url_key("") is "/" — every url-less row would collide on it, so the empty case
            # is checked before the normalisation, not after.
            if not url:
                continue
            key = url_key(url)
            if key in seen:
                continue
            seen.add(key)
            plan.append((login, entry, pull))
    return plan


def apply_stamps(plan, data_dir=None):
    """Write submitted_by onto the entries a plan names; returns what was written.

    Rewriting a whole data file is safe only because every file in data/ round-trips through
    json.dumps(indent=2, ensure_ascii=False) byte for byte — tests assert that, so a commit
    here touches the stamped entry and nothing else. An entry that already carries a
    submitted_by is left alone: a maintainer's correction outranks the bot's inference.
    """
    from build_readme import url_key  # local: build_readme imports this module
    data_dir = Path(data_dir) if data_dir else DATA_DIR
    wanted = {}
    for handle, entry, pull in plan:
        wanted.setdefault(url_key(entry.get("url") or ""), (handle, pull))
    applied = []
    for path in sorted(data_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        touched = False
        for entry in doc.get("entries", []):
            url = entry.get("url") or ""
            if not url:
                continue
            hit = wanted.get(url_key(url))
            if not hit or entry.get("submitted_by"):
                continue
            handle, pull = hit
            entry["submitted_by"] = handle
            touched = True
            applied.append({"handle": handle, "name": entry.get("name"), "url": entry.get("url"),
                            "file": path.name, "number": pull.get("number"),
                            "pull": pull.get("html_url") or ""})
        if touched:
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return applied


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--fetch", action="store_true", help="read merged pull requests from the GitHub API")
    source.add_argument("--from-json", help="read a saved API page instead of the network")
    source.add_argument("--check", action="store_true", help="validate the credits file and stop (the CI gate)")
    ap.add_argument("--repo", help="owner/repo (default: $GITHUB_REPOSITORY, else the repo in this file)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="handle to leave out (repeatable; default: the repository owner)")
    ap.add_argument("--path", default=str(CONTRIBUTORS_PATH), help="credits file to update")
    ap.add_argument("--stamp", action="store_true",
                    help="also write submitted_by onto the entries these pull requests added")
    ap.add_argument("--today", help="YYYY-MM-DD override, for tests and dry runs")
    ap.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or REPO
    exclude = list(args.exclude) or [repo.split("/")[0]]

    if args.check:
        path = Path(args.path)
        if not path.exists():
            fail(["%s does not exist — run the sync, or restore it from git" % path])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            fail(["%s: invalid JSON (%s)" % (path, e)])
        errors, entries = check_contributors(data, today=today)
        if errors:
            fail(["%s: %s" % (path, e) for e in errors])
        print("Validated %d credited pull request author(s)." % len(entries))
        return
    if not (args.fetch or args.from_json):
        fail(["pass --check, --fetch or --from-json"])

    existing = load_contributors(args.path)
    if args.fetch:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            fail(["--fetch needs GITHUB_TOKEN (or GH_TOKEN) with read access to %s's pull requests" % repo])
        pulls = fetch_merged_pulls(repo, token)
    else:
        pulls = load_pulls_json(args.from_json)

    found = credit_from_pulls(pulls, exclude=exclude)
    merged, added = merge_contributors(existing, found)
    print("Scanned %d merged pull request(s): %d author(s), %d newly credited."
          % (len(pulls), len(found), len(added)))
    for entry in added:
        print("  + @%s (%s, %s)" % (entry["handle"], entry["first"], entry["url"]))

    plan = []
    if args.stamp:
        # The entries each pull request added, from the merge commit that carries them. This
        # is what puts a name beside the entry, not just in the list at the bottom.
        by_sha = {}
        for pull in pulls:
            sha = pull.get("merge_commit_sha") or ""
            if not sha or sha in by_sha or not eligible_author(pull, exclude):
                continue
            by_sha[sha] = entries_added_by(sha)
        plan = plan_stamps(pulls, by_sha, exclude=exclude)
        print("Entry credits: %d entr(y/ies) would gain a submitted_by." % len(plan))
        for handle, entry, pull in plan:
            print("  ~ @%s beside %s (#%s)" % (handle, entry.get("name"), pull.get("number")))

    if args.dry_run:
        return

    if added:
        errors, _ = check_contributors({"version": 1, "contributors": merged}, today=today)
        if errors:
            fail(errors)
        Path(args.path).write_text(render_file(merged), encoding="utf-8")
        print("Wrote %d credit(s) to %s" % (len(merged), args.path))
    if plan:
        applied = apply_stamps(plan)
        print("Wrote submitted_by on %d entr(y/ies) under %s." % (len(applied), DATA_DIR))
        for item in applied:
            print("  ~ @%s beside %s (%s)" % (item["handle"], item["name"], item["file"]))


if __name__ == "__main__":
    main()
