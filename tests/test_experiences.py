"""Unit tests for the creator field notes (scripts/experiences.py).

The store is small but its rules are the whole moderation policy: a note only reaches the
site if it anchors to a real entry, carries a public GitHub thread, and reads like a story
rather than a slogan. Each rule below is one of those promises. Stdlib only. License: CC0.
"""
import json
import sys
import unittest
from datetime import date
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_readme  # noqa: E402
import experiences  # noqa: E402

TODAY = date(2026, 6, 1)

CATEGORIES = [{
    "category": "Fonts", "category_zh": "字体", "slug": "fonts", "description": "d",
    "entries": [{
        "name": "Example Fonts", "url": "https://example.com/",
        "description": "Free typefaces.", "license": "CC0", "license_family": "cc0",
        "attribution": "not-required", "monetization": "allowed",
    }],
}]

GOOD_NOTE = {
    "id": "example-fonts-video-subtitles",
    "entry_url": "https://example.com/",
    "author": "skyzhao1223",
    "lang": "zh",
    "date": "2026-01-05",
    "title": "用它的中文字体做了整套视频字幕",
    "body": "x" * 120,
    "evidence": "https://github.com/skyzhao1223/free-for-creators/issues/12",
    "reviewed_by": "skyzhao1223",
    "reviewed_on": "2026-01-06",
}


def store(*notes):
    return {"version": 1, "notes": list(notes)}


def check(*notes):
    return experiences.check_store(store(*notes), CATEGORIES, today=TODAY)


def note(**over):
    return dict(GOOD_NOTE, **over)


class ConstantsTests(unittest.TestCase):
    def test_repo_matches_the_generators(self):
        self.assertEqual(experiences.REPO, build_readme.REPO)

    def test_the_intake_form_the_ctas_point_at_exists(self):
        form = ROOT / ".github" / "ISSUE_TEMPLATE" / "share-experience.yml"
        self.assertTrue(form.exists(), form)
        self.assertIn("entry_url", form.read_text(encoding="utf-8"))


class AcceptanceTests(unittest.TestCase):
    def test_a_complete_note_passes(self):
        errors, warnings = check(GOOD_NOTE)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_an_empty_store_is_valid(self):
        self.assertEqual(check(), ([], []))

    def test_optional_tags_may_be_absent(self):
        errors, _ = check(note())
        self.assertEqual(errors, [])

    def test_the_shipped_store_loads(self):
        with mock.patch("sys.stderr"):
            categories = build_readme.load_categories()
        notes = experiences.load_notes(categories)
        self.assertIsInstance(notes, list)
        self.assertEqual(experiences.check_store(experiences.load_store(), categories), ([], []))


class RejectionTests(unittest.TestCase):
    """One broken rule per case — each message must name the field it is about."""

    def assert_error(self, needle, *notes):
        errors, _ = check(*notes)
        self.assertTrue(any(needle in e for e in errors), (needle, errors))

    def test_unknown_field(self):
        self.assert_error("unknown field", note(licence="CC0"))

    def test_unknown_top_level_key(self):
        errors, _ = experiences.check_store({"version": 1, "notes": [], "count": 3}, CATEGORIES, today=TODAY)
        self.assertTrue(any("count" in e for e in errors), errors)

    def test_wrong_version(self):
        errors, _ = experiences.check_store({"version": 2, "notes": []}, CATEGORIES, today=TODAY)
        self.assertTrue(any("version" in e for e in errors), errors)

    def test_missing_required_field(self):
        self.assert_error("missing or empty 'evidence'", note(evidence=""))

    def test_duplicate_id(self):
        self.assert_error("duplicate id", note(), note())

    def test_id_must_be_a_slug(self):
        self.assert_error("lower-kebab", note(id="Example Fonts!"))

    def test_entry_url_must_exist_verbatim(self):
        self.assert_error("entry_url", note(entry_url="https://example.com"))

    def test_author_must_be_a_bare_handle(self):
        self.assert_error("author", note(author="@skyzhao1223"))

    def test_lang_is_limited(self):
        self.assert_error("lang", note(lang="ja"))

    def test_date_must_be_real(self):
        self.assert_error("real YYYY-MM-DD", note(date="2026-02-30"))

    def test_date_cannot_be_in_the_future(self):
        self.assert_error("in the future", note(date="2026-12-01", reviewed_on="2026-12-02"))

    def test_title_has_a_floor(self):
        self.assert_error("title must be", note(title="short"))

    def test_title_has_a_ceiling(self):
        self.assert_error("title must be", note(title="t" * (experiences.MAX_TITLE + 1)))

    def test_title_cannot_break_the_readme_list(self):
        self.assert_error("must not contain", note(title="fonts | icons"))

    def test_body_has_a_floor(self):
        self.assert_error("body must be", note(body="too short"))

    def test_body_is_one_paragraph(self):
        self.assert_error("single paragraph", note(body="a" * 60 + chr(10) + "b" * 60))

    def test_body_is_trimmed(self):
        self.assert_error("whitespace", note(body=" " + "x" * 120))

    def test_too_many_tags(self):
        self.assert_error("at most", note(tags=["one", "two", "three", "four"]))

    def test_tag_length(self):
        self.assert_error("each tag", note(tags=["a"]))

    def test_duplicate_tags(self):
        self.assert_error("duplicate tags", note(tags=["video", "video"]))

    def test_evidence_must_be_a_thread_in_this_repo(self):
        self.assert_error("evidence", note(evidence="https://example.com/post/1"))

    def test_evidence_must_be_a_thread_not_a_path(self):
        self.assert_error("evidence", note(evidence="https://github.com/skyzhao1223/free-for-creators"))

    def test_reviewed_on_cannot_precede_the_story(self):
        self.assert_error("precedes", note(date="2026-01-05", reviewed_on="2026-01-04"))

    def test_reviewed_on_cannot_be_in_the_future(self):
        self.assert_error("in the future", note(date="2026-05-05", reviewed_on="2026-12-01"))

    def test_a_note_must_be_an_object(self):
        errors, _ = check("just a string")
        self.assertTrue(any("must be an object" in e for e in errors), errors)

    def test_notes_must_be_a_list(self):
        errors, _ = experiences.check_store({"version": 1, "notes": "nope"}, CATEGORIES, today=TODAY)
        self.assertTrue(any("must be a list" in e for e in errors), errors)


class WarningTests(unittest.TestCase):
    def test_two_notes_by_one_author_on_one_resource_warn(self):
        _, warnings = check(note(id="one-note"), note(id="second-note"))
        self.assertTrue(any("same author" in w for w in warnings), warnings)


class EntryIndexTests(unittest.TestCase):
    def test_index_keys_on_the_url_exactly(self):
        index = experiences.entry_index(CATEGORIES)
        self.assertIn("https://example.com/", index)
        self.assertNotIn("https://example.com", index)
        self.assertEqual(index["https://example.com/"][0]["slug"], "fonts")


if __name__ == "__main__":
    unittest.main()
