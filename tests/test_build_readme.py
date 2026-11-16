"""Unit tests for the generator and its validation (scripts/build_readme.py).

load_categories() is the only thing standing between a bad entry and main, and its rules
are almost all edge cases: a URL that differs only by 'www.' or a trailing slash, a query
string that is load-bearing, a fragment, a tracking parameter. Each of those was a real
escape before it was a rule. Stdlib only. License: CC0.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import build_readme  # noqa: E402

GOOD_ENTRY = {
    "name": "Example",
    "url": "https://example.com/",
    "description": "Free clips for editors.",
    "license": "CC0",
    "license_family": "cc0",
    "attribution": "not-required",
    "monetization": "allowed",
}


def category(slug="example", entries=None, **over):
    doc = {
        "category": "Example",
        "slug": slug,
        "description": "Example category.",
        "entries": entries if entries is not None else [dict(GOOD_ENTRY)],
    }
    doc.update(over)
    return doc


class UrlKeyTests(unittest.TestCase):
    def test_drops_www_case_and_trailing_slash(self):
        for variant in ("https://www.Example.com/free/", "https://example.com/free",
                        "HTTPS://example.com/free/"):
            self.assertEqual(build_readme.url_key(variant), "https://example.com/free", variant)

    def test_keeps_the_query_string(self):
        # kenney.nl/assets and kenney.nl/assets?type=audio are two real, distinct rows.
        self.assertNotEqual(build_readme.url_key("https://kenney.nl/assets"),
                            build_readme.url_key("https://kenney.nl/assets?type=audio"))


class SlugifyTests(unittest.TestCase):
    def test_matches_github_anchors(self):
        self.assertEqual(build_readme.slugify("Graphics & Icons"), "graphics--icons")
        self.assertEqual(build_readme.slugify("3D & Game Assets"), "3d--game-assets")
        self.assertEqual(build_readme.slugify("Mockups & Design Templates"), "mockups--design-templates")

    def test_keeps_cjk(self):
        self.assertEqual(build_readme.slugify("字体"), "字体")


class StatsAndDescriptionTests(unittest.TestCase):
    def test_counts_only_what_it_claims(self):
        cats = [category(entries=[
            dict(GOOD_ENTRY, license_family="cc0", monetization="allowed", attribution="not-required"),
            dict(GOOD_ENTRY, name="B", license_family="mixed", monetization="varies",
                 attribution="required", signup="not-required", contains_cc0=True),
        ])]
        stats = build_readme.compute_stats(cats)
        self.assertEqual(stats["monetizable"], 1)
        self.assertEqual(stats["no_attribution"], 1)
        self.assertEqual(stats["no_signup"], 1)
        self.assertEqual(stats["open"], 1)
        self.assertEqual(stats["open_mixed"], 1)

    def test_repo_description_carries_the_count_and_fits_github(self):
        desc = build_readme.repo_description(205)
        self.assertIn("205", desc)
        self.assertLessEqual(len(desc), 350)

    def test_repo_description_refuses_to_exceed_the_limit(self):
        with mock.patch.object(build_readme, "REPO_DESCRIPTION", "x" * 400):
            with self.assertRaises(SystemExit):
                build_readme.repo_description(1)


class ValidationTests(unittest.TestCase):
    """load_categories() against a throwaway data/ — one bad entry per case."""

    def run_with(self, docs):
        with tempfile.TemporaryDirectory() as tmp:
            for name, doc in docs.items():
                Path(tmp, name).write_text(json.dumps(doc), encoding="utf-8")
            slugs = [Path(n).stem for n in docs]
            with mock.patch.object(build_readme, "DATA_DIR", Path(tmp)), \
                 mock.patch.object(build_readme, "CATEGORY_ORDER", slugs):
                return build_readme.load_categories()

    def assert_rejected(self, entry_over=None, doc_over=None, needle=""):
        doc = category()
        if doc_over:
            doc.update(doc_over)
        if entry_over:
            bad = dict(GOOD_ENTRY)
            bad.update(entry_over)
            doc["entries"] = [bad]
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.run_with({"example.json": doc})

    def test_accepts_a_valid_category(self):
        cats = self.run_with({"example.json": category()})
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]["entries"][0]["name"], "Example")

    def test_rejects_a_tracking_parameter(self):
        self.assert_rejected({"url": "https://example.com/?utm_source=directory"})

    def test_rejects_a_duplicate_url(self):
        doc = category(entries=[dict(GOOD_ENTRY), dict(GOOD_ENTRY, name="Copy")])
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.run_with({"example.json": doc})

    def test_rejects_plain_http(self):
        self.assert_rejected({"url": "http://example.com/"})

    def test_rejects_a_fragment(self):
        self.assert_rejected({"url": "https://example.com/#free"})

    def test_rejects_an_unknown_field(self):
        self.assert_rejected({"licence": "CC0"})

    def test_rejects_a_missing_required_field(self):
        self.assert_rejected({"monetization": None})

    def test_rejects_an_enum_value_outside_the_schema(self):
        self.assert_rejected({"attribution": "maybe"})

    def test_rejects_an_over_long_description(self):
        self.assert_rejected({"description": "x" * (build_readme.MAX_DESCRIPTION + 1)})

    def test_a_null_chinese_description_does_not_crash_the_validator(self):
        # description_zh is optional, but a JSON null is still not a string: the length
        # check must not reach len(None) and turn a clear message into a TypeError.
        with mock.patch("sys.stderr"):
            cats = self.run_with({"example.json": category(
                entries=[dict(GOOD_ENTRY, description_zh=None)])})
        self.assertEqual(len(cats[0]["entries"]), 1)

    def test_rejects_a_pipe_in_any_field(self):
        self.assert_rejected({"notes": "free | paid"})

    def test_rejects_a_slug_that_disagrees_with_the_filename(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.run_with({"other.json": category(slug="example")})

    def test_rejects_a_data_file_missing_from_category_order(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.run_with({"example.json": category(), "extra.json": category(slug="extra")})

    def test_rejects_a_bad_boolean(self):
        self.assert_rejected({"contains_cc0": "yes", "license_family": "mixed"})


class ShippedDataTests(unittest.TestCase):
    """data/ and CATEGORY_ORDER must agree, and every file must load."""

    def test_every_category_in_order_has_a_file_and_nothing_else_does(self):
        on_disk = {p.stem for p in build_readme.DATA_DIR.glob("*.json")}
        self.assertEqual(on_disk, set(build_readme.CATEGORY_ORDER))

    def test_shipped_data_is_valid(self):
        with mock.patch("sys.stderr"):
            categories = build_readme.load_categories()
        self.assertEqual([c["slug"] for c in categories], build_readme.CATEGORY_ORDER)
        self.assertTrue(all(c["entries"] for c in categories))


if __name__ == "__main__":
    unittest.main()
