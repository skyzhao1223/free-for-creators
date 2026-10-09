#!/usr/bin/env python3
"""Build everything the website needs from data/*.json.

Outputs — ALL generated at deploy time and gitignored, like site/data/:

    site/data/*.json   per-category copies + manifest.json + all.json (one merged file)
    site/all.html      a fully static, crawlable mirror of every entry — no JavaScript
    site/sitemap.xml   both URLs, with <lastmod> from the last commit that touched data/

Why all.html exists: site/index.html renders client-side, so a crawler that does not
execute JavaScript sees an empty directory — the site's whole value was invisible to
search. all.html gives crawlers the real content plus ItemList/Dataset JSON-LD.

Why these are NOT committed (they were, briefly): sitemap's <lastmod> and the JSON-LD
`dateModified` come from the last commit that touched data/. The commit that
regenerates them *is* a commit that touches data/, so a committed copy can never agree
with what CI computes — every data change failed its own sync check. A derived file
whose inputs include its own commit belongs in the build, not in git.

This file is the single implementation of the site build. It replaces the inline python
that used to be duplicated in .github/workflows/pages.yml and in CONTRIBUTING.md's
local-preview one-liner — three copies that could (and did) drift.

Usage:
    python3 scripts/build_site.py            # build everything into site/
    python3 scripts/build_site.py --out DIR  # put the payload somewhere else
    python3 scripts/build_site.py --check    # local: is my site/ preview stale?

Also fails when site/index.html's CAT_COLORS does not cover every CATEGORY_ORDER slug —
that map is the one site-side constant you must update by hand for a new category, and
this is the check CI does run.

Stdlib only (Python >= 3.9). License: CC0.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
DEFAULT_OUT = SITE_DIR / "data"

sys.path.insert(0, str(ROOT / "scripts"))
from build_readme import (  # noqa: E402
    ATTR_DISPLAY, CATEGORY_ORDER, MON_DISPLAY, PAGES_URL, REPO, SIGNUP_DISPLAY, load_categories,
)

ALL_HTML = SITE_DIR / "all.html"
SITEMAP = SITE_DIR / "sitemap.xml"

# Labels for the static page. Kept here (not in build_readme.T) because they are
# page furniture, not README prose.
LABELS = {
    "title": "All resources — Free for Creators",
    "h1": "Every resource, in one static table",
    "intro": ("The interactive directory needs JavaScript. This page does not: it is the same "
              "%d entries, generated from the same data/*.json by the same build."),
    "interactive": "Open the interactive directory (search + filters)",
    "repo": "data/*.json on GitHub",
    "zh_readme": "简体中文 README",
    "th": ["Resource", "About", "License", "Attribution", "Monetization", "Sign-up"],
    "toc": "Categories",
    "license_note": ("Licenses change and this list is maintained by volunteers. It is not legal "
                     "advice — verify the current license on the source website before publishing, "
                     "especially for monetized or client work."),
    "generated": "Generated from data/*.json — do not edit by hand.",
}

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px; max-width: 1080px; margin-inline: auto;
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", "PingFang SC",
        Helvetica, Arial, sans-serif, "Apple Color Emoji";
  color: #1b1d26; background: #fbfbfd;
}
h1 { font-size: 26px; letter-spacing: -.02em; margin: 0 0 6px; }
h2 { font-size: 19px; margin: 34px 0 4px; letter-spacing: -.01em; }
p.lede { margin: 0 0 18px; color: #555a6b; }
nav.toc { margin: 18px 0 8px; display: flex; flex-wrap: wrap; gap: 8px; }
nav.toc a { text-decoration: none; font-size: 13px; padding: 4px 10px; border-radius: 999px;
  border: 1px solid #dfe1ea; background: #fff; color: #2b2f3d; }
nav.toc a:hover { border-color: #6d28d9; color: #6d28d9; }
p.cat { margin: 0 0 10px; color: #555a6b; font-size: 14px; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e4e6ef;
  border-radius: 10px; overflow: hidden; font-size: 13.5px; }
caption { caption-side: bottom; text-align: left; color: #6b7080; font-size: 12px; padding: 8px 2px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef0f5; vertical-align: top; }
th { background: #f4f5fa; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em;
  color: #55596a; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
td.about { color: #3d4152; }
td.about em { color: #6b7080; }
a { color: #5b21b6; }
a:hover { text-decoration: none; }
footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid #e4e6ef;
  color: #6b7080; font-size: 12.5px; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 8px; top: 8px; background: #5b21b6; color: #fff; padding: 8px 12px;
  border-radius: 6px; z-index: 9; }
@media (prefers-color-scheme: dark) {
  body { color: #e8eaf6; background: #0a0a12; }
  p.lede, p.cat, td.about { color: #a7adca; }
  td.about em { color: #8b93b5; }
  table { background: #12121c; border-color: #262636; }
  th { background: #191926; color: #a7adca; border-color: #262636; }
  th, td { border-bottom-color: #1f1f2c; }
  nav.toc a { background: #12121c; border-color: #262636; color: #cfd3ea; }
  a { color: #c4b5fd; }
  footer { border-color: #262636; color: #8b93b5; }
}
@media (max-width: 720px) {
  th, td { padding: 6px 7px; }
  .hide-sm { display: none; }
}
""".strip()


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=str(ROOT), capture_output=True, text=True, timeout=20)


def data_lastmod():
    """Date of the last commit that touched data/.

    A shallow clone grafts HEAD into a root commit, so `git log -- data/` reports HEAD
    even when HEAD never touched data/. Both CI workflows check out with
    `fetch-depth: 0`; anywhere else we warn and fall back to today rather than fail,
    because this value only ever reaches a deploy-time artifact.
    """
    try:
        if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            print("WARNING: shallow clone — sitemap <lastmod> falls back to today's date. "
                  "Run `git fetch --unshallow` for the real one.", file=sys.stderr)
            return date.today().isoformat()
        out = git("log", "-1", "--format=%cs", "--", "data/")
        stamp = out.stdout.strip()
        if out.returncode == 0 and re.match(r"^\d{4}-\d{2}-\d{2}$", stamp):
            return stamp
    except Exception:
        pass
    return date.today().isoformat()


def payloads(categories):
    """{filename: content} for the deploy payload. Byte-identical to what pages.yml's
    inline python used to produce, so the deployed artifact does not change shape."""
    by_slug = {c["slug"]: c for c in categories}
    out = {}
    for slug in CATEGORY_ORDER:
        out["%s.json" % slug] = (DATA_DIR / ("%s.json" % slug)).read_text(encoding="utf-8")
    out["manifest.json"] = json.dumps([{"slug": s} for s in CATEGORY_ORDER])
    out["all.json"] = json.dumps(
        {"total": sum(len(by_slug[s]["entries"]) for s in CATEGORY_ORDER),
         "categories": [by_slug[s] for s in CATEGORY_ORDER]},
        ensure_ascii=False,
    )
    return out


def jsonld(categories, total, lastmod):
    """ItemList + Dataset structured data with the real counts."""
    items = []
    pos = 0
    for c in categories:
        for e in c["entries"]:
            pos += 1
            items.append({"@type": "ListItem", "position": pos, "name": e["name"], "url": e["url"]})
    return {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "ItemList",
                "name": "Free for Creators — all resources",
                "description": "License-verified directory of free assets for video creators, "
                               "streamers, podcasters, game developers, musicians and designers.",
                "numberOfItems": total,
                "itemListElement": items,
            },
            {
                "@type": "Dataset",
                "name": "Free for Creators",
                "description": "Structured, CC0-licensed directory of free creative assets with "
                               "license, attribution, monetization and sign-up facts per entry.",
                "url": PAGES_URL + "all.html",
                "license": "https://creativecommons.org/publicdomain/zero/1.0/",
                "dateModified": lastmod,
                "distribution": {
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": PAGES_URL + "data/all.json",
                },
                "creator": {"@type": "Organization", "name": "github.com/" + REPO},
            },
        ],
    }


def render_all_html(categories, total, lastmod):
    attr, mon, sig = ATTR_DISPLAY["en"], MON_DISPLAY["en"], SIGNUP_DISPLAY["en"]
    out = []
    a = out.append
    a("<!doctype html>")
    a('<html lang="en">')
    a("<head>")
    a('<meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a("<title>%s</title>" % esc(LABELS["title"]))
    a('<meta name="description" content="All %d license-verified free resources for creators in '
      'one static, crawlable table: license, attribution, monetization and sign-up per entry.">' % total)
    a('<link rel="canonical" href="%sall.html">' % PAGES_URL)
    a('<meta name="robots" content="index,follow">')
    a('<meta property="og:title" content="%s">' % esc(LABELS["title"]))
    a('<meta property="og:type" content="website">')
    a('<meta property="og:url" content="%sall.html">' % PAGES_URL)
    # `<` is escaped as \u003c so a name/url containing "</script>" cannot terminate the
    # block early. Inside <script> HTML entities are NOT decoded, so & stays literal.
    ld = json.dumps(jsonld(categories, total, lastmod), ensure_ascii=False,
                    separators=(",", ":")).replace("<", "\\u003c")
    a('<script type="application/ld+json">%s</script>' % ld)
    a("<style>%s</style>" % CSS)
    a("</head>")
    a("<body>")
    a('<a class="skip" href="#main">Skip to the table</a>')
    a('<main id="main" tabindex="-1">')
    a("<h1>%s</h1>" % esc(LABELS["h1"]))
    a('<p class="lede">%s</p>' % esc(LABELS["intro"] % total))
    a('<p class="lede"><a href="%s">%s</a> · <a href="https://github.com/%s/tree/main/data">%s</a> · '
      '<a href="https://github.com/%s/blob/main/README.zh-CN.md">%s</a></p>'
      % (PAGES_URL, esc(LABELS["interactive"]), REPO, esc(LABELS["repo"]), REPO, esc(LABELS["zh_readme"])))
    a('<nav class="toc" aria-label="%s">' % esc(LABELS["toc"]))
    for c in categories:
        a('<a href="#%s">%s (%d)</a>' % (esc(c["slug"]), esc(c["category"]), len(c["entries"])))
    a("</nav>")
    for c in categories:
        a('<section aria-labelledby="h-%s">' % esc(c["slug"]))
        a('<h2 id="h-%s">%s <span aria-hidden="true">·</span> %d</h2>'
          % (esc(c["slug"]), esc(c["category"]), len(c["entries"])))
        a('<p class="cat">%s</p>' % esc(c["description"]))
        a("<table>")
        a("<caption>%s</caption>" % esc(c["category"]))
        a("<thead><tr>" + "".join(
            '<th scope="col"%s>%s</th>' % (' class="hide-sm"' if i in (2, 5) else "", esc(h))
            for i, h in enumerate(LABELS["th"])) + "</tr></thead>")
        a("<tbody>")
        for e in c["entries"]:
            about = esc(e["description"])
            if e.get("notes"):
                about += " <em>%s</em>" % esc(e["notes"])
            a("<tr>")
            a('<th scope="row"><a href="%s" rel="noopener">%s</a></th>' % (esc(e["url"]), esc(e["name"])))
            a('<td class="about">%s</td>' % about)
            a('<td class="hide-sm">%s</td>' % esc(e["license"]))
            a("<td>%s</td>" % esc(attr.get(e["attribution"], e["attribution"])))
            a("<td>%s</td>" % esc(mon.get(e["monetization"], e["monetization"])))
            a('<td class="hide-sm">%s</td>' % esc(sig.get(e.get("signup", "unknown"), "?")))
            a("</tr>")
        a("</tbody></table>")
        a("</section>")
    a("</main>")
    a("<footer>")
    a("<p>%s</p>" % esc(LABELS["license_note"]))
    a("<p>%s · <a href=\"https://creativecommons.org/publicdomain/zero/1.0/\">CC0</a> · %s</p>"
      % (esc(LABELS["generated"]), esc("last data change: " + lastmod)))
    a("</footer>")
    a("</body>")
    a("</html>")
    return "\n".join(out) + "\n"


def render_sitemap(lastmod):
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n"
            "  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>\n"
            "    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>\n"
            "  <url>\n    <loc>%sall.html</loc>\n    <lastmod>%s</lastmod>\n"
            "    <changefreq>weekly</changefreq>\n    <priority>0.9</priority>\n  </url>\n"
            "</urlset>\n" % (PAGES_URL, lastmod, PAGES_URL, lastmod))


def check_cat_colors(index_html, slugs):
    """Every category slug must have a colour in site/index.html's CAT_COLORS map."""
    m = re.search(r"const CAT_COLORS = \{(.*?)\};", index_html, re.S)
    if not m:
        return ["could not find `const CAT_COLORS` in site/index.html"]
    have = set(re.findall(r'"([a-z0-9-]+)"\s*:', m.group(1)))
    problems = []
    missing = [s for s in slugs if s not in have]
    if missing:
        problems.append("site/index.html CAT_COLORS is missing slug(s): %s" % ", ".join(missing))
    unknown = sorted(have - set(slugs))
    if unknown:
        problems.append("site/index.html CAT_COLORS has slug(s) not in CATEGORY_ORDER: %s" % ", ".join(unknown))
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="payload output directory (default: site/data)")
    ap.add_argument("--check", action="store_true",
                    help="local convenience: fail if the site/ preview is stale. Not a CI gate.")
    ap.add_argument("--skip-color-check", action="store_true", help="do not verify CAT_COLORS coverage")
    args = ap.parse_args()

    categories = load_categories()
    total = sum(len(c["entries"]) for c in categories)
    lastmod = data_lastmod()
    pages = {ALL_HTML: render_all_html(categories, total, lastmod), SITEMAP: render_sitemap(lastmod)}
    payload = payloads(categories)

    # The one gate that can fail CI: a hand-maintained map drifting from CATEGORY_ORDER.
    problems = []
    if not args.skip_color_check:
        problems += check_cat_colors((SITE_DIR / "index.html").read_text(encoding="utf-8"), CATEGORY_ORDER)
    if problems:
        for p in problems:
            print("ERROR: %s" % p, file=sys.stderr)
        sys.exit(1)
    print("CAT_COLORS covers all %d category slugs." % len(CATEGORY_ORDER))

    out_dir = Path(args.out)
    if args.check:
        stale = []
        for path, content in pages.items():
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current is None:
                stale.append("%s (missing)" % path.name)
            elif current != content:
                stale.append("%s (differs)" % path.name)
        for name, content in sorted(payload.items()):
            path = out_dir / name
            if not path.exists():
                stale.append("%s (missing)" % name)
            elif path.read_text(encoding="utf-8") != content:
                stale.append("%s (differs)" % name)
        if out_dir.is_dir():
            stale += ["%s (unexpected)" % p.name for p in sorted(out_dir.glob("*.json")) if p.name not in payload]
        if stale:
            print("Local site/ preview is stale: %s\n       Run: python3 scripts/build_site.py"
                  % ", ".join(stale), file=sys.stderr)
            sys.exit(1)
        print("Local site/ preview is up to date (%d entries, lastmod %s)." % (total, lastmod))
        return

    for path, content in pages.items():
        path.write_text(content, encoding="utf-8")
        print("Wrote %s" % path)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(payload.items()):
        (out_dir / name).write_text(content, encoding="utf-8")
    # Drop leftovers from removed categories. Match on the full file name — `p.stem`
    # never equals a payload key (which carries ".json") and would delete everything.
    for extra in sorted(p for p in out_dir.glob("*.json") if p.name not in payload):
        extra.unlink()
    print("Wrote %d payload files to %s (%d entries)" % (len(payload), out_dir, total))


if __name__ == "__main__":
    main()
