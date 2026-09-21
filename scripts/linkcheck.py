#!/usr/bin/env python3
"""Link-check every URL in data/*.json. Stdlib only (Python >= 3.9). License: CC0.

Classification:
    ok         HTTP 2xx/3xx
    blocked    HTTP 401/403/405/429/501/503/999, SSL errors, timeouts
               (bot protection — a public page never 401s a real browser;
               needs a human, does NOT fail CI)
    broken     HTTP 404/410, other persistent 4xx/5xx, DNS failure, refused
               (fails CI with exit code 1)

Usage:
    python3 scripts/linkcheck.py                # check everything
    python3 scripts/linkcheck.py --slug music   # only one category (repeatable)

When $GITHUB_STEP_SUMMARY is set, a Markdown report is appended to it (CI job summary).
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 free-for-creators-linkcheck"
)
TIMEOUT = 20
RETRIES = 2
MAX_WORKERS = 8

BLOCKED_CODES = {401, 403, 405, 429, 501, 503, 999}


def collect(slugs=None):
    items = []
    for path in sorted(DATA_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        slug = data.get("slug", path.stem)
        if slugs and slug not in slugs:
            continue
        for e in data.get("entries", []):
            if e.get("skip_linkcheck"):
                continue
            items.append((slug, e.get("name", "?"), e.get("url", "")))
    return items


def probe(url, method):
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        r.read(64)
        return r.status


def check_one(url):
    """Return (status, detail). status in {ok, blocked, broken}."""
    last = None
    for method in ("HEAD", "GET"):
        blocked_here = False
        for attempt in range(RETRIES + 1):
            try:
                code = probe(url, method)
                return ("ok", str(code))
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code in BLOCKED_CODES:
                    blocked_here = True
                    break
                if e.code in (404, 410):
                    return ("broken", last)
                if 500 <= e.code < 600 and attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                return ("broken", last)
            except ssl.SSLError as e:
                last = "SSL: %s" % e
                blocked_here = True
                break
            except OSError as e:
                msg = str(e)
                last = "%s: %s" % (type(e).__name__, msg)
                if "nodename nor servname" in msg or "Name or service not known" in msg:
                    return ("broken", "DNS failure")
                if "Connection refused" in msg:
                    return ("broken", "connection refused")
                if attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                blocked_here = True
                break
        if not blocked_here:
            continue
        if method == "HEAD":
            continue  # retry with GET: many sites block HEAD but allow GET
        return ("blocked", last or "unknown")
    return ("blocked", last or "unknown")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", action="append", help="only check this category slug (repeatable)")
    args = ap.parse_args()

    items = collect(args.slug)
    if not items:
        print("No URLs found.", file=sys.stderr)
        sys.exit(2)

    print("Checking %d URLs (%d workers)..." % (len(items), MAX_WORKERS))
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [(ex.submit(check_one, url), slug, name, url) for slug, name, url in items]
        for fut, slug, name, url in futures:
            try:
                status, detail = fut.result()
            except Exception as e:  # never crash the run
                status, detail = "blocked", "check crashed: %s" % e
            results.append((status, slug, name, url, detail))
            print("  [%-7s] %-42s %s" % (status, name[:42], detail), flush=True)

    ok = [r for r in results if r[0] == "ok"]
    blocked = [r for r in results if r[0] == "blocked"]
    broken = [r for r in results if r[0] == "broken"]

    print("\nSummary: %d ok, %d blocked (needs manual check), %d broken" % (len(ok), len(blocked), len(broken)))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("## Link check: %d ok / %d blocked / %d broken\n\n" % (len(ok), len(blocked), len(broken)))
            for label, group in (("Broken (must fix)", broken), ("Blocked (manual check)", blocked)):
                if group:
                    f.write("### %s\n\n| Category | Resource | URL | Detail |\n| --- | --- | --- | --- |\n" % label)
                    for _, slug, name, url, detail in sorted(group, key=lambda r: (r[1], r[2])):
                        f.write("| %s | %s | %s | %s |\n" % (slug, name, url, detail))
                    f.write("\n")

    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
