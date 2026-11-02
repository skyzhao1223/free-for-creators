#!/usr/bin/env python3
"""Link-check every URL in data/*.json. Stdlib only (Python >= 3.9). License: CC0.

Classification:
    ok         HTTP 200/2xx-with-content AND the page looks like a real page
    blocked    HTTP 401/403/405/429/999, any 5xx that survives retries, 202/204 (the
               server accepted the request but returned no document), SSL errors,
               timeouts (bot protection), OR a 200 that needs a human: a JS shell with no
               <title>/<h1> in the HTML, or a redirect onto a different domain (rebrand?).
               Needs a human, does NOT fail CI.
    broken     HTTP 404/410, another persistent 4xx, DNS failure, a connection that is
               still refused after retries on BOTH methods, OR a 200 whose own
               <title>/<h1> says "not found" (soft 404).
               Fails CI with exit code 1.

The line between the last two is evidence, not inconvenience. `broken` means the resource
is gone and the entry should be removed, so it is reserved for verdicts that cannot be
explained by a bad day: a 404/410 from the origin, a name that does not resolve, or the
page itself saying "not found". A 5xx is the server's own state and proves nothing about
the resource — fontsquirrel.com answered 502 for an entire run and 202 a minute later,
with the origin merely unwell. Filing a dead-link issue for that would train maintainers
to ignore the issue, which is worse than missing a genuinely dead 5xx; a persistent one
shows up as `blocked` week after week and gets looked at.

A status code alone is not enough: some SPAs answer 200 for any path and render
"Page not found" client-side, and some sites 301 to a new domain after a rebrand.

Usage:
    python3 scripts/linkcheck.py                # check everything
    python3 scripts/linkcheck.py --slug music   # only one category (repeatable)

When $GITHUB_STEP_SUMMARY is set, a Markdown report is appended to it (CI job summary).
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 free-for-creators-linkcheck"
)
TIMEOUT = 20
RETRIES = 2

# Set by --no-proxy. build_opener() otherwise inherits the environment's proxies, and on
# macOS that includes a system-wide proxy with no env var set, so `env | grep -i proxy`
# shows nothing while every request still leaves through it.
#
# The flag exists to make that visible and controllable, NOT to reproduce CI. Measured on
# one machine against all 200 entries, the two modes disagree in both directions and
# neither matches the weekly CI run:
#   through 127.0.0.1:7897   146 ok / 52 blocked / 1 broken   (3 network failures)
#   direct, no proxy         134 ok / 64 blocked / 1 broken   (20 network failures)
#   CI, US runner, no proxy  127 ok / 53 blocked / 0 broken
# The proxy's exit IP is bot-walled by some sites (eight of ten sampled 403s were reachable
# straight from the host, two of them plain 200), while its routing reaches sites the host
# cannot: itch.io timed out on both paths here and refused port 443 outright, yet CI has
# never reported it broken. Treat a local run as indicative and CI's as the baseline.
NO_PROXY = False
MAX_WORKERS = 8

# 5xx is deliberately absent: every 5xx now goes through the retry ladder below and ends
# as `blocked`, so 501/503 no longer need special-casing here (they used to skip retries).
BLOCKED_CODES = {401, 403, 405, 429, 999}

# --- Domain takeovers ----------------------------------------------------------------
# A status code cannot see the failure this guards: when a domain is taken over, the
# replacement answers 200 with its own healthy <title>, which is indistinguishable from the
# resource still being there. Found on 2026-10-18 in another directory — teenyicons.com had
# been serving a gambling site for months while the icon project lived on under a GitHub
# home. A green weekly check would have said "fine" the whole time.
#
# So each run records the <title> it saw, and a later run compares. The comparison is
# deliberately crude and deliberately non-fatal: a title that loses almost every shared word
# is *not* proof of anything — a rebrand looks the same — so it downgrades the entry to
# `blocked` (which never fails the build) with a detail string a human can act on, and keeps
# the old fingerprint so the item stays visible until somebody looks.
TITLE_STATE_PATH = ROOT / "linkcheck-titles.json"
TITLE_CHANGE_THRESHOLD = 0.2
TITLE_STATE_VERSION = 1

# A 2xx that carries no document. 202 means "accepted, still processing" — what a bot
# queue or challenge returns instead of the page — and 204 is explicitly empty. Neither
# proves the resource is alive, and neither proves it is gone.
CONTENTLESS_OK = {202, 204}

# How much of a GET body to inspect for soft-404 signals. <title> and the first <h1>
# are essentially always inside the first 64 KB of server-rendered HTML.
BODY_BYTES = 65536

# A 200 that is really the site's own "not found" page. Matched against <title> and
# the first <h1> ONLY — scanning the whole body false-positives on legitimate pages
# that merely mention 404.
SOFT_404 = re.compile(
    r"page not found|page (?:cannot|can'?t|could not) be found|can'?t find that|"
    r"doesn'?t exist|does not exist|appears to be moved|error 404|^404\b|404 (?:error|page)",
    re.IGNORECASE,
)


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
            items.append((slug, e.get("name", "?"), e.get("url", ""), bool(e.get("linkcheck_js"))))
    return items


def registrable(host):
    """Crude registrable-domain guess ('www.a.b.co.uk' -> 'co.uk' is wrong, but the
    only thing we care about is 'did the host change identity', e.g. freepik.com ->
    magnific.com, which this catches)."""
    parts = (host or "").lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def strip_tags(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def inspect_body(body):
    """Return (is_soft_404, title_or_h1_text, had_title_or_h1) for a GET body."""
    ti = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
    text = " ".join(strip_tags(m.group(1)) for m in (ti, h1) if m).strip()
    return (bool(text) and bool(SOFT_404.search(text))), text[:90], bool(ti or h1)


class Redirect308(urllib.request.HTTPRedirectHandler):
    """Follow 308 as well as 301/302/303/307.

    Python's urllib only learned 308 in 3.11. On 3.9/3.10 a permanent, method-preserving
    308 surfaced as `HTTPError: 308`, which check_one classified as `broken` — so the
    same URL was `broken` locally and `ok` in CI (which runs 3.12). Verdicts must not
    depend on the interpreter version. Example that triggered this:
    https://atelier-anchor.com/typefaces/smiley-sans/ -> 308 -> the same path sans slash.
    """

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_307(req, fp, code, msg, headers)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 308 and 307 share semantics for our purposes: keep the method, follow Location.
        return super().redirect_request(req, fp, 307 if code == 308 else code,
                                        msg, headers, newurl)


def probe(url, method):
    """Return (status, final_url, body_text). body_text is '' for HEAD."""
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8" if method == "GET" else "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    ctx = ssl.create_default_context()
    handlers = [Redirect308, urllib.request.HTTPSHandler(context=ctx)]
    if NO_PROXY:
        # build_opener inherits the environment's proxies otherwise, including the
        # system-wide one macOS exposes through getproxies() with no env var set.
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=TIMEOUT) as r:
        body = r.read(BODY_BYTES).decode("utf-8", "replace") if method == "GET" else ""
        return r.status, r.geturl(), body


#: Scripts written without spaces (CJK) cannot be split on whitespace or punctuation: the
#: old rule cut on [^0-9a-z]+ and turned 「阿里巴巴普惠体」 into the empty set. They are
#: indexed as overlapping two-character grams instead — the usual CJK approach, and what
#: CJK search indexes do — rather than one character at a time, because single characters
#: carry almost no identity: 「免费素材」 -> 「免费在线赌场」 shares 2 of 6 characters
#: (0.25, above TITLE_CHANGE_THRESHOLD, so the takeover would pass as unchanged), while as
#: grams it shares 1 of 7 (0.14, correctly flagged). A one-character run keeps that
#: character, so a short title is not silently reduced to nothing.
#: Ranges: kana, CJK ext. A, unified ideographs, compatibility ideographs, hangul.
CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
CJK_RUN = re.compile("[%s]+" % CJK)


def title_tokens(text):
    """Words worth comparing: lowercased, punctuation stripped, 1-2 char and numeric tokens
    dropped — except CJK, which yields two-character grams (a lone character stands for
    itself)."""
    text = (text or "").lower()
    tokens = {t for t in re.split(r"[^0-9a-z]+", text)
              if len(t) >= 3 and not t.isdigit()}
    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def title_similarity(previous, current):
    """Jaccard overlap of the two titles' words. 1.0 identical, 0.0 nothing in common.

    A title can tokenise to nothing — 「---」, 「404」, or any script the tokeniser does not
    know. That is not evidence of a takeover, and returning 0.0 for it made an unchanged
    title score 0 against itself, so such a pair falls back to comparing the raw strings
    (whitespace and case aside). When only one side tokenises, the pair is still
    incomparable and stays at 0.0.
    """
    a, b = title_tokens(previous), title_tokens(current)
    if not a and not b:
        return 1.0 if (previous or "").strip().lower() == (current or "").strip().lower() else 0.0
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def load_titles(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    titles = data.get("titles") if isinstance(data, dict) else None
    return titles if isinstance(titles, dict) else {}


def review_titles(previous, observed, accept=False, threshold=TITLE_CHANGE_THRESHOLD):
    """Compare this run's titles with the stored ones.

    previous/observed: {url: title}. Returns (changes, updated):
      changes — [(url, old_title, new_title, similarity)] that moved too far to assume the
                same site, unless `accept` (a human has looked, so take the new title)
      updated — the fingerprint map to store; a flagged entry keeps its old title, so the
                next run flags it again instead of quietly rebasing onto the takeover.
    """
    changes, updated = [], {}
    for url, current in observed.items():
        old = previous.get(url)
        if not old:
            updated[url] = current
            continue
        similarity = title_similarity(old, current)
        if similarity >= threshold or accept:
            updated[url] = current
        else:
            changes.append((url, old, current, similarity))
            updated[url] = old
    # Keep fingerprints for entries we could not observe this time: a site that is briefly
    # behind a bot wall must not forget what it looked like when it comes back.
    for url, old in previous.items():
        updated.setdefault(url, old)
    return changes, updated


def check_one(url, expect_js=False, title_sink=None):
    """Return (status, detail). status in {ok, blocked, broken}.

    `expect_js` is the entry's `linkcheck_js` flag: the page is a known JavaScript app
    whose HTML carries no <title>/<h1>, so the JS-shell heuristic must not fire on it.

    GET is tried first (HEAD second) so the body is available for the checks below.
    Beyond the plain status code this now catches three things a status code hides:
      * soft 404 — a 200 whose own <title>/<h1> says "not found"  -> broken
      * JS shell — a 200 with no <title> and no <h1> at all        -> blocked
        (this is exactly how https://www.pretzel.rocks/library looked healthy while
         rendering h1 "Page not found" in a browser)
      * a redirect onto a different registrable domain             -> blocked
        (a rebrand such as freepik.com -> magnific.com must be reviewed by a human,
         not silently reported as ok)
    A redirect within the same domain is reported in the detail string instead.
    """
    orig = urlsplit(url)
    last = None
    pending = None  # a network-level `broken` waiting for confirmation on the other method
    for method in ("GET", "HEAD"):
        verdict = None
        network_broken = False
        for attempt in range(RETRIES + 1):
            try:
                code, final, body = probe(url, method)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code in BLOCKED_CODES:
                    verdict = ("blocked", last)
                    break
                if e.code in (404, 410):
                    if method == "GET":
                        return ("broken", last)
                    verdict = None  # 404 on HEAD only: confirm with GET
                    break
                if 500 <= e.code < 600:
                    if attempt < RETRIES:
                        time.sleep(2 * (attempt + 1))
                        continue
                    # Retries exhausted. A 5xx reports the server's own state and proves
                    # nothing about whether the resource still exists, so it is
                    # indeterminate (`blocked`) rather than dead (`broken`).
                    verdict = ("blocked", "%s after %d attempts (server error, not a dead link)"
                               % (last, RETRIES + 1))
                    break
                return ("broken", last)
            except ssl.SSLError as e:
                last = "SSL: %s" % e
                verdict = ("blocked", last)
                break
            except OSError as e:
                msg = str(e)
                last = "%s: %s" % (type(e).__name__, msg)
                if "nodename nor servname" in msg or "Name or service not known" in msg:
                    return ("broken", "DNS failure")  # definitive, and cheap to re-resolve
                if attempt < RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                # Retries exhausted. A refusal normally means nothing is listening, but it can
                # also be one bad CDN edge: rocketstock.com refused urllib while `nc` reached
                # port 443 three times and curl got a clean 302 seconds earlier. So a refusal
                # is confirmed on the second method before it is allowed to fail CI.
                refused = "refused" in msg.lower()
                network_broken = refused
                verdict = (("broken", "connection refused after %d attempts" % (RETRIES + 1))
                           if refused else ("blocked", last))
                break

            # --- 2xx/3xx: look past the status code -----------------------------
            fin = urlsplit(final or url)
            if registrable(fin.netloc) != registrable(orig.netloc):
                return ("blocked", "%d but redirects to another domain: %s" % (code, final))
            if code in CONTENTLESS_OK:
                return ("blocked", "%d: accepted but no document returned (bot queue or empty "
                                   "response) — open in a browser" % code)
            if body:
                soft, text, had_marker = inspect_body(body)
                if soft:
                    return ("broken", "soft 404 (page says: %s)" % text)
                if not had_marker and not expect_js:
                    return ("blocked", "%d but HTML has no <title>/<h1> (JS shell?) — open in a browser" % code)
                if title_sink is not None and had_marker and text:
                    # Reported out of band so this function keeps its (status, detail) shape
                    # for every other caller. See TITLE_STATE_PATH for what uses it.
                    title_sink.append(text)
            moved = "" if (final or url).rstrip("/") == url.rstrip("/") else " -> %s" % final
            return ("ok", "%d%s" % (code, moved))
        if verdict:
            if network_broken:
                # Do not fail CI on one method's network error; let the other method confirm.
                pending = verdict
                continue
            if method == "GET":
                return verdict
            continue  # HEAD was the fallback and it is blocked too
    return pending or ("blocked", last or "unknown")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", action="append", help="only check this category slug (repeatable)")
    ap.add_argument("--no-proxy", action="store_true",
                    help="ignore environment/system proxies (changes which sites are "
                         "reachable; see the NO_PROXY comment — neither mode matches CI)")
    ap.add_argument("--status-file", metavar="PATH",
                    help="write a JSON summary (date/checked/ok/blocked/broken) for the "
                         "README's link-verification badge; linkcheck.yml commits it")
    ap.add_argument("--titles-file", metavar="PATH", default=str(TITLE_STATE_PATH),
                    help="where the domain-takeover fingerprints live (default: %s)"
                         % TITLE_STATE_PATH.name)
    ap.add_argument("--accept-titles", action="store_true",
                    help="take this run's titles as the new baseline — use it after looking at "
                         "an entry the takeover check flagged")
    ap.add_argument("--no-titles", action="store_true",
                    help="skip the domain-takeover check entirely (nothing is read or written)")
    args = ap.parse_args()

    global NO_PROXY
    NO_PROXY = args.no_proxy
    proxies = urllib.request.getproxies()
    if NO_PROXY:
        print("Proxy: disabled (--no-proxy). Note this is not how CI runs either; see NO_PROXY.")
    elif proxies:
        print("Proxy: using %s — its exit IP is bot-walled by some sites, so verdicts are "
              "indicative only. CI's weekly run is the baseline." % (proxies.get("https") or proxies),
              file=sys.stderr)
    else:
        print("Proxy: none.")

    items = collect(args.slug)
    if not items:
        print("No URLs found.", file=sys.stderr)
        sys.exit(2)

    print("Checking %d URLs (%d workers)..." % (len(items), MAX_WORKERS))
    results = []
    sinks = {}  # url -> [title], filled in by check_one; one list per future, so no sharing
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [(ex.submit(check_one, url, js, sinks.setdefault(url, [])), slug, name, url)
                   for slug, name, url, js in items]
        for fut, slug, name, url in futures:
            try:
                status, detail = fut.result()
            except Exception as e:  # never crash the run
                status, detail = "blocked", "check crashed: %s" % e
            results.append((status, slug, name, url, detail))
            print("  [%-7s] %-42s %s" % (status, name[:42], detail), flush=True)

    # Domain-takeover pass. A title that lost almost every shared word proves nothing on its
    # own, so it downgrades an `ok` to `blocked` — never to `broken`, and never a build
    # failure — and the old fingerprint is kept so the item stays flagged until a human either
    # fixes the entry or says --accept-titles.
    observed = {url: seen[0] for url, seen in sinks.items() if seen}
    previous = {} if args.no_titles else load_titles(args.titles_file)
    changes, updated = review_titles(previous, observed, accept=args.accept_titles)
    reviewed = []
    if changes:
        by_url = {url: (old, new) for url, old, new, _ in changes}
        adjusted = []
        for status, slug, name, url, detail in results:
            if url in by_url:
                old, new = by_url[url]
                reviewed.append((slug, name, url, old, new))
                if status == "ok":
                    detail = ("title changed since a previous run: %r -> %r (domain takeover "
                              "or rebrand?) — open it in a browser, then re-run with "
                              "--accept-titles" % (old, new))
                    status = "blocked"
            adjusted.append((status, slug, name, url, detail))
        results = adjusted

    if reviewed:
        print("\nTitles that moved too far to trust (old fingerprint kept):")
        for slug, name, url, old, new in sorted(reviewed, key=lambda r: r[1]):
            print("  %-40s %r -> %r" % (name[:40], old, new))

    if not args.no_titles:
        import datetime
        import json as _json
        Path(args.titles_file).write_text(
            _json.dumps({"version": TITLE_STATE_VERSION,
                         "date": datetime.date.today().isoformat(),
                         "titles": updated}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print("Titles: recorded %d fingerprint(s) in %s" % (len(updated), args.titles_file))

    ok = [r for r in results if r[0] == "ok"]
    blocked = [r for r in results if r[0] == "blocked"]
    broken = [r for r in results if r[0] == "broken"]

    print("\nSummary: %d ok, %d blocked (needs manual check), %d broken%s"
          % (len(ok), len(blocked), len(broken),
             ", %d title change(s) to review" % len(reviewed) if reviewed else ""))

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
            if reviewed:
                # Its own section, above the noise: a takeover is the one failure here that a
                # green status code cannot express.
                f.write("### Titles that changed (review)\n\n"
                        "| Category | Resource | URL | Was | Now |\n"
                        "| --- | --- | --- | --- | --- |\n")
                for slug, name, url, old, new in sorted(reviewed, key=lambda r: r[1]):
                    f.write("| %s | %s | %s | %s | %s |\n" % (slug, name, url, old, new))
                f.write("\n")

    review_path = os.environ.get("LINKCHECK_REVIEW")
    if review_path:
        lines = ["## Titles that changed (%d)" % len(reviewed), ""]
        if reviewed:
            lines += ["These entries answered 200 with a title that no longer resembles the one "
                      "recorded last run. That is what a domain takeover looks like from here - "
                      "and also what a rebrand looks like, which is why a human decides.", "",
                      "| Category | Resource | URL | Was | Now |",
                      "| --- | --- | --- | --- | --- |"]
            for slug, name, url, old, new in sorted(reviewed, key=lambda r: r[1]):
                lines.append("| %s | %s | %s | %s | %s |" % (slug, name, url, old, new))
            lines += ["", "Open each one in a browser. If the resource is still there, accept the "
                          "new title by re-running the check with --accept-titles (and the same "
                          "--slug filters). If it is gone, or the domain changed hands, fix or "
                          "remove the entry."]
        else:
            lines.append("No title moved far enough to review.")
        with open(review_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    report_path = os.environ.get("LINKCHECK_REPORT")
    if report_path:
        lines = ["## Broken links (%d)" % len(broken), "",
                 "| Category | Resource | URL | Detail |",
                 "| --- | --- | --- | --- |"]
        for _, slug, name, url, detail in sorted(broken, key=lambda r: (r[1], r[2])):
            lines.append("| %s | %s | %s | %s |" % (slug, name, url, detail))
        lines += ["", "OK: %d \u00b7 Blocked (manual check): %d" % (len(ok), len(blocked))]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    status_path = args.status_file
    if status_path:
        # Written even when links are broken, so the badge can say so: a stale "0 broken"
        # would be worse than a red one. build_readme.py reads this and refuses nothing —
        # it renders the date next to the count precisely so an old run cannot pass as new.
        import datetime
        import json as _json
        payload = {
            "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
            "checked": len(results),
            "ok": len(ok),
            "blocked": len(blocked),
            "broken": len(broken),
            # Titles that changed beyond recognition this run. Informational: they are already
            # counted inside blocked, and build_readme.py only reads the keys it knows.
            "review": len(reviewed),
        }
        with open(status_path, "w", encoding="utf-8") as f:
            f.write(_json.dumps(payload, indent=2) + "\n")
        print("Status: wrote %s %s" % (status_path, payload))

    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
