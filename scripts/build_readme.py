#!/usr/bin/env python3
"""Build README.md (English) and README.zh-CN.md (Chinese) from data/*.json,
and validate every entry.

Usage:
    python3 scripts/build_readme.py                # regenerate both READMEs
    python3 scripts/build_readme.py --check        # CI mode: fail if any README is out of sync
    python3 scripts/build_readme.py --validate-only

Data lives in data/<slug>.json; the READMEs are generated, so edit the JSON, not the Markdown.
Chinese rendering uses the optional *_zh fields and falls back to English when absent.
Stdlib only (Python >= 3.9). License: CC0.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUTS = {
    "en": ROOT / "README.md",
    "zh": ROOT / "README.zh-CN.md",
}

# Used for CI badges and the Star History embed.
# NOTE: update this (and .github/ISSUE_TEMPLATE/config.yml) if the repo lives elsewhere.
REPO = "skyzhao1223/free-for-creators"
PAGES_URL = "https://skyzhao1223.github.io/free-for-creators/"

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
OPTIONAL_ENTRY_FIELDS = ("signup", "notes", "skip_linkcheck", "description_zh", "notes_zh")
ATTRIBUTION_VALUES = {"required", "not-required", "varies"}
MONETIZATION_VALUES = {"allowed", "conditional", "varies", "not-allowed"}
SIGNUP_VALUES = {"required", "not-required", "optional", "unknown"}

ATTR_DISPLAY = {
    "en": {"required": "\u2705 Required", "not-required": "\u2014", "varies": "\u2753 Varies"},
    "zh": {"required": "\u2705 \u9700\u7f72\u540d", "not-required": "\u2014", "varies": "\u2753 \u9010\u9879\u800c\u5b9a"},
}
MON_DISPLAY = {
    "en": {
        "allowed": "\U0001f49a Allowed",
        "conditional": "\u26a0\ufe0f Conditional",
        "varies": "\u2753 Varies",
        "not-allowed": "\U0001f6ab Not allowed",
    },
    "zh": {
        "allowed": "\U0001f49a \u53ef\u5546\u7528",
        "conditional": "\u26a0\ufe0f \u6709\u6761\u4ef6",
        "varies": "\u2753 \u9010\u9879\u800c\u5b9a",
        "not-allowed": "\U0001f6ab \u7981\u6b62\u5546\u7528",
    },
}
SIGNUP_DISPLAY = {
    "en": {"required": "\U0001f511 Yes", "not-required": "\u2014", "optional": "Optional", "unknown": "\u2753"},
    "zh": {"required": "\U0001f511 \u9700\u6ce8\u518c", "not-required": "\u2014", "optional": "\u53ef\u9009", "unknown": "\u2753"},
}

T = {
    "en": {
        "lang_switch": "<strong>English</strong> | <a href=\"README.zh-CN.md\">\u7b80\u4f53\u4e2d\u6587</a>",
        "title": "# \U0001f381 Free for Creators",
        "tagline": "> A curated, license-transparent directory of **truly free** assets for video creators, streamers, podcasters, game devs, musicians and designers: **{total} resources across {ncat} categories**.",
        "glance": "**At a glance:** {monetizable} entries are 💚 safe for monetized content · {no_attribution} need ➖ no attribution · {no_signup} need 🔓 no account · {open} are CC0 / public domain.",
        "questions_intro": "Every entry answers the three questions that decide whether a \u201cfree\u201d asset is actually free for *you*:",
        "q1": "1. **What is the license?**",
        "q2": "2. **Do I have to credit anyone?** \u2192 the *Attribution* column",
        "q3": "3. **Can I use it in monetized content?** \u2192 the *Monetization* column",
        "ci_line": "Unlike plain link lists, this repo is **verified by CI**: a weekly GitHub Action link-checks every URL, and all entries live as structured, machine-readable JSON in [`data/`](data) \u2014 free to reuse (CC0).",
        "website_line": "\U0001f310 Prefer clicking? **[Browse the interactive directory](https://skyzhao1223.github.io/free-for-creators/)** \u2014 search and filter every entry by license, attribution and monetization.",
        "disclaimer": "> \u26a0\ufe0f **Disclaimer** \u2014 Licenses change and this list is maintained by volunteers. It is not legal advice. Always verify the current license on the source website before publishing, especially for monetized or client work.",
        "legend_title": "## Legend",
        "legend_header": "| Symbol | Meaning |",
        "legend": [
            "| \U0001f49a Allowed | Safe for monetized/commercial content under the listed license |",
            "| \u26a0\ufe0f Conditional | Allowed only under conditions (credit, free-tier limits, specific platforms) |",
            "| \u2753 Varies | License differs per item \u2014 check each asset |",
            "| \U0001f6ab Not allowed | Not free for commercial/monetized use |",
            "| \u2705 Required | You must credit the author/source |",
            "| \U0001f511 Yes | An (free) account is needed to download |",
        ],
        "toc_title": "## Table of Contents",
        "toc_suffix": "{n} resources",
        "related_anchor": "Related Lists",
        "contributing_anchor": "Contributing",
        "table_header": "| Resource | About | License | Attribution | Monetization | Sign-up |",
        "related_title": "## Related Lists",
        "related": [
            "- [public-apis](https://github.com/public-apis/public-apis) \u2014 free APIs for software projects",
            "- [free-for-dev](https://github.com/ripienaar/free-for-dev) \u2014 free SaaS/PaaS/IaaS tiers for developers",
            "- [awesome-stock-resources](https://github.com/neutraltone/awesome-stock-resources) \u2014 classic stock photo/video collection",
            "- [GameDev-Resources](https://github.com/Kavex/GameDev-Resources) \u2014 game development resources",
            "- [free-font](https://github.com/jaywcjlove/free-font) \u2014 \u4e2d\u82f1\u6587\u53ef\u5546\u7528\u514d\u8d39\u5b57\u4f53 (free CJK fonts)",
            "- [design-resources-for-developers](https://github.com/bradtraversy/design-resources-for-developers) \u2014 design resources for developers",
        ],
        "contributing_title": "## Contributing",
        "contributing": "Found a great free resource? Spot a dead link or an outdated license? Please open a PR or use the issue templates \u2014 see [CONTRIBUTING.md](CONTRIBUTING.md). Entries are added to the JSON files in [`data/`](data); both READMEs are generated automatically.",
        "star_title": "## Star History",
        "license_title": "## License",
        "license_body": "To the extent possible under law, the contributors waive all copyright and related rights to this collection under [CC0 1.0](LICENSE). Fork it, mirror it, build on it \u2014 no strings attached.",
        "footer": "*If this list ever saved you from a copyright claim, consider dropping a \u2b50 \u2014 it helps other creators find it.*",
    },
    "zh": {
        "lang_switch": "<a href=\"README.md\">English</a> | <strong>\u7b80\u4f53\u4e2d\u6587</strong>",
        "title": "# \U0001f381 Free for Creators \u00b7 \u521b\u4f5c\u8005\u514d\u8d39\u7d20\u6750\u6e05\u5355",
        "tagline": "> \u4e00\u4efd**\u8bb8\u53ef\u900f\u660e**\u7684\u514d\u8d39\u7d20\u6750\u76ee\u5f55\uff0c\u4e13\u4e3a\u89c6\u9891\u521b\u4f5c\u8005\u3001\u4e3b\u64ad\u3001\u64ad\u5ba2\u3001\u6e38\u620f\u5f00\u53d1\u8005\u3001\u97f3\u4e50\u4eba\u4e0e\u8bbe\u8ba1\u5e08\u6253\u9020\uff1a**{ncat} \u5927\u5206\u7c7b\u3001{total} \u4e2a\u8d44\u6e90**\u3002",
        "glance": "**一眼看数据**：{monetizable} 条 💚 可直接用于货币化内容 · {no_attribution} 条 ➖ 无需署名 · {no_signup} 条 🔓 无需注册 · {open} 条属 CC0 / 公有领域。",
        "questions_intro": "\u6bcf\u6761\u8d44\u6e90\u90fd\u56de\u7b54\u4e86\u51b3\u5b9a\u300c\u514d\u8d39\u300d\u7d20\u6750\u80fd\u5426\u653e\u5fc3\u7528\u7684\u4e09\u4e2a\u95ee\u9898\uff1a",
        "q1": "1. **\u8bb8\u53ef\u534f\u8bae\u662f\u4ec0\u4e48\uff1f**",
        "q2": "2. **\u662f\u5426\u9700\u8981\u7f72\u540d\uff1f** \u2192 \u770b\u300c\u7f72\u540d\u300d\u5217",
        "q3": "3. **\u80fd\u5426\u7528\u4e8e\u5df2\u8d27\u5e01\u5316/\u5546\u4e1a\u5185\u5bb9\uff1f** \u2192 \u770b\u300c\u5546\u7528\u300d\u5217",
        "ci_line": "\u4e0e\u7eaf\u94fe\u63a5\u6e05\u5355\u4e0d\u540c\uff0c\u672c\u4ed3\u5e93\u7531 **CI \u81ea\u52a8\u6838\u9a8c**\uff1aGitHub Action \u6bcf\u5468\u68c0\u67e5\u5168\u90e8\u94fe\u63a5\uff1b\u6240\u6709\u6761\u76ee\u4ee5\u7ed3\u6784\u5316 JSON \u5b58\u4e8e [`data/`](data)\uff0c\u53ef\u81ea\u7531\u590d\u7528\uff08CC0\uff09\u3002",
        "website_line": "\U0001f310 \u66f4\u559c\u6b22\u70b9\u9009\u6d4f\u89c8\uff1f**[\u6253\u5f00\u4ea4\u4e92\u5f0f\u76ee\u5f55\u7f51\u7ad9](https://skyzhao1223.github.io/free-for-creators/)**\u2014\u2014\u652f\u6301\u641c\u7d22\uff0c\u5e76\u6309\u8bb8\u53ef\u3001\u7f72\u540d\u3001\u5546\u7528\u6761\u4ef6\u7b5b\u9009\u3002",
        "disclaimer": "> \u26a0\ufe0f **\u514d\u8d23\u58f0\u660e** \u2014 \u8bb8\u53ef\u6761\u6b3e\u4f1a\u53d8\uff0c\u672c\u6e05\u5355\u7531\u5fd7\u613f\u8005\u7ef4\u62a4\uff0c\u4e0d\u6784\u6210\u6cd5\u5f8b\u5efa\u8bae\u3002\u53d1\u5e03\u524d\uff08\u5c24\u5176\u662f\u8d27\u5e01\u5316\u6216\u5546\u5355\u5185\u5bb9\uff09\u8bf7\u52a1\u5fc5\u5230\u6e90\u7ad9\u6838\u5bf9\u6700\u65b0\u8bb8\u53ef\u3002",
        "legend_title": "## \u56fe\u4f8b",
        "legend_header": "| \u56fe\u6807 | \u542b\u4e49 |",
        "legend": [
            "| \U0001f49a \u53ef\u5546\u7528 | \u6309\u6240\u5217\u8bb8\u53ef\uff0c\u53ef\u5b89\u5168\u7528\u4e8e\u8d27\u5e01\u5316/\u5546\u4e1a\u5185\u5bb9 |",
            "| \u26a0\ufe0f \u6709\u6761\u4ef6 | \u4ec5\u5728\u6ee1\u8db3\u6761\u4ef6\u65f6\u53ef\u7528\uff08\u7f72\u540d\u3001\u514d\u8d39\u989d\u5ea6\u3001\u7279\u5b9a\u5e73\u53f0\uff09 |",
            "| \u2753 \u9010\u9879\u800c\u5b9a | \u7ad9\u5185\u5404\u8d44\u6e90\u8bb8\u53ef\u4e0d\u540c\u2014\u2014\u9010\u4e2a\u6838\u5bf9 |",
            "| \U0001f6ab \u7981\u6b62\u5546\u7528 | \u4e0d\u53ef\u514d\u8d39\u7528\u4e8e\u5546\u4e1a/\u8d27\u5e01\u5316\u5185\u5bb9 |",
            "| \u2705 \u9700\u7f72\u540d | \u5fc5\u987b\u6ce8\u660e\u4f5c\u8005/\u6765\u6e90 |",
            "| \U0001f511 \u9700\u6ce8\u518c | \u4e0b\u8f7d\u9700\u8981\uff08\u514d\u8d39\uff09\u8d26\u53f7 |",
        ],
        "toc_title": "## \u76ee\u5f55",
        "toc_suffix": "{n} \u4e2a\u8d44\u6e90",
        "related_anchor": "\u76f8\u5173\u6e05\u5355",
        "contributing_anchor": "\u53c2\u4e0e\u8d21\u732e",
        "table_header": "| \u8d44\u6e90 | \u7b80\u4ecb | \u8bb8\u53ef | \u7f72\u540d | \u5546\u7528 | \u6ce8\u518c |",
        "related_title": "## \u76f8\u5173\u6e05\u5355",
        "related": [
            "- [public-apis](https://github.com/public-apis/public-apis) \u2014 \u8f6f\u4ef6\u9879\u76ee\u53ef\u7528\u7684\u514d\u8d39 API",
            "- [free-for-dev](https://github.com/ripienaar/free-for-dev) \u2014 \u9762\u5411\u5f00\u53d1\u8005\u7684\u514d\u8d39 SaaS/PaaS/IaaS \u989d\u5ea6",
            "- [awesome-stock-resources](https://github.com/neutraltone/awesome-stock-resources) \u2014 \u7ecf\u5178\u56fe\u5e93/\u89c6\u9891\u7d20\u6750\u5408\u96c6",
            "- [GameDev-Resources](https://github.com/Kavex/GameDev-Resources) \u2014 \u6e38\u620f\u5f00\u53d1\u8d44\u6e90",
            "- [free-font](https://github.com/jaywcjlove/free-font) \u2014 \u4e2d\u82f1\u6587\u53ef\u5546\u7528\u514d\u8d39\u5b57\u4f53",
            "- [design-resources-for-developers](https://github.com/bradtraversy/design-resources-for-developers) \u2014 \u9762\u5411\u5f00\u53d1\u8005\u7684\u8bbe\u8ba1\u8d44\u6e90",
        ],
        "contributing_title": "## \u53c2\u4e0e\u8d21\u732e",
        "contributing": "\u53d1\u73b0\u597d\u8d44\u6e90\uff1f\u9047\u5230\u6b7b\u94fe\u6216\u8bb8\u53ef\u8fc7\u671f\uff1f\u6b22\u8fce\u63d0 PR \u6216\u4f7f\u7528 Issue \u6a21\u677f\uff0c\u8be6\u89c1 [CONTRIBUTING.md](CONTRIBUTING.md)\u3002\u6761\u76ee\u6dfb\u52a0\u5728 [`data/`](data) \u7684 JSON \u4e2d\uff0c\u4e24\u4efd README \u5747\u81ea\u52a8\u751f\u6210\u3002",
        "star_title": "## Star \u5386\u53f2",
        "license_title": "## \u8bb8\u53ef",
        "license_body": "\u5728\u6cd5\u5f8b\u5141\u8bb8\u7684\u6700\u5927\u8303\u56f4\u5185\uff0c\u8d21\u732e\u8005\u4f9d\u636e [CC0 1.0](LICENSE) \u653e\u5f03\u5bf9\u672c\u5408\u96c6\u7684\u5168\u90e8\u7248\u6743\u53ca\u76f8\u5173\u6743\u5229\u3002\u968f\u610f Fork\u3001\u955c\u50cf\u3001\u4e8c\u6b21\u521b\u4f5c\u2014\u2014\u65e0\u4efb\u4f55\u9644\u52a0\u6761\u4ef6\u3002",
        "footer": "*\u5982\u679c\u8fd9\u4efd\u6e05\u5355\u66fe\u5e2e\u4f60\u907f\u5f00\u4e00\u6b21\u7248\u6743\u7d22\u8d54\uff0c\u6b22\u8fce\u70b9\u4e2a \u2b50\uff0c\u8ba9\u66f4\u591a\u521b\u4f5c\u8005\u627e\u5230\u5b83\u3002*",
    },
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
        for field in ("category_zh", "description_zh"):
            if field not in data:
                warnings.append("%s: missing '%s' (zh README falls back to English)" % (path.name, field))
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
            if "description_zh" not in e:
                warnings.append("%s: missing description_zh" % where)
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


def compute_stats(categories):
    """Aggregate counts for the 'At a glance' README line."""
    entries = [e for c in categories for e in c["entries"]]
    return {
        "monetizable": sum(1 for e in entries if e["monetization"] == "allowed"),
        "no_attribution": sum(1 for e in entries if e["attribution"] == "not-required"),
        "no_signup": sum(1 for e in entries if e.get("signup") == "not-required"),
        "open": sum(1 for e in entries if re.search(r"cc0|public domain", e.get("license", ""), re.IGNORECASE)),
    }


def slugify(heading):
    """GitHub-style anchor slug for a heading.

    GitHub drops punctuation (keeping - and _), lowercases, and turns EACH
    space into a hyphen without collapsing runs — so 'Graphics & Icons'
    becomes 'graphics--icons'. CJK characters are kept as-is.
    """
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


def cat_name(c, lang):
    if lang == "zh":
        return c.get("category_zh") or c["category"]
    return c["category"]


def cat_desc(c, lang):
    if lang == "zh":
        return c.get("description_zh") or c["description"]
    return c["description"]


def entry_row(e, lang):
    attr = ATTR_DISPLAY[lang]
    mon = MON_DISPLAY[lang]
    signup = SIGNUP_DISPLAY[lang]
    if lang == "zh" and e.get("description_zh"):
        desc = e["description_zh"]
    else:
        desc = e["description"]
    notes = None
    if e.get("notes"):
        notes = e["notes_zh"] if (lang == "zh" and e.get("notes_zh")) else e["notes"]
    desc = desc.replace("|", "\\|")
    if notes:
        desc += " _%s_" % notes.replace("|", "\\|")
    return "| [%s](%s) | %s | %s | %s | %s | %s |" % (
        e["name"].replace("|", "\\|"),
        e["url"],
        desc,
        e["license"],
        attr[e["attribution"]],
        mon[e["monetization"]],
        signup.get(e.get("signup", "unknown"), "\u2753"),
    )


def render(categories, lang):
    t = T[lang]
    total = sum(len(c["entries"]) for c in categories)
    lines = []
    a = lines.append

    a("<!-- This file is generated by scripts/build_readme.py from data/*.json \u2014 edit the JSON, then run the script (CI checks sync). -->")
    a("")
    a('<p align="center">')
    a('  <img src="assets/banner.png" alt="Free for Creators: music, sound effects, footage, photos, fonts, icons, LUTs, mockups, 3D assets, search engines" width="860">')
    a("</p>")
    a("")
    a('<p align="center">%s</p>' % t["lang_switch"])
    a("")
    a(t["title"])
    a("")
    a('<p align="center">')
    a('  <a href="' + PAGES_URL + '"><img src="https://img.shields.io/badge/%F0%9F%8C%90_website-browse-success" alt="Website"></a>')
    a('  <a href="https://github.com/%s/actions/workflows/ci.yml"><img src="https://github.com/%s/actions/workflows/ci.yml/badge.svg" alt="CI"></a>' % (REPO, REPO))
    a('  <a href="https://github.com/%s/actions/workflows/linkcheck.yml"><img src="https://github.com/%s/actions/workflows/linkcheck.yml/badge.svg" alt="Link check"></a>' % (REPO, REPO))
    a('  <img src="https://img.shields.io/badge/resources-%d-blue" alt="%d resources">' % (total, total))
    a('  <img src="https://img.shields.io/badge/license-CC0-lightgrey" alt="License: CC0">')
    a('  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs welcome">')
    a("</p>")
    a("")
    a(t["tagline"].format(total=total, ncat=len(categories)))
    a("")
    a(t["glance"].format(**compute_stats(categories)))
    a("")
    a(t["questions_intro"])
    a("")
    a(t["q1"])
    a(t["q2"])
    a(t["q3"])
    a("")
    a(t["ci_line"])
    a("")
    a(t["website_line"])
    a("")
    a(t["disclaimer"])
    a("")
    a(t["legend_title"])
    a("")
    a(t["legend_header"])
    a("| --- | --- |")
    for row in t["legend"]:
        a(row)
    a("")
    a(t["toc_title"])
    a("")
    for c in categories:
        name = cat_name(c, lang)
        a("- [%s](#%s) \u2014 %s \u00b7 %s" % (name, slugify(name), t["toc_suffix"].format(n=len(c["entries"])), cat_desc(c, lang)))
    a("- [%s](#%s)" % (t["related_anchor"], slugify(t["related_anchor"])))
    a("- [%s](#%s)" % (t["contributing_anchor"], slugify(t["contributing_anchor"])))
    a("")
    for c in categories:
        a("## %s" % cat_name(c, lang))
        a("")
        a("*%s*" % cat_desc(c, lang))
        a("")
        a(t["table_header"])
        a("| --- | --- | --- | --- | --- | --- |")
        for e in c["entries"]:
            a(entry_row(e, lang))
        a("")
    a(t["related_title"])
    a("")
    for row in t["related"]:
        a(row)
    a("")
    a(t["contributing_title"])
    a("")
    a(t["contributing"])
    a("")
    a(t["star_title"])
    a("")
    a('<a href="https://star-history.com/#%s&Date">' % REPO)
    a("  <picture>")
    a('    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=%s&type=Date&theme=dark" />' % REPO)
    a('    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=%s&type=Date" width="600" />' % REPO)
    a("  </picture>")
    a("</a>")
    a("")
    a(t["license_title"])
    a("")
    a("[![CC0](https://licensebuttons.net/p/zero/1.0/88x31.png)](https://creativecommons.org/publicdomain/zero/1.0/)")
    a("")
    a(t["license_body"])
    a("")
    a("---")
    a("")
    a(t["footer"])
    a("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if any README is not in sync with data/")
    ap.add_argument("--validate-only", action="store_true", help="only validate data files")
    args = ap.parse_args()

    categories = load_categories()
    total = sum(len(c["entries"]) for c in categories)
    print("Validated %d categories, %d entries." % (len(categories), total))
    if args.validate_only:
        return

    rendered = {lang: render(categories, lang) for lang in OUTPUTS}
    if args.check:
        bad = False
        for lang, path in OUTPUTS.items():
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != rendered[lang]:
                bad = True
                print("ERROR: %s is out of sync with data/. Run: python3 scripts/build_readme.py" % path.name, file=sys.stderr)
                if current is None:
                    print("(%s does not exist)" % path.name, file=sys.stderr)
        if bad:
            sys.exit(1)
        print("All READMEs are in sync.")
        return

    for lang, path in OUTPUTS.items():
        path.write_text(rendered[lang], encoding="utf-8")
        print("Wrote %s" % path)


if __name__ == "__main__":
    main()
