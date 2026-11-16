#!/usr/bin/env python3
"""Admission gate for entries: style, coherence and evidence. Stdlib only. License: CC0.

`build_readme.py --validate-only` proves the data matches the schema. This script proves
the data is *good*, which the schema cannot express: that a description does not lean on
words that mean nothing, that a claim of "conditional" monetization is actually explained
somewhere, that a URL is canonical and unique, and — for a new entry — that somebody read
the licence rather than assuming it.

It exists because the rules were being rediscovered by hand. A prose checklist in
CONTRIBUTING describes the loop; this makes it executable, so a submission can be judged
the same way every time and a regression cannot land quietly. Seventeen entries added in
one week reintroduced four unverifiable quantity claims and one over-long note, all of
which this would have caught before review.

Three modes:

    python3 scripts/check_entry.py --all              audit every entry in data/
    python3 scripts/check_entry.py --candidates c.json  gate new entries, evidence required
    python3 scripts/check_entry.py --all --offline    skip the live link check
    python3 scripts/check_entry.py --license-audit    which entries' licence wording is
                                                      unverified, or older than a year

The licence audit fails only on entries that *carry* a check date older than the budget.
Entries with no date are reported as a backlog rather than a failure: they predate the
field, and inventing a date for them would be worse than admitting there is none.

Candidate file format (a JSON array):

    [{"file": "fonts.json",
      "entry": {"name": "...", "url": "https://...", ...},
      "evidence": {"quote": "verbatim licence text, >= 20 chars",
                   "source": "https://page-the-quote-came-from",
                   "signup_how": "how the sign-up value was established"}}]

`evidence` is required for candidates and ignored by --all, because existing entries
predate the requirement and re-deriving it for all of them is a separate project.

Exit code is 1 on any error, so this can gate CI.
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_readme import (  # noqa: E402  (one contract, read from the schema)
    MAX_DESCRIPTION,
    OPTIONAL_ENTRY_FIELDS,
    REQUIRED_ENTRY_FIELDS,
    SCHEMA,
    TRACKING_PARAMS,
    url_key,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

ENTRY_PROPS = SCHEMA["definitions"]["entry"]["properties"]
ENUMS = {k: set(v["enum"]) for k, v in ENTRY_PROPS.items() if "enum" in v}
MAXLEN = {k: v["maxLength"] for k, v in ENTRY_PROPS.items() if "maxLength" in v}
KNOWN_FIELDS = set(REQUIRED_ENTRY_FIELDS) | set(OPTIONAL_ENTRY_FIELDS)

# Words that assert quality without saying anything checkable. Two deliberate exclusions:
# "seamless" is a term of art in texturing (it means tileable), and "premium" is usually
# part of a product name such as Flaticon Premium. Neither is doing rhetorical work.
FLUFF = re.compile(
    r"\b(curated|hand-?picked|huge|massive|enormous|vast|stunning|gorgeous|amazing|"
    r"incredible|ultimate|perfect|best|finest|world-?class|top-?notch|effortless|"
    r"one-?stop|everything you need|look no further|game-?changing|cutting-?edge|"
    r"state-of-the-art|unparalleled|premium quality)\b",
    re.IGNORECASE,
)
FLUFF_ALLOW = {"seamless", "premium"}

# A quantity nobody can check and that no reader can act on. Floored exact figures
# ("2,000+", "100M+") are fine: they stay true as a library grows and can be verified
# against the source. "Thousands of" is neither.
VAGUE_QUANTITY = re.compile(r"\b(thousands|millions|hundreds|dozens|tens of thousands)\s+of\b",
                            re.IGNORECASE)
VAGUE_QUANTITY_ZH = re.compile(r"(数千|数万|数十万|数百万|上千万|大量|海量|无数)(?=[个张首条件款套幅])")
# Chinese intensifiers that claim size without a checkable number, the counterpart of
# "huge" and "enormous" above.
VAGUE_SIZE_ZH = re.compile(r"(超大|超多|超全|超丰富|极多|极其丰富|巨量|海量)")

# Fields whose wording is ours. `license` is excluded on purpose: it often quotes the
# source's own name for its terms ("Flaticon Free License", "Pixabay Content License"), and
# rewriting a quoted name to satisfy a style rule would make it stop matching the source.
TEXT_FIELDS = ("description", "description_zh", "notes", "notes_zh", "name")


def check_length_and_chars(entry, where, errors, warnings):
    """Field lengths from the schema, and characters that would break the Markdown tables."""
    for field, value in entry.items():
        if not isinstance(value, str):
            continue
        limit = MAXLEN.get(field)
        if limit and len(value) > limit:
            errors.append("%s: %s is %d chars, schema allows %d"
                          % (where, field, len(value), limit))
        if field in ("description", "description_zh") and len(value) > MAX_DESCRIPTION:
            errors.append("%s: %s is %d chars, limit %d" % (where, field, len(value), MAX_DESCRIPTION))
        if "|" in value:
            errors.append("%s: %s contains '|', which breaks the generated Markdown tables"
                          % (where, field))
        if value != value.strip():
            warnings.append("%s: %s has leading or trailing whitespace" % (where, field))


def check_voice(entry, where, errors):
    """No unverifiable superlatives, no unverifiable quantities."""
    for field in TEXT_FIELDS:
        value = entry.get(field) or ""
        for word in FLUFF.findall(value):
            if word.lower() not in FLUFF_ALLOW:
                errors.append('%s: %s uses "%s", which asserts quality without evidence'
                              % (where, field, word))
        m = VAGUE_QUANTITY.search(value)
        if m:
            errors.append('%s: %s says "%s" — an unverifiable quantity; state the licence '
                          "or the source instead" % (where, field, m.group(0)))
        m = VAGUE_QUANTITY_ZH.search(value)
        if m:
            errors.append('%s: %s says "%s" — same rule as the English vague quantity'
                          % (where, field, m.group(0)))
        m = VAGUE_SIZE_ZH.search(value)
        if m:
            errors.append('%s: %s says "%s" — an unverifiable size claim' % (where, field, m.group(0)))


def check_coherence(entry, where, errors, warnings):
    """Cross-field rules the schema cannot express."""
    fam = entry.get("license_family")
    mon = entry.get("monetization")
    attr = entry.get("attribution")
    notes = entry.get("notes")

    for field in ("license_family", "monetization", "attribution", "signup"):
        if field in entry and entry[field] not in ENUMS.get(field, {entry[field]}):
            errors.append("%s: %s=%r is not in the schema enum %s"
                          % (where, field, entry[field], sorted(ENUMS.get(field, []))))

    if mon == "conditional" and not notes and attr == "not-required":
        # The condition has to be discoverable somewhere. Attribution being required is
        # itself a condition a reader can see, so that satisfies the rule; "conditional"
        # plus "no attribution" plus silence does not, because then nobody can tell what
        # the entry is conditional on.
        errors.append("%s: monetization is 'conditional' but nothing records the condition — "
                      "no notes, and attribution is 'not-required'" % where)
    if fam == "noncommercial" and mon != "not-allowed":
        errors.append("%s: license_family is 'noncommercial' but monetization is %r; "
                      "a non-commercial licence cannot permit commercial use" % (where, mon))
    if entry.get("contains_cc0") and fam != "mixed":
        errors.append("%s: contains_cc0 is true but license_family is %r; a site that mixes "
                      "CC0 items in with others is 'mixed' by definition" % (where, fam))
    if fam == "cc0" and attr == "required":
        errors.append("%s: license_family is 'cc0' but attribution is 'required'; CC0 waives "
                      "attribution, so one of the two is wrong" % where)
    if fam == "cc0" and mon not in ("allowed", None):
        warnings.append("%s: license_family is 'cc0' but monetization is %r — worth a second "
                        "look" % (where, mon))
    # Translation gaps are warnings, not errors: the source of every Chinese string here is
    # a human writing it, and failing the build over one would trade a slightly weaker
    # Chinese page for a contributor who cannot merge at all. The cost is real but bounded —
    # the row renders in English, and site/index.html's search index stops matching Chinese
    # keywords for that entry.
    if not entry.get("description_zh"):
        warnings.append("%s: no description_zh — that row renders in English on the Chinese "
                        "page and cannot be found by a Chinese search" % where)
    if entry.get("notes") and not entry.get("notes_zh"):
        warnings.append("%s: has English notes but no notes_zh" % where)
    check_provenance(entry, where, errors)


LICENSE_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def check_provenance(entry, where, errors):
    """A licence check date and the page it came from travel together.

    The weekly link check proves a URL resolves. Nothing proves the licence still says what
    the entry claims it says — that takes a human reading the page, and a date is only worth
    anything if it says *when* and *where*. So the two fields are all-or-nothing, and a date
    in the future is a typo rather than a check.
    """
    checked = entry.get("license_checked")
    source = entry.get("license_source")
    if bool(checked) != bool(source):
        absent = "license_source" if checked else "license_checked"
        errors.append("%s: %s is set but %s is not — a date with no page behind it, or a "
                      "page with no date, cannot be re-checked later"
                      % (where, "license_checked" if checked else "license_source", absent))
        return
    if not checked:
        return
    if not LICENSE_DATE.match(str(checked)):
        errors.append("%s: license_checked %r is not YYYY-MM-DD" % (where, checked))
        return
    try:
        when = datetime.date.fromisoformat(str(checked))
    except ValueError:
        errors.append("%s: license_checked %r is not a real calendar date" % (where, checked))
        return
    if when > datetime.date.today():
        errors.append("%s: license_checked %s is in the future" % (where, checked))
    if not str(source).startswith("https://"):
        errors.append("%s: license_source must be https:// (got %r)" % (where, str(source)[:60]))


def check_url(entry, where, errors, seen_urls, seen_names):
    """Canonical, untracked, unique."""
    url = entry.get("url") or ""
    name = entry.get("name") or ""
    if not url.startswith("https://"):
        errors.append("%s: url must be https (got %r)" % (where, url[:60]))
    if "#" in url:
        errors.append("%s: url contains a fragment; link the resource, not a position in it" % where)
    if "?" in url:
        query = url.split("?", 1)[1]
        params = [p.split("=", 1)[0].lower() for p in query.split("&") if p]
        tracked = [p for p in params if p in TRACKING_PARAMS]
        if tracked:
            errors.append("%s: url carries tracking parameter(s) %s" % (where, tracked))
    key = url_key(url) if url.startswith("http") else url
    if key in seen_urls:
        errors.append("%s: url %s duplicates the existing entry %r" % (where, url, seen_urls[key]))
    seen_urls[key] = name
    low = name.strip().lower()
    if low in seen_names:
        errors.append("%s: name %r duplicates an existing entry" % (where, name))
    seen_names.add(low)


def check_live(url, where, errors, warnings, expect_js=False):
    """The URL must resolve to something that is not definitively dead."""
    try:
        from linkcheck import check_one
    except ImportError as exc:
        warnings.append("%s: cannot import linkcheck (%s); skipping the live check" % (where, exc))
        return
    try:
        status, detail = check_one(url, expect_js)
    except Exception as exc:  # a crash in the checker is not a verdict about the resource
        warnings.append("%s: link check crashed (%s: %s)" % (where, type(exc).__name__, exc))
        return
    if status == "broken":
        errors.append("%s: link check says broken — %s" % (where, detail))
    elif status == "blocked":
        warnings.append("%s: link check says blocked (%s); confirm in a real browser before "
                        "shipping" % (where, detail))


def check_evidence(cand, where, errors):
    """A new entry must carry the licence text somebody actually read."""
    ev = cand.get("evidence") or {}
    quote = (ev.get("quote") or "").strip()
    source = (ev.get("source") or "").strip()
    how = (ev.get("signup_how") or "").strip()
    if len(quote) < 20:
        errors.append("%s: evidence.quote is missing or under 20 chars. A licence claim needs "
                      "the wording it came from, not a paraphrase." % where)
    if not source.startswith("http"):
        errors.append("%s: evidence.source must be the http(s) URL the quote was read at" % where)
    signup = (cand.get("entry") or {}).get("signup")
    if signup and signup != "unknown" and not how:
        errors.append("%s: signup is %r but evidence.signup_how is empty. Say how it was "
                      "established (an anonymous download, a first-party statement, a "
                      "click-through) or set signup to \"unknown\"." % (where, signup))
    entry = cand.get("entry") or {}
    if not entry.get("license_checked") or not entry.get("license_source"):
        errors.append("%s: a new entry must carry license_checked (the date you read the "
                      "licence) and license_source (the page you read it at), so the next "
                      "person can re-check the claim instead of re-deriving it" % where)


def load_existing():
    """Return (entries, seen_urls, seen_names) for everything already in data/."""
    entries, seen_urls, seen_names = [], {}, set()
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for entry in doc.get("entries", []):
            entry["_file"] = os.path.basename(path)
            entries.append(entry)
            seen_urls[url_key(entry.get("url", ""))] = entry.get("name", "?")
            seen_names.add((entry.get("name") or "").strip().lower())
    return entries, seen_urls, seen_names


def licence_risk(entry):
    """How much a wrong licence claim on this entry would cost, 0-4, with the reason.

    The backlog is 190 entries and nobody is going to read them in one sitting, so the useful
    question is not "how many are left" but "which of these is a creator most likely to have
    relied on already". An entry that promises monetized use and no attribution is exactly
    that: the reader has no reason to go and check. One that says "conditional, credit me" has
    already sent them to the source.
    """
    score, reasons = 0, []
    if entry.get("monetization") == "allowed":
        score += 2
        reasons.append("claims monetized use")
    if entry.get("attribution") == "not-required":
        score += 1
        reasons.append("claims no attribution needed")
    if entry.get("license_family") == "cc0":
        score += 1
        reasons.append("claims CC0")
    return score, ", ".join(reasons) or "no strong claim"


def license_audit(max_age_days):
    """Return (total, checked, missing, stale) for the licence check dates in data/.

    `missing` is the honest backlog: entries that predate the field, whose licence wording
    nobody has recorded reading. It is ordered by licence_risk, so the first thing printed is
    the claim that would hurt most if it were wrong. `stale` is the actionable set — a date
    that has aged past the budget, which means somebody claimed to have read the source and
    that claim is now old enough that the source may have changed under it.
    """
    existing, _, _ = load_existing()
    today = datetime.date.today()
    checked, missing, stale = 0, [], []
    for entry in existing:
        where = "%s/%s" % (entry.get("_file", "?"), entry.get("name", "?"))
        raw = entry.get("license_checked")
        if not raw:
            score, reason = licence_risk(entry)
            missing.append((score, where, reason))
            continue
        try:
            when = datetime.date.fromisoformat(str(raw))
        except ValueError:
            stale.append((where, str(raw), "unreadable date"))
            continue
        checked += 1
        age = (today - when).days
        if age > max_age_days:
            stale.append((where, str(raw), "%d days old" % age))
    missing.sort(key=lambda row: (-row[0], row[1]))
    return len(existing), checked, missing, stale


def report_license_audit(max_age_days, show_all=False):
    total, checked, missing, stale = license_audit(max_age_days)
    print("Licence provenance: %d of %d entries carry a check date; %d are an unverified "
          "backlog." % (checked, total, len(missing)))
    if stale:
        print("")
        print("Older than %d days — re-read the source, then update license_checked:" % max_age_days)
        for where, when, age in stale:
            print("  %-58s checked %s  (%s)" % (where[:58], when, age))
    else:
        print("No checked entry is older than %d days." % max_age_days)
    if missing:
        print("")
        print("Unverified: %d entries carry no check date. Nobody is going to read them in one "
              "sitting, so they are ordered by what a wrong claim would cost — the ones that "
              "promise monetized use and no attribution are the ones a reader has no reason to "
              "verify themselves:" % len(missing))
        for score, where, reason in (missing if show_all else missing[:12]):
            print("  [risk %d] %-52s %s" % (score, where[:52], reason))
        if not show_all and len(missing) > 12:
            print("  … and %d more, in the same order (add --all-unverified to print every one)"
                  % (len(missing) - 12))
    print("")
    print("%d stale, %d unverified." % (len(stale), len(missing)))
    return 1 if stale else 0


def audit_all(offline):
    entries, seen_urls, seen_names = [], {}, set()
    existing, _, _ = load_existing()
    errors, warnings = [], []
    for entry in existing:
        where = "%s/%s" % (entry["_file"], entry.get("name", "?"))
        unknown = set(entry) - KNOWN_FIELDS - {"_file"}
        if unknown:
            errors.append("%s: unknown field(s) %s" % (where, sorted(unknown)))
        missing = [f for f in REQUIRED_ENTRY_FIELDS if not entry.get(f)]
        if missing:
            errors.append("%s: missing required field(s) %s" % (where, missing))
        check_length_and_chars(entry, where, errors, warnings)
        check_voice(entry, where, errors)
        check_coherence(entry, where, errors, warnings)
        # Rebuild the dedup maps as we go so a duplicate is reported once, against the
        # later of the two entries.
        check_url(entry, where, errors, seen_urls, seen_names)
        if not offline:
            check_live(entry.get("url", ""), where, errors, warnings,
                       bool(entry.get("linkcheck_js")))
        entries.append(entry)
    return entries, errors, warnings


def gate_candidates(path, offline):
    with open(path, encoding="utf-8") as fh:
        cands = json.load(fh)
    if not isinstance(cands, list):
        return [], ["%s: expected a JSON array of candidates" % path], []
    _, seen_urls, seen_names = load_existing()
    errors, warnings, accepted = [], [], []
    for i, cand in enumerate(cands):
        entry = cand.get("entry") or {}
        target = cand.get("file") or "?"
        where = "candidate[%d] %s/%s" % (i, target, entry.get("name", "?"))
        unknown = set(entry) - KNOWN_FIELDS
        if unknown:
            errors.append("%s: unknown field(s) %s" % (where, sorted(unknown)))
        missing = [f for f in REQUIRED_ENTRY_FIELDS if not entry.get(f)]
        if missing:
            errors.append("%s: missing required field(s) %s" % (where, missing))
        if target != "?" and not os.path.exists(os.path.join(DATA_DIR, target)):
            errors.append("%s: no such category file data/%s" % (where, target))
        check_length_and_chars(entry, where, errors, warnings)
        check_voice(entry, where, errors)
        check_coherence(entry, where, errors, warnings)
        check_url(entry, where, errors, seen_urls, seen_names)
        check_evidence(cand, where, errors)
        if not offline and entry.get("url"):
            check_live(entry["url"], where, errors, warnings, bool(entry.get("linkcheck_js")))
        accepted.append(cand)
    return accepted, errors, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="audit every entry in data/")
    ap.add_argument("--candidates", metavar="FILE", help="gate a candidate JSON file")
    ap.add_argument("--offline", action="store_true", help="skip the live link check")
    ap.add_argument("--quiet", action="store_true", help="only print a summary line")
    ap.add_argument("--license-audit", action="store_true",
                    help="report which entries' licence wording is unverified or too old to trust")
    ap.add_argument("--max-age-days", type=int, default=365, metavar="N",
                    help="staleness budget for --license-audit (default 365)")
    ap.add_argument("--all-unverified", action="store_true",
                    help="with --license-audit, print every entry that lacks a check date "
                         "instead of the twelve highest-stakes ones")
    args = ap.parse_args()

    if args.license_audit:
        return report_license_audit(args.max_age_days, args.all_unverified)

    if not args.all and not args.candidates:
        args.all = True

    if args.candidates:
        label = "candidates from %s" % args.candidates
        items, errors, warnings = gate_candidates(args.candidates, args.offline)
        count = len(items)
    else:
        label = "every entry in data/"
        items, errors, warnings = audit_all(args.offline)
        count = len(items)

    if not args.quiet:
        for w in warnings:
            print("  warn  %s" % w)
        for e in errors:
            print("  ERROR %s" % e)
    mode = "offline" if args.offline else "with live link check"
    print("%d entries checked in %s (%s): %d error(s), %d warning(s)"
          % (count, label, mode, len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
