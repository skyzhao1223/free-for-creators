#!/usr/bin/env python3
"""Link-check ONLY the URLs newly added by a pull request.

Designed for `pull_request_target`: this script runs from the BASE checkout.
PR content is read exclusively through git objects (`git show <ref>:<file>`)
and is never executed.

Env:
    PR_REF     Git ref holding the PR head (default: FETCH_HEAD)
    BASE_REF   Override the merge-base (used for local testing)
    PR_REPORT  Path to write the Markdown report consumed by the PR comment step

Exit codes: 0 = no broken new links (or nothing to check), 1 = broken links.
License: CC0.
"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from linkcheck import check_one  # noqa: E402  (reuse probe/classification logic)

STATUS_ICON = {"ok": "\u2705", "blocked": "\u26a0\ufe0f", "broken": "\u274c"}


def git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (key, value))


def urls_at(ref):
    """Return {url: (slug, name)} for every entry in data/*.json at <ref>."""
    urls = {}
    listing = git("ls-tree", "--name-only", ref, "data/")
    for fname in listing.split():
        try:
            data = json.loads(git("show", "%s:%s" % (ref, fname)))
        except Exception:
            continue  # deleted/unparseable at that ref — nothing to check
        slug = data.get("slug", fname)
        for e in data.get("entries", []):
            url = e.get("url")
            if url:
                urls[url] = (slug, e.get("name", "?"))
    return urls


def write_report(text):
    path = os.environ.get("PR_REPORT")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def main():
    head = os.environ.get("PR_REF", "FETCH_HEAD")
    base = os.environ.get("BASE_REF") or git("merge-base", "HEAD", head).strip()

    old_urls = urls_at(base)
    new_urls = urls_at(head)
    added = sorted(
        [(meta[0], meta[1], url) for url, meta in new_urls.items() if url not in old_urls],
        key=lambda r: (r[0], r[1]),
    )

    if not added:
        print("No new URLs added by this PR (base %s -> head %s)." % (base[:8], head))
        set_output("skip", "true")
        return 0
    set_output("skip", "false")

    print("Checking %d new URL(s)..." % len(added))
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [(ex.submit(check_one, url), slug, name, url) for slug, name, url in added]
        for fut, slug, name, url in futures:
            try:
                status, detail = fut.result()
            except Exception as e:  # never crash the bot
                status, detail = "blocked", "check crashed: %s" % e
            results.append((status, slug, name, url, detail))
            print("  [%-7s] %s (%s)" % (status, name, detail), flush=True)

    ok = [r for r in results if r[0] == "ok"]
    blocked = [r for r in results if r[0] == "blocked"]
    broken = [r for r in results if r[0] == "broken"]

    lines = [
        "Checked **%d** new URL(s): %d ok \u00b7 %d blocked \u00b7 %d broken." % (len(results), len(ok), len(blocked), len(broken)),
        "",
        "| | Category | Resource | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for status, slug, name, url, detail in results:
        lines.append("| %s | `%s` | [%s](%s) | %s |" % (STATUS_ICON[status], slug, name, url, detail))
    if blocked:
        lines += [
            "",
            "> \u26a0\ufe0f *blocked* = the site refused our bot (403/429/timeout). That is common for big sites "
            "(Pixabay, Flaticon\u2026) and does not fail this check \u2014 but please open the link in a browser once to confirm it is alive.",
        ]
    if broken:
        lines += ["", "> \u274c *broken* links must be fixed (correct URL or remove the entry) before merging."]
    write_report("\n".join(lines) + "\n")

    print("Summary: %d ok, %d blocked, %d broken" % (len(ok), len(blocked), len(broken)))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
