"""Unit tests for the pull-request credit sync (scripts/sync_contributors.py).

The credits file is the project's only thanks list, and it is written by a bot: a wrong
credit, a bot account listed as a person, or a duplicate that shifts everybody else is worse
than a missing one. Every rule that keeps that from happening gets a case here. Stdlib only.
License: CC0.
"""
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_readme  # noqa: E402
import check_pr  # noqa: E402
import sync_contributors as credits  # noqa: E402

TODAY = date(2026, 6, 1)
PULL_URL = "https://github.com/skyzhao1223/free-for-creators/pull/%d"


def pull(number, login, merged="2026-05-0%dT10:00:00Z" % 1, url=None, sha=None):
    return {"number": number, "user": {"login": login}, "merged_at": merged,
            "merge_commit_sha": sha or ("%040d" % number),
            "html_url": url or PULL_URL % number}


def entry(handle="octocat", first="2026-05-01", url=None):
    return {"handle": handle, "first": first, "url": url or PULL_URL % 12}


class ConstantsTests(unittest.TestCase):
    def test_repo_matches_the_generator(self):
        self.assertEqual(credits.REPO, build_readme.REPO)

    def test_the_credits_file_is_not_inside_data(self):
        # data/*.json is read as a category file by load_categories(); a credits file there
        # would fail the entry schema on every run.
        self.assertFalse(str(credits.CONTRIBUTORS_PATH).startswith(str(ROOT / "data")))

    def test_the_shipped_file_is_valid(self):
        data = json.loads(credits.CONTRIBUTORS_PATH.read_text(encoding="utf-8"))
        errors, entries = credits.check_contributors(data)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), len(data["contributors"]))


class ValidationTests(unittest.TestCase):
    def check(self, data):
        return credits.check_contributors(data, today=TODAY)

    def file(self, *rows, **over):
        doc = {"version": 1, "contributors": list(rows)}
        doc.update(over)
        return doc

    def assert_error(self, needle, doc):
        errors, _ = self.check(doc)
        self.assertTrue(any(needle in e for e in errors), (needle, errors))

    def test_a_good_file(self):
        errors, entries = self.check(self.file(entry()))
        self.assertEqual(errors, [])
        self.assertEqual(entries, [entry()])

    def test_an_empty_file_is_valid(self):
        self.assertEqual(self.check(self.file()), ([], []))

    def test_unknown_top_level_key(self):
        self.assert_error("unknown top-level key", self.file(count=1))

    def test_wrong_version(self):
        self.assert_error("version", self.file(**{"version": 2}))

    def test_contributors_must_be_a_list(self):
        self.assert_error("must be a list", self.file(**{"contributors": {}}))

    def test_a_row_must_be_an_object(self):
        self.assert_error("must be an object", self.file("octocat"))

    def test_unknown_field_in_a_row(self):
        self.assert_error("unknown field", self.file(dict(entry(), role="author")))

    def test_missing_field(self):
        self.assert_error("missing", self.file({"handle": "octocat", "first": "2026-05-01"}))

    def test_handle_must_be_bare(self):
        self.assert_error("bare GitHub username", self.file(entry(handle="@octocat")))

    def test_a_bot_account_is_not_a_contributor(self):
        self.assert_error("app account", self.file(entry(handle="dependabot[bot]")))

    def test_a_duplicate_handle_is_rejected_case_insensitively(self):
        self.assert_error("credited twice", self.file(entry(handle="VFSeanm"), entry(handle="vfseanm")))

    def test_first_must_be_a_real_date(self):
        self.assert_error("real YYYY-MM-DD", self.file(entry(first="2026-02-30")))

    def test_first_cannot_be_in_the_future(self):
        self.assert_error("in the future", self.file(entry(first="2026-12-01")))

    def test_url_must_be_a_pull_request_in_this_repository(self):
        self.assert_error("pull request", self.file(entry(url="https://github.com/skyzhao1223/free-for-creators/issues/12")))
        self.assert_error("pull request", self.file(entry(url="https://github.com/other/repo/pull/12")))
        self.assert_error("pull request", self.file(entry(url=PULL_URL % 12 + "?diff=split")))

    def test_only_clean_rows_are_returned(self):
        errors, entries = self.check(self.file(entry(), entry(handle="dependabot[bot]")))
        self.assertTrue(errors)
        self.assertEqual([e["handle"] for e in entries], ["octocat"])

    def test_a_missing_file_reads_as_no_credits(self):
        self.assertEqual(credits.load_contributors("/tmp/definitely-not-here.json"), [])


class CreditFromPullsTests(unittest.TestCase):
    def test_the_earliest_pull_request_becomes_the_credit(self):
        pulls = [pull(12, "octocat", "2026-05-09T10:00:00Z"), pull(9, "octocat", "2026-05-02T10:00:00Z")]
        out = credits.credit_from_pulls(pulls)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], PULL_URL % 9)
        self.assertEqual(out[0]["first"], "2026-05-02")

    def test_pulls_are_returned_in_merge_order(self):
        pulls = [pull(12, "late", "2026-05-09T10:00:00Z"), pull(9, "early", "2026-05-02T10:00:00Z")]
        self.assertEqual([e["handle"] for e in credits.credit_from_pulls(pulls)], ["early", "late"])

    def test_an_unmerged_pull_request_is_skipped(self):
        self.assertEqual(credits.credit_from_pulls([pull(1, "octocat", None)]), [])

    def test_app_accounts_are_skipped(self):
        self.assertEqual(credits.credit_from_pulls([pull(1, "dependabot[bot]")]), [])

    def test_the_excluded_handles_are_skipped(self):
        self.assertEqual(credits.credit_from_pulls([pull(6, "skyzhao1223")], exclude=["skyzhao1223"]), [])
        self.assertEqual(credits.credit_from_pulls([pull(6, "Skyzhao1223")], exclude=["@skyzhao1223"]), [])

    def test_a_pull_request_without_an_author_is_skipped(self):
        self.assertEqual(credits.credit_from_pulls([{"number": 3, "merged_at": "2026-05-01T00:00:00Z"}]), [])


class EligibleAuthorTests(unittest.TestCase):
    """One function decides who a pull request credits; the list and the entry stamp both
    ask it, so they can never disagree."""

    def test_a_merged_pull_request_by_a_person_credits_that_person(self):
        self.assertEqual(credits.eligible_author(pull(1, "octocat")), "octocat")

    def test_an_unmerged_pull_request_credits_nobody(self):
        self.assertIsNone(credits.eligible_author(pull(1, "octocat", None)))

    def test_an_app_account_credits_nobody(self):
        self.assertIsNone(credits.eligible_author(pull(1, "dependabot[bot]")))

    def test_an_excluded_handle_credits_nobody_however_it_is_written(self):
        # The @-form belongs to the exclude list (where a human may write it that way); a
        # GitHub login never carries one.
        for excluded in ("skyzhao1223", "Skyzhao1223", "@skyzhao1223"):
            self.assertIsNone(credits.eligible_author(pull(1, "skyzhao1223"), exclude=[excluded]), excluded)
        self.assertIsNone(credits.eligible_author(pull(1, "SKYzhao1223"), exclude=["skyzhao1223"]))

    def test_a_pull_request_without_an_author_credits_nobody(self):
        self.assertIsNone(credits.eligible_author({"number": 1, "merged_at": "2026-05-01T00:00:00Z"}))


class MergeTests(unittest.TestCase):
    def test_new_credits_are_appended_and_existing_ones_are_never_rewritten(self):
        existing = [entry(handle="vfseanm", first="2025-09-21", url=PULL_URL % 6)]
        merged, added = credits.merge_contributors(existing, [entry(handle="octocat", first="2026-05-01")])
        self.assertEqual([e["handle"] for e in merged], ["vfseanm", "octocat"])
        self.assertEqual(merged[0], existing[0])
        self.assertEqual([e["handle"] for e in added], ["octocat"])

    def test_an_already_credited_handle_is_not_added_again(self):
        existing = [entry(handle="Octocat", first="2026-05-01")]
        merged, added = credits.merge_contributors(existing, [entry(handle="octocat", first="2026-04-01")])
        self.assertEqual(added, [])
        self.assertEqual(len(merged), 1)

    def test_the_file_order_is_by_first_credit_then_handle(self):
        found = [entry(handle="zeta", first="2026-05-09"), entry(handle="alpha", first="2026-05-02"),
                 entry(handle="beta", first="2026-05-02")]
        merged, _ = credits.merge_contributors([], found)
        self.assertEqual([e["handle"] for e in merged], ["alpha", "beta", "zeta"])


class RenderTests(unittest.TestCase):
    def test_the_file_ends_with_a_newline_and_parses_back(self):
        text = credits.render_file([entry()])
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text)["contributors"], [entry()])

    def test_the_key_order_is_handle_first_url(self):
        text = credits.render_file([entry()])
        self.assertLess(text.index('"handle"'), text.index('"first"'))
        self.assertLess(text.index('"first"'), text.index('"url"'))


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
    def test_unmerged_pull_requests_are_dropped_and_a_short_page_ends_the_walk(self):
        page = [pull(2, "octocat", "2026-05-02T10:00:00Z"), pull(3, "someone", None)]
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(page)) as opener:
            pulls = credits.fetch_merged_pulls("o/r", "tok")
        self.assertEqual([p["number"] for p in pulls], [2])
        self.assertEqual(opener.call_count, 1)
        request = opener.call_args[0][0]
        self.assertIn("/repos/o/r/pulls?state=closed", request.full_url)
        self.assertIn("Bearer tok", request.headers["Authorization"])

    def test_an_api_error_is_fatal_rather_than_silently_empty(self):
        error = urllib.error.HTTPError("u", 403, "no", {}, None)
        error.read = lambda: b"bad token"
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SystemExit):
                with mock.patch("sys.stderr"):
                    credits.fetch_merged_pulls("o/r", "tok")


class ReadmeIntegrationTests(unittest.TestCase):
    def categories(self):
        return [{
            "category": "Fonts", "slug": "fonts", "description": "d",
            "entries": [{
                "name": "Example", "url": "https://example.com/", "description": "x",
                "license": "CC0", "license_family": "cc0", "attribution": "not-required",
                "monetization": "allowed", "submitted_by": "vfseanm",
            }],
        }]

    def render(self, credited, lang="en"):
        with mock.patch.object(build_readme, "load_contributors", return_value=credited):
            with mock.patch("sys.stderr"):
                return build_readme.render(self.categories(), lang)

    def test_a_credited_pull_request_author_reaches_the_readme(self):
        text = self.render([entry(handle="octocat")])
        self.assertIn("- [@octocat](https://github.com/octocat)", text)

    def test_a_handle_credited_twice_appears_once(self):
        # vfseanm is in the entry's submitted_by as well as in the credits file: once beside
        # the entry and once in the list, never twice in the list.
        text = self.render([entry(handle="vfseanm")])
        self.assertEqual(text.count("- [@vfseanm](https://github.com/vfseanm)"), 1)

    def test_the_chinese_readme_gets_the_same_people(self):
        text = self.render([entry(handle="octocat")], "zh")
        self.assertIn("- [@octocat](https://github.com/octocat)", text)


CATEGORY_DOC = {
    "category": "Fonts", "slug": "fonts", "description": "d",
    "entries": [
        {"name": "Example", "url": "https://example.com/", "description": "x", "license": "CC0",
         "license_family": "cc0", "attribution": "not-required", "monetization": "allowed"},
        {"name": "Credited Already", "url": "https://credited.example/", "description": "y",
         "license": "CC0", "license_family": "cc0", "attribution": "not-required",
         "monetization": "allowed", "submitted_by": "someone"},
    ],
}


class DataRoundTripTests(unittest.TestCase):
    """The stamp rewrites a whole data file, which is only safe because every file in data/
    is exactly json.dumps(indent=2). If that ever stops being true, one bot commit would
    show every line of the file as changed — so it is pinned here."""

    def test_every_data_file_round_trips(self):
        for path in sorted(credits.DATA_DIR.glob("*.json")):
            raw = path.read_text(encoding="utf-8")
            again = json.dumps(json.loads(raw), ensure_ascii=False, indent=2) + "\n"
            self.assertEqual(again, raw, path.name)


class ResourcesAddedTests(unittest.TestCase):
    """What counts as a suggestion, decided on the resource rather than on the diff shape."""

    def entries(self, *names_urls):
        return {"fonts.json": dict(CATEGORY_DOC, entries=[
            {"name": name, "url": url} for name, url in names_urls])}

    def test_a_resource_that_was_not_there_before_is_added(self):
        before = self.entries(("Old", "https://old.example/"))
        after = self.entries(("Old", "https://old.example/"), ("New", "https://new.example/"))
        self.assertEqual([e["name"] for e in credits.resources_added(before, after)], ["New"])

    def test_an_edited_entry_is_not_added(self):
        before = self.entries(("Same", "https://same.example/"))
        after = self.entries(("Same", "https://same.example/"))
        after["fonts.json"]["entries"][0]["license_checked"] = "2026-05-01"
        self.assertEqual(credits.resources_added(before, after), [])

    def test_several_edits_at_once_are_not_adds(self):
        # The bug this predicate exists for: check_pr.describe_changes() reports a correction
        # as an add+remove pair as soon as a commit touches more than one entry, which would
        # have credited the author of a licence-date sweep as the suggester.
        before = {"fonts.json": {"entries": [{"name": n, "url": "https://%s.example/" % n}
                                             for n in ("a", "b", "c")]}}
        after = json.loads(json.dumps(before))
        for row in after["fonts.json"]["entries"]:
            row["license_checked"] = "2026-05-01"
        self.assertEqual(credits.resources_added(before, after), [])

    def test_a_renamed_resource_that_keeps_its_url_is_not_added(self):
        before = self.entries(("Original", "https://one.example/"))
        after = self.entries(("Original Renamed", "https://one.example/"))
        self.assertEqual(credits.resources_added(before, after), [])

    def test_a_new_name_and_a_new_url_read_as_a_new_resource(self):
        # check_pr.same_resource() is the repository's own definition of "the same resource":
        # the same name or the same url. A row that changed both is indistinguishable from a
        # removal plus an addition, and is treated as one.
        before = self.entries(("Original", "https://one.example/"))
        after = self.entries(("Original Renamed", "https://two.example/"))
        self.assertEqual([e["name"] for e in credits.resources_added(before, after)], ["Original Renamed"])

    def test_a_new_category_file_counts(self):
        after = self.entries(("New", "https://new.example/"))
        self.assertEqual([e["name"] for e in credits.resources_added({}, after)], ["New"])

    def test_a_ref_that_is_not_in_this_checkout_credits_nobody(self):
        with mock.patch.object(check_pr, "categories_at", side_effect=check_pr.ContractError("nope")):
            self.assertEqual(credits.entries_added_by("deadbeef"), [])


class PlanStampsTests(unittest.TestCase):
    def added(self, entry_rows):
        return {"%040d" % 12: entry_rows}

    def test_it_names_the_entry_and_its_author(self):
        plan = credits.plan_stamps([pull(12, "octocat")], self.added([{"name": "New", "url": "https://new.example/"}]))
        self.assertEqual([(h, e["name"]) for h, e, _ in plan], [("octocat", "New")])

    def test_a_bot_or_an_excluded_author_gets_no_entry_credit(self):
        rows = [{"name": "New", "url": "https://new.example/"}]
        self.assertEqual(credits.plan_stamps([pull(12, "dependabot[bot]")], self.added(rows)), [])
        self.assertEqual(credits.plan_stamps([pull(12, "skyzhao1223")], self.added(rows),
                                             exclude=["skyzhao1223"]), [])

    def test_an_unmerged_pull_request_credits_no_entry(self):
        self.assertEqual(credits.plan_stamps([pull(12, "octocat", None)], self.added([{"name": "N", "url": "https://n.example/"}])), [])

    def test_the_earliest_pull_request_wins_the_same_resource(self):
        rows = [{"name": "New", "url": "https://new.example/"}]
        pulls = [pull(12, "later", "2026-05-09T10:00:00Z", sha="b" * 40),
                 pull(9, "earlier", "2026-05-02T10:00:00Z", sha="a" * 40)]
        plan = credits.plan_stamps(pulls, {"a" * 40: rows, "b" * 40: rows})
        self.assertEqual([h for h, _, _ in plan], ["earlier"])

    def test_the_url_is_compared_the_way_the_directory_compares_it(self):
        plan = credits.plan_stamps(
            [pull(12, "octocat", sha="c" * 40), pull(13, "second", sha="d" * 40)],
            {"c" * 40: [{"name": "New", "url": "https://www.New.example/"}],
             "d" * 40: [{"name": "New", "url": "https://new.example"}]})
        self.assertEqual([h for h, _, _ in plan], ["octocat"])

    def test_an_entry_without_a_url_is_ignored(self):
        self.assertEqual(credits.plan_stamps([pull(12, "octocat")], self.added([{"name": "No url"}])), [])


class ApplyStampsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.path = self.data_dir / "fonts.json"
        self.path.write_text(json.dumps(CATEGORY_DOC, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def stamp(self, handle="octocat", url="https://example.com/", number=12):
        return [(handle, {"name": "Example", "url": url}, pull(number, handle))]

    def test_it_writes_the_field_beside_the_entry(self):
        applied = credits.apply_stamps(self.stamp(), self.data_dir)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(doc["entries"][0]["submitted_by"], "octocat")
        self.assertEqual([a["file"] for a in applied], ["fonts.json"])
        self.assertEqual(applied[0]["number"], 12)

    def test_an_entry_that_already_names_someone_is_left_alone(self):
        credits.apply_stamps(self.stamp(url="https://credited.example/"), self.data_dir)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(doc["entries"][1]["submitted_by"], "someone")

    def test_the_rest_of_the_file_is_byte_identical(self):
        before = json.loads(self.path.read_text(encoding="utf-8"))
        credits.apply_stamps(self.stamp(), self.data_dir)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(before["entries"][1], after["entries"][1])
        self.assertEqual({k: before[k] for k in ("category", "slug", "description")},
                         {k: after[k] for k in ("category", "slug", "description")})
        self.assertTrue(self.path.read_text(encoding="utf-8").endswith("}\n"))

    def test_a_file_with_no_match_is_not_rewritten(self):
        other = self.data_dir / "music.json"
        other.write_text(json.dumps({"slug": "music", "entries": []}, indent=2) + "\n", encoding="utf-8")
        credits.apply_stamps(self.stamp(url="https://not-listed.example/"), self.data_dir)
        self.assertEqual(json.loads(other.read_text(encoding="utf-8")), {"slug": "music", "entries": []})
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("submitted_by", doc["entries"][0])

    def test_an_empty_plan_touches_nothing(self):
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(credits.apply_stamps([], self.data_dir), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
