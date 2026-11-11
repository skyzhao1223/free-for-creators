"""Unit tests for the field-note collector (scripts/sync_experiences.py).

This script is the only thing standing between a public issue and a published record, so
each rule gets a case: a form field read back from a heading, a confirmation box that was
unticked after submission, a date typed as YYYY-MM, a tag too short to store, an entry url
that is not in the directory, a submission that was already collected last week. Stdlib
only. License: CC0.
"""
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_readme  # noqa: E402
import sync_experiences as sync  # noqa: E402

TODAY = date(2026, 6, 1)
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "share-experience.yml"

CATEGORIES = [{
    "category": "Fonts", "category_zh": "字体", "slug": "fonts", "description": "d",
    "entries": [{
        "name": "Example Fonts", "url": "https://example.com/",
        "description": "Free typefaces.", "license": "CC0", "license_family": "cc0",
        "attribution": "not-required", "monetization": "allowed",
    }],
}]

BODY_TEXT = ("I set a 40 page book in two of these families and embedded the fonts in the "
             "print pdf. The licence allowed the commercial job and the download needed no "
             "account, which is the part the site did not spell out clearly.")

CONFIRMED = ("- [x] This is my own first-hand experience with the resource.\n"
             "- [x] I checked the current licence on the source site myself.\n"
             "- [x] This thread may be published on the site under CC0.")


def form_body(entry="https://example.com/", lang="English", when="2026-04-18",
              title="Shipped a 40-page brand book", body=BODY_TEXT, tags="print, branding",
              credit="_No response_", confirmations=CONFIRMED, extra=""):
    return "\n".join([
        "### Which resource is this about?", "", entry, "",
        "### Language of your write-up", "", lang, "",
        "### When did you use it?", "", when, "",
        "### One line that says what you made (6-90 characters)", "", title, "",
        "### What did you make, and what did you learn? (80-800 characters)", "", body, "",
        "### Tags (optional, up to 3, comma separated)", "", tags, "",
        "### Credit as (optional)", "", credit, "",
        "### Before you post", "", confirmations, extra,
    ])


def issue(number=103, body=None, title="[Experience] Shipped a 40-page brand book",
          url=None, login="another-creator"):
    return {
        "number": number,
        "title": title,
        "html_url": url or "https://github.com/skyzhao1223/free-for-creators/issues/%d" % number,
        "user": {"login": login},
        "body": form_body() if body is None else body,
    }


def store(*notes):
    return {"version": 1, "notes": list(notes)}


class FormContractTests(unittest.TestCase):
    """The parser keys on the form's own headings, so the file and the dict must agree."""

    def setUp(self):
        self.form = FORM.read_text(encoding="utf-8")

    def test_every_heading_the_parser_wants_is_still_in_the_form(self):
        for field, fragment in sync.FIELDS.items():
            self.assertIn(fragment, self.form, "the form no longer has the heading for %s" % field)

    def test_the_form_still_collects_the_resource_url_verbatim(self):
        self.assertIn("id: entry_url", self.form)

    def test_the_confirmation_count_matches_the_boxes_that_must_be_ticked(self):
        confirmations = self.form.split("id: confirmations", 1)[1]
        boxes = [line for line in confirmations.splitlines() if line.strip().startswith("- label:")]
        self.assertEqual(len(boxes), sync.REQUIRED_BOXES, boxes)


class ParseTests(unittest.TestCase):
    def test_reads_every_field_back(self):
        fields = sync.parse_issue_body(form_body())
        self.assertEqual(fields["entry_url"], "https://example.com/")
        self.assertEqual(fields["lang"], "English")
        self.assertEqual(fields["tags"], "print, branding")
        self.assertEqual(fields["confirmations"].count("- [x]"), 3)

    def test_no_response_becomes_empty(self):
        self.assertEqual(sync.parse_issue_body(form_body())["credit_as"], "")

    def test_a_missing_section_is_simply_absent(self):
        self.assertNotIn("tags", sync.parse_issue_body("### Which resource is this about?\n\nhttps://example.com/"))

    def test_the_credit_handle_is_read(self):
        self.assertEqual(sync.parse_issue_body(form_body(credit="@someone"))["credit_as"], "@someone")


class SubmissionTests(unittest.TestCase):
    def test_a_form_submission_is_recognised(self):
        self.assertTrue(sync.is_submission(issue()))

    def test_a_resource_suggestion_is_not(self):
        self.assertFalse(sync.is_submission({"title": "[Resource] Some icons", "body": "### The resource"}))

    def test_a_pull_request_is_never_a_submission(self):
        self.assertFalse(sync.is_submission({"title": "[Experience] x", "pull_request": {"url": "y"}}))

    def test_a_form_body_without_the_title_prefix_still_counts(self):
        raw = issue(title="Shipped a brand book")
        self.assertTrue(sync.is_submission(raw))


class NormalizeTests(unittest.TestCase):
    def test_full_date(self):
        self.assertEqual(sync.normalize_date("2026-03-11"), "2026-03-11")

    def test_month_only_lands_on_the_first(self):
        self.assertEqual(sync.normalize_date("2026-03"), "2026-03-01")

    def test_slashes_and_single_digits(self):
        self.assertEqual(sync.normalize_date("2026/3/7"), "2026-03-07")

    def test_junk_is_not_a_date(self):
        self.assertIsNone(sync.normalize_date("last spring"))

    def test_languages(self):
        self.assertEqual(sync.normalize_lang("中文"), "zh")
        self.assertEqual(sync.normalize_lang("English"), "en")
        self.assertIsNone(sync.normalize_lang("日本語"))

    def test_tags_drop_what_the_store_would_reject(self):
        tags, dropped = sync.normalize_tags("video, a, video, " + "x" * 17 + ", audio")
        self.assertEqual(tags, ["video", "audio"])
        self.assertEqual(len(dropped), 2)

    def test_tags_are_capped_at_three(self):
        tags, dropped = sync.normalize_tags("one, two, three, four")
        self.assertEqual(tags, ["one", "two", "three"])
        self.assertEqual(dropped, ["four"])

    def test_ids_stay_readable_and_unique_for_cjk_titles(self):
        self.assertEqual(sync.note_id(42, "Shipped a Brand Book"), "issue-42-shipped-a-brand-book")
        self.assertEqual(sync.note_id(42, "用它的中文字体做了整套字幕"), "issue-42")


class NoteTests(unittest.TestCase):
    def build(self, raw=None, **over):
        raw = raw or issue()
        fields = sync.parse_issue_body(raw["body"])
        return sync.submission_to_note(raw, fields, CATEGORIES, "skyzhao1223", TODAY)

    def test_a_good_submission_becomes_a_note(self):
        note, warnings = self.build()
        self.assertEqual(warnings, [])
        self.assertEqual(note["entry_url"], "https://example.com/")
        self.assertEqual(note["lang"], "en")
        self.assertEqual(note["date"], "2026-04-18")
        self.assertEqual(note["author"], "another-creator")
        self.assertEqual(note["reviewed_by"], "skyzhao1223")
        self.assertEqual(note["reviewed_on"], "2026-06-01")
        self.assertEqual(note["evidence"], "https://github.com/skyzhao1223/free-for-creators/issues/103")

    def test_credit_as_overrides_the_issue_author(self):
        note, _ = self.build(issue(body=form_body(credit="@someone-else")))
        self.assertEqual(note["author"], "someone-else")

    def test_an_unticked_box_rejects_the_submission(self):
        note, reasons = self.build(issue(body=form_body(confirmations="- [x] one\n- [ ] two")))
        self.assertIsNone(note)
        self.assertIn("confirmation", reasons[0])

    def test_a_month_only_date_is_normalized_before_validation(self):
        note, _ = self.build(issue(body=form_body(when="2026-04")))
        self.assertEqual(note["date"], "2026-04-01")

    def test_an_impossible_date_is_rejected_by_the_store_rules(self):
        note, reasons = self.build(issue(body=form_body(when="2026-02-30")))
        self.assertIsNone(note)
        self.assertIn("real YYYY-MM-DD", reasons[0])

    def test_a_future_date_is_rejected_by_the_store_rules(self):
        note, reasons = self.build(issue(body=form_body(when="2026-12-01")))
        self.assertIsNone(note)
        self.assertIn("future", reasons[0])

    def test_an_entry_url_outside_the_directory_is_rejected(self):
        note, reasons = self.build(issue(body=form_body(entry="https://not-listed.example/")))
        self.assertIsNone(note)
        self.assertIn("entry_url", reasons[0])

    def test_a_short_body_is_rejected(self):
        note, reasons = self.build(issue(body=form_body(body="too short")))
        self.assertIsNone(note)
        self.assertIn("body must be", reasons[0])

    def test_line_breaks_in_the_body_are_collapsed_rather_than_rejected(self):
        note, _ = self.build(issue(body=form_body(body=BODY_TEXT + "\n" + BODY_TEXT)))
        self.assertNotIn(chr(10), note["body"])


class SyncTests(unittest.TestCase):
    def test_a_new_submission_is_collected_with_its_entry_name(self):
        added, skipped, already = sync.sync([issue()], store(), CATEGORIES, "skyzhao1223", TODAY)
        self.assertEqual([a["note"]["id"] for a in added], ["issue-103-shipped-a-40-page-brand-book"])
        self.assertEqual(added[0]["entry_name"], "Example Fonts")
        self.assertEqual(skipped, [])
        self.assertEqual(already, [])

    def test_a_submission_already_in_the_store_is_left_alone(self):
        first, _, _ = sync.sync([issue()], store(), CATEGORIES, "skyzhao1223", TODAY)
        stored = store(*[a["note"] for a in first])
        added, skipped, already = sync.sync([issue()], stored, CATEGORIES, "skyzhao1223", TODAY)
        self.assertEqual((added, skipped), ([], []))
        self.assertEqual([i["number"] for i in already], [103])

    def test_the_ignore_list_wins(self):
        url = issue()["html_url"]
        added, skipped, already = sync.sync([issue()], store(), CATEGORIES, "skyzhao1223", TODAY,
                                            {url: "off topic"})
        self.assertEqual(added, [])
        self.assertIn("off topic", skipped[0][1])

    def test_resource_suggestions_are_invisible(self):
        other = {"number": 99, "title": "[Resource] Some icons", "body": "### The resource\n\nhttps://x/"}
        added, skipped, already = sync.sync([other], store(), CATEGORIES, "skyzhao1223", TODAY)
        self.assertEqual((added, skipped, already), ([], [], []))

    def test_an_id_collision_gets_a_suffix_instead_of_overwriting(self):
        raw = issue()
        first, _, _ = sync.sync([raw], store(), CATEGORIES, "skyzhao1223", TODAY)
        stored = store(*[a["note"] for a in first])
        stored["notes"][0]["evidence"] = "https://github.com/skyzhao1223/free-for-creators/issues/1"
        added, _, _ = sync.sync([raw], stored, CATEGORIES, "skyzhao1223", TODAY)
        self.assertEqual(added[0]["note"]["id"], "issue-103-shipped-a-40-page-brand-book-2")

    def test_two_submissions_about_one_resource_both_survive(self):
        a, b = issue(number=201), issue(number=202)
        added, _, _ = sync.sync([a, b], store(), CATEGORIES, "skyzhao1223", TODAY)
        self.assertEqual(len(added), 2)


class SkippedFileTests(unittest.TestCase):
    def test_a_good_list(self):
        errors, out = sync.check_skipped({"version": 1, "skipped": [
            {"url": "https://github.com/skyzhao1223/free-for-creators/issues/9", "reason": "spam"}]})
        self.assertEqual(errors, [])
        self.assertEqual(out, {"https://github.com/skyzhao1223/free-for-creators/issues/9": "spam"})

    def test_an_empty_list_is_fine(self):
        self.assertEqual(sync.check_skipped({"version": 1, "skipped": []}), ([], {}))

    def test_missing_reason(self):
        errors, _ = sync.check_skipped({"version": 1, "skipped": [
            {"url": "https://github.com/o/r/issues/9"}]})
        self.assertTrue(any("reason" in e for e in errors), errors)

    def test_a_url_that_is_not_a_thread(self):
        errors, _ = sync.check_skipped({"version": 1, "skipped": [
            {"url": "https://example.com/9", "reason": "x"}]})
        self.assertTrue(any("github.com" in e for e in errors), errors)

    def test_a_duplicate_url(self):
        entry = {"url": "https://github.com/o/r/issues/9", "reason": "x"}
        errors, _ = sync.check_skipped({"version": 1, "skipped": [entry, dict(entry)]})
        self.assertTrue(any("twice" in e for e in errors), errors)

    def test_an_unknown_field(self):
        errors, _ = sync.check_skipped({"version": 1, "skipped": [
            {"url": "https://github.com/o/r/issues/9", "reason": "x", "why": "y"}]})
        self.assertTrue(any("unknown field" in e for e in errors), errors)

    def test_a_wrong_version(self):
        errors, _ = sync.check_skipped({"version": 2, "skipped": []})
        self.assertTrue(any("version" in e for e in errors), errors)

    def test_a_missing_ignore_list_reads_as_empty(self):
        self.assertEqual(sync.load_skipped("/tmp/definitely-not-a-file.json"), {})

    def test_the_shipped_ignore_list_is_valid(self):
        errors, _ = sync.check_skipped(json.loads(sync.SKIPPED_PATH.read_text(encoding="utf-8")))
        self.assertEqual(errors, [])


class ReportTests(unittest.TestCase):
    def report(self, issues, stored=None, ignored=None):
        added, skipped, already = sync.sync(issues, stored or store(), CATEGORIES,
                                            "skyzhao1223", TODAY, ignored)
        return sync.render_report(added, skipped, already, "skyzhao1223")

    def test_it_names_the_note_its_resource_and_its_thread(self):
        text = self.report([issue()])
        self.assertIn("Shipped a 40-page brand book", text)
        self.assertIn("Example Fonts", text)
        self.assertIn("issues/103", text)
        self.assertIn("2026-04-18", text)

    def test_it_says_what_merging_means(self):
        text = self.report([issue()])
        self.assertIn("Merging this pull request publishes", text)
        self.assertIn("reviewed_by @skyzhao1223", text)
        self.assertIn("skipped.json", text)

    def test_it_explains_what_was_left_out(self):
        text = self.report([issue(body=form_body(confirmations="- [x] one"))])
        self.assertIn("Left out this week", text)
        self.assertIn("confirmation", text)

    def test_it_says_so_when_there_is_nothing_to_publish(self):
        self.assertIn("No new submissions", self.report([]))

    def test_a_reason_with_a_pipe_or_a_newline_cannot_break_the_report(self):
        text = self.report([issue()], ignored={issue()["html_url"]: "why | not\nreally"})
        self.assertIn("why / not really", text)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTests(unittest.TestCase):
    def test_it_drops_pull_requests_and_stops_on_a_short_page(self):
        page = [{"number": 1, "title": "issue"}, {"number": 2, "title": "pr", "pull_request": {}}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(page)) as opener:
            issues = sync.fetch_issues("o/r", "tok")
        self.assertEqual([i["number"] for i in issues], [1])
        self.assertEqual(opener.call_count, 1)
        request = opener.call_args[0][0]
        self.assertIn("/repos/o/r/issues?", request.full_url)
        self.assertIn("Bearer tok", request.headers["Authorization"])

    def test_an_api_error_is_fatal_rather_than_silently_empty(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 403, "no", {}, None)
        error.read = lambda: b"bad token"
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SystemExit):
                with mock.patch("sys.stderr"):
                    sync.fetch_issues("o/r", "tok")


if __name__ == "__main__":
    unittest.main()
