#!/usr/bin/env python3
"""Build README.md from data/*.json and validate every entry.

Usage:
    python3 scripts/build_readme.py                # regenerate README.md
    python3 scripts/build_readme.py --check        # CI mode: fail if README.md is out of sync
    python3 scripts/build_readme.py --validate-only

Data lives in data/<slug>.json; the README is generated, so edit the JSON, not the README.
Stdlib only (Python >= 3.9). License: CC0.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
README_PATH = ROOT / "README.md"

# Order in which categories appear in the README.
CATEGORY_ORDER = [
    "music",
    "sound-effects",
    "stock-footage",
    "stock-photos",
    "fonts",
    "graphics-icons",
    "video-production",
    "mockups-templates",
    "3d-assets",
    "search-engines",
]

REQUIRED_ENTRY_FIELDS = ("name", "url", "description", "license", "attribution", "monetization")
OPTIONAL_ENTRY_FIELDS = ("signup", "notes", "skip_linkcheck")
ATTRIBUTION_VALUES = {"required", "not-required", "varies"}
MONETIZATION_VALUES = {"allowed", "conditional", "varies", "not-allowed"}
SIGNUP_VALUES = {"required", "not-required", "optional", "unknown"}

ATTR_DISPLAY = {
    "required": "\u2705 Required",
    "not-required": "\u2014",
    "varies": "\u2753 Varies",
}
MON_DISPLAY = {
    "allowed": "\U0001f49a Allowed",
    "conditional": "\u26a0\ufe0f Conditional",
    "varies": "\u2753 Varies",
    "not-allowed": "\U0001f6ab Not allowed",
}
SIGNUP_DISPLAY = {
    "required": "\U0001f511 Yes",
    "not-required": "\u2014",
    "optional": "Optional",
    "unknown": "\u2753",
}


def fail(errors):
    for e in errors:
        print("ERROR: %s" % e, file=sys.stderr)
    sys.exit(1)


def load_categories():
    """Load and validate every data file. Returns list of category dicts in CATEGORY_ORDER."""
    errors = []
    warnings = []
    by_slug = {}
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append("%s: invalid JSON (%s)" % (path.name, e))
            continue
        slug = data.get("slug")
        if not slug:
            errors.append("%s: missing 'slug'" % path.name)
            continue
        if slug != path.stem:
            errors.append("%s: slug '%s' does not match filename" % (path.name, slug))
        by_slug[slug] = (path, data)

    missing = [s for s in CATEGORY_ORDER if s not in by_slug]
    if missing:
        errors.append("data files missing for CATEGORY_ORDER slugs: %s" % ", ".join(missing))
    extra = [s for s in by_slug if s not in CATEGORY_ORDER]
    if extra:
        errors.append("data files not listed in CATEGORY_ORDER: %s (add them to scripts/build_readme.py)" % ", ".join(extra))

    seen_urls = {}
    categories = []
    for slug in CATEGORY_ORDER:
        if slug not in by_slug:
            continue
        path, data = by_slug[slug]
        for field in ("category", "slug", "description", "entries"):
            if field not in data:
                errors.append("%s: missing field '%s'" % (path.name, field))
        entries = data.get("entries", [])
        if not isinstance(entries, list) or not entries:
            errors.append("%s: 'entries' must be a non-empty list" % path.name)
            entries = []
        for i, e in enumerate(entries):
            where = "%s: entry #%d (%s)" % (path.name, i + 1, e.get("name", "?"))
            unknown = set(e.keys()) - set(REQUIRED_ENTRY_FIELDS) - set(OPTIONAL_ENTRY_FIELDS)
            if unknown:
                errors.append("%s: unknown field(s) %s" % (where, ", ".join(sorted(unknown))))
            for field in REQUIRED_ENTRY_FIELDS:
                if not e.get(field):
                    errors.append("%s: missing required field '%s'" % (where, field))
            if e.get("attribution") and e["attribution"] not in ATTRIBUTION_VALUES:
                errors.append("%s: attribution '%s' not in %s" % (where, e["attribution"], sorted(ATTRIBUTION_VALUES)))
            if e.get("monetization") and e["monetization"] not in MONETIZATION_VALUES:
                errors.append("%s: monetization '%s' not in %s" % (where, e["monetization"], sorted(MONETIZATION_VALUES)))
            if "signup" in e and e["signup"] not in SIGNUP_VALUES:
                errors.append("%s: signup '%s' not in %s" % (where, e["signup"], sorted(SIGNUP_VALUES)))
            url = e.get("url", "")
            if url:
                if not url.startswith("https://"):
                    errors.append("%s: url must start with https:// (%s)" % (where, url))
                key = url.rstrip("/").lower()
                if key in seen_urls:
                    errors.append("%s: duplicate url also in '%s'" % (where, seen_urls[key]))
                else:
                    seen_urls[key] = e.get("name", "?")
            desc = e.get("description", "")
            if len(desc) > 160:
                warnings.append("%s: description longer than 160 chars (%d)" % (where, len(desc)))
            for field in REQUIRED_ENTRY_FIELDS + OPTIONAL_ENTRY_FIELDS:
                v = e.get(field)
                if isinstance(v, str) and "|" in v:
                    errors.append("%s: field '%s' contains '|' which breaks Markdown tables" % (where, field))
        categories.append(data)

    for w in warnings:
        print("WARNING: %s" % w, file=sys.stderr)
    if errors:
        fail(errors)
    return categories


def slugify(heading):
    """GitHub-style anchor slug for a heading.

    GitHub drops punctuation (keeping - and _), lowercases, and turns EACH
    space into a hyphen without collapsing runs — so 'Graphics & Icons'
    becomes 'graphics--icons'.
    """
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


def entry_row(e):
    name = e["name"].replace("|", "\\|")
    desc = e["description"].replace("|", "\\|")
    if e.get("notes"):
        desc += " _%s_" % e["notes"].replace("|", "\\|")
    signup = SIGNUP_DISPLAY.get(e.get("signup", "unknown"), "\u2753")
    return "| [%s](%s) | %s | %s | %s | %s | %s |" % (
        name,
        e["url"],
        desc,
        e["license"],
        ATTR_DISPLAY[e["attribution"]],
        MON_DISPLAY[e["monetization"]],
        signup,
    )


def render(categories):
    total = sum(len(c["entries"]) for c in categories)
    lines = []
    a = lines.append

    a("<!-- This README is generated by scripts/build_readme.py from data/*.json — edit the JSON, then run the script (CI checks sync). -->")
    a("")
    a("# \U0001f381 Free for Creators")
    a("")
    a("> A curated, license-transparent directory of **truly free** assets for video creators, streamers, podcasters, game devs, musicians and designers: **%d resources across %d categories**." % (total, len(categories)))
    a("")
    a("Every entry answers the three questions that decide whether a \u201cfree\u201d asset is actually free for *you*:")
    a("")
    a("1. **What is the license?**")
    a("2. **Do I have to credit anyone?** \u2192 the *Attribution* column")
    a("3. **Can I use it in monetized content?** \u2192 the *Monetization* column")
    a("")
    a("Unlike plain link lists, this repo is **verified by CI**: a weekly GitHub Action link-checks every URL, and all entries live as structured, machine-readable JSON in [`data/`](data) \u2014 free to reuse (CC0).")
    a("")
    a("> \u26a0\ufe0f **Disclaimer** \u2014 Licenses change and this list is maintained by volunteers. It is not legal advice. Always verify the current license on the source website before publishing, especially for monetized or client work.")
    a("")
    a("## Legend")
    a("")
    a("| Symbol | Meaning |")
    a("| --- | --- |")
    a("| \U0001f49a Allowed | Safe for monetized/commercial content under the listed license |")
    a("| \u26a0\ufe0f Conditional | Allowed only under conditions (credit, free-tier limits, specific platforms) |")
    a("| \u2753 Varies | License differs per item \u2014 check each asset |")
    a("| \U0001f6ab Not allowed | Not free for commercial/monetized use |")
    a("| \u2705 Required | You must credit the author/source |")
    a("| \U0001f511 Yes | An (free) account is needed to download |")
    a("")
    a("## Table of Contents")
    a("")
    for c in categories:
        a("- [%s](#%s) \u2014 %d resources \u00b7 %s" % (c["category"], slugify(c["category"]), len(c["entries"]), c["description"]))
    a("- [Related Lists](#related-lists)")
    a("- [Contributing](#contributing)")
    a("")
    for c in categories:
        a("## %s" % c["category"])
        a("")
        a("*%s*" % c["description"])
        a("")
        a("| Resource | About | License | Attribution | Monetization | Sign-up |")
        a("| --- | --- | --- | --- | --- | --- |")
        for e in c["entries"]:
            a(entry_row(e))
        a("")
    a("## Related Lists")
    a("")
    a("- [public-apis](https://github.com/public-apis/public-apis) \u2014 free APIs for software projects")
    a("- [free-for-dev](https://github.com/ripienaar/free-for-dev) \u2014 free SaaS/PaaS/IaaS tiers for developers")
    a("- [awesome-stock-resources](https://github.com/neutraltone/awesome-stock-resources) \u2014 classic stock photo/video collection")
    a("- [GameDev-Resources](https://github.com/Kavex/GameDev-Resources) \u2014 game development resources")
    a("- [free-font](https://github.com/jaywcjlove/free-font) \u2014 \u4e2d\u82f1\u6587\u53ef\u5546\u7528\u514d\u8d39\u5b57\u4f53 (free CJK fonts)")
    a("- [design-resources-for-developers](https://github.com/bradtraversy/design-resources-for-developers) \u2014 design resources for developers")
    a("")
    a("## Contributing")
    a("")
    a("Found a great free resource? Spot a dead link or an outdated license? Please open a PR or use the issue templates \u2014 see [CONTRIBUTING.md](CONTRIBUTING.md). Entries are added to the JSON files in [`data/`](data); the README is generated automatically.")
    a("")
    a("## License")
    a("")
    a("[![CC0](https://licensebuttons.net/p/zero/1.0/88x31.png)](https://creativecommons.org/publicdomain/zero/1.0/)")
    a("")
    a("To the extent possible under law, the contributors waive all copyright and related rights to this collection under [CC0 1.0](LICENSE). Fork it, mirror it, build on it \u2014 no strings attached.")
    a("")
    a("---")
    a("")
    a("*If this list ever saved you from a copyright claim, consider dropping a \u2b50 \u2014 it helps other creators find it.*")
    a("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if README.md is not in sync with data/")
    ap.add_argument("--validate-only", action="store_true", help="only validate data files")
    args = ap.parse_args()

    categories = load_categories()
    total = sum(len(c["entries"]) for c in categories)
    print("Validated %d categories, %d entries." % (len(categories), total))
    if args.validate_only:
        return

    rendered = render(categories)
    if args.check:
        current = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else None
        if current == rendered:
            print("README.md is in sync.")
            return
        print("ERROR: README.md is out of sync with data/. Run: python3 scripts/build_readme.py", file=sys.stderr)
        if current is None:
            print("(README.md does not exist)", file=sys.stderr)
        sys.exit(1)

    README_PATH.write_text(rendered, encoding="utf-8")
    print("Wrote %s" % README_PATH)


if __name__ == "__main__":
    main()
