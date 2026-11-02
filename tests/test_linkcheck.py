"""Unit tests for the link checker's classification (scripts/linkcheck.py).

Almost every rule here exists because a status code lied: a 200 whose own title says "page
not found", a 200 that is an empty JavaScript shell, a redirect onto a different company's
domain, a 403 that means "bot wall" rather than "gone". These are the branches that decide
whether the weekly job files an issue, so they are worth pinning. License: CC0.
"""
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import linkcheck  # noqa: E402

#: Verbatim from the case that motivated the takeover check: another directory still lists
#: Teeny Icons while teenyicons.com serves this, and every status-code check calls it fine.
GAMBLING_TITLE = "Sòng Bạc 18BET: Thỏa Mãn Đam Mê Cá Cược Hiện Đại"


class RegistrableTests(unittest.TestCase):
    def test_two_label_hosts_pass_through(self):
        self.assertEqual(linkcheck.registrable("freepik.com"), "freepik.com")
        self.assertEqual(linkcheck.registrable("www.freepik.com"), "freepik.com")

    def test_subdomains_collapse_to_the_same_identity(self):
        self.assertEqual(linkcheck.registrable("a.b.example.com"), "example.com")

    def test_port_is_ignored(self):
        self.assertEqual(linkcheck.registrable("example.com:8443"), "example.com")

    def test_single_label_host_is_returned_as_is(self):
        self.assertEqual(linkcheck.registrable("localhost"), "localhost")


class StripTagsTests(unittest.TestCase):
    def test_tags_go_and_whitespace_collapses(self):
        self.assertEqual(linkcheck.strip_tags("<b>Page</b>\n   not   found"), "Page not found")


class InspectBodyTests(unittest.TestCase):
    def test_a_200_that_says_not_found_is_a_soft_404(self):
        soft, text, marker = linkcheck.inspect_body(
            "<html><head><title>Page not found</title></head></html>")
        self.assertTrue(soft)
        self.assertIn("not found", text)
        self.assertTrue(marker)

    def test_an_h1_alone_can_say_it_too(self):
        soft, _, _ = linkcheck.inspect_body("<html><body><h1>404 - Not Found</h1></body></html>")
        self.assertTrue(soft)

    def test_a_normal_page_is_not_soft(self):
        soft, text, marker = linkcheck.inspect_body("<title>Poly Haven</title>")
        self.assertFalse(soft)
        self.assertEqual(text, "Poly Haven")
        self.assertTrue(marker)

    def test_no_title_and_no_h1_is_reported_as_markerless(self):
        soft, text, marker = linkcheck.inspect_body("<html><body><div id=app></div></body></html>")
        self.assertFalse(soft)
        self.assertFalse(marker)
        self.assertEqual(text, "")


def fake_probe(return_value):
    def _probe(url, method):
        if isinstance(return_value, Exception):
            raise return_value
        return return_value
    return _probe


class CheckOneTests(unittest.TestCase):
    URL = "https://example.com/page"

    def run_check(self, value, expect_js=False):
        with mock.patch.object(linkcheck, "probe", fake_probe(value)), \
             mock.patch.object(linkcheck.time, "sleep", lambda *_: None):
            return linkcheck.check_one(self.URL, expect_js)

    def test_a_404_is_broken(self):
        err = urllib.error.HTTPError(self.URL, 404, "Not Found", {}, None)
        self.assertEqual(self.run_check(err)[0], "broken")

    def test_a_403_is_a_bot_wall_not_a_dead_link(self):
        err = urllib.error.HTTPError(self.URL, 403, "Forbidden", {}, None)
        self.assertEqual(self.run_check(err)[0], "blocked")

    def test_a_200_that_says_not_found_is_broken(self):
        status, detail = self.run_check((200, self.URL, "<title>Page not found</title>"))
        self.assertEqual(status, "broken")
        self.assertIn("soft 404", detail)

    def test_a_200_with_no_title_is_a_js_shell(self):
        status, detail = self.run_check((200, self.URL, "<div id=app></div>"))
        self.assertEqual(status, "blocked")
        self.assertIn("JS shell", detail)

    def test_the_same_page_is_ok_once_a_human_flagged_it_as_js(self):
        status, _ = self.run_check((200, self.URL, "<div id=app></div>"), expect_js=True)
        self.assertEqual(status, "ok")

    def test_redirect_onto_another_domain_needs_a_human(self):
        status, detail = self.run_check((200, "https://magnific.com/", "<title>Magnific</title>"))
        self.assertEqual(status, "blocked")
        self.assertIn("another domain", detail)

    def test_same_domain_redirect_is_ok_but_reported(self):
        status, detail = self.run_check(
            (200, "https://example.com/page2", "<title>Moved</title>"))
        self.assertEqual(status, "ok")
        self.assertIn("->", detail)

    def test_a_202_is_accepted_but_not_yet_a_document(self):
        self.assertEqual(self.run_check((202, self.URL, ""))[0], "blocked")

    def test_a_dns_failure_is_definitive(self):
        err = OSError("nodename nor servname provided, or not known")
        self.assertEqual(self.run_check(err)[0], "broken")

    def test_a_connection_refusal_is_confirmed_on_the_other_method(self):
        # Both methods refuse: the entry is reported broken, but only after the second
        # method has had its say (one bad CDN edge must not fail the build).
        err = OSError("[Errno 61] Connection refused")
        self.assertEqual(self.run_check(err)[0], "broken")

    def test_an_ssl_error_is_indeterminate(self):
        import ssl
        self.assertEqual(self.run_check(ssl.SSLError("certificate verify failed"))[0], "blocked")


class TitleFingerprintTests(unittest.TestCase):
    """The domain-takeover check: a 200 whose content is no longer the resource."""

    def test_tokens_drop_punctuation_short_words_and_bare_numbers(self):
        self.assertEqual(linkcheck.title_tokens("Pixabay - Free Images (2024)!"),
                         {"pixabay", "free", "images"})

    def test_identical_titles_are_identical(self):
        self.assertEqual(linkcheck.title_similarity("Nord", "Nord"), 1.0)

    def test_nothing_in_common_is_zero(self):
        self.assertEqual(linkcheck.title_similarity(
            "TeenyIcons - 1px icon set", GAMBLING_TITLE), 0.0)

    def test_a_suffix_or_redesign_keeps_enough_overlap(self):
        for old, new in (("Pixabay", "Pixabay - Free Images"),
                         ("Poly Haven", "Poly Haven | HDRIs, Textures, Models"),
                         ("Openverse", "Openverse | WordPress.org")):
            self.assertGreaterEqual(linkcheck.title_similarity(old, new),
                                    linkcheck.TITLE_CHANGE_THRESHOLD, (old, new))

    def test_an_empty_title_scores_zero_rather_than_crashing(self):
        self.assertEqual(linkcheck.title_similarity("", "Nord"), 0.0)
        self.assertEqual(linkcheck.title_similarity("Nord", ""), 0.0)

    # -- CJK (#16) --------------------------------------------------------------------
    # A title made only of Chinese characters used to tokenise to nothing, so it scored
    # 0.0 against itself and every Chinese entry was reported as changed on every run.
    CJK_TITLE = "阿里巴巴普惠体"

    def test_cjk_titles_tokenise_as_two_character_grams(self):
        self.assertEqual(linkcheck.title_tokens(self.CJK_TITLE),
                         {"阿里", "里巴", "巴巴", "巴普", "普惠", "惠体"})

    def test_a_one_character_cjk_title_keeps_its_character(self):
        # Bigrams need two characters; a one-character title must not vanish again.
        self.assertEqual(linkcheck.title_tokens("云"), {"云"})
        self.assertEqual(linkcheck.title_similarity("云", "云"), 1.0)

    def test_identical_cjk_titles_are_identical(self):
        self.assertEqual(linkcheck.title_similarity(self.CJK_TITLE, self.CJK_TITLE), 1.0)

    def test_mixed_cjk_and_latin_titles_are_unchanged_by_case(self):
        mixed = "Alibaba PuHuiTi (阿里巴巴普惠体)"
        self.assertEqual(linkcheck.title_similarity(mixed, mixed), 1.0)
        self.assertEqual(linkcheck.title_similarity(mixed, mixed.upper()), 1.0)

    def test_another_page_on_the_same_cjk_site_stays_above_the_threshold(self):
        # 阿/里/巴 巴巴 vs 图标库: three shared grams out of nine, 1/3.
        similarity = linkcheck.title_similarity(self.CJK_TITLE, "阿里巴巴图标库")
        self.assertAlmostEqual(similarity, 1 / 3.0)
        self.assertGreaterEqual(similarity, linkcheck.TITLE_CHANGE_THRESHOLD)

    def test_a_cjk_rename_or_suffix_keeps_overlap(self):
        for old, new in (("得意黑", "得意黑 - Smiley Sans"),
                         ("霞鹜文楷", "霞鹜文楷 GB"),
                         ("思源黑体", "思源宋体")):
            self.assertGreaterEqual(linkcheck.title_similarity(old, new),
                                    linkcheck.TITLE_CHANGE_THRESHOLD, (old, new))

    def test_a_shared_common_word_is_not_enough_to_pass_as_unchanged(self):
        # 「免费」 appears on half the freebie sites in China. Matching single characters
        # scored this pair 0.25 — above the threshold, so the takeover passed as unchanged;
        # grams score it 0.14 and it is flagged.
        self.assertLess(linkcheck.title_similarity("免费素材", "免费在线赌场"),
                        linkcheck.TITLE_CHANGE_THRESHOLD)

    def test_a_cjk_title_replaced_by_an_unrelated_one_is_still_zero(self):
        for hijack in (GAMBLING_TITLE, "页面不存在", "澳门新葡京威尼斯人娱乐城"):
            self.assertEqual(linkcheck.title_similarity(self.CJK_TITLE, hijack), 0.0, hijack)

    def test_the_ascii_path_is_untouched_by_the_cjk_rule(self):
        self.assertEqual(linkcheck.title_tokens("Pixabay - Free Images (2024)!"),
                         {"pixabay", "free", "images"})
        self.assertEqual(linkcheck.title_tokens("Nord"), {"nord"})
        self.assertEqual(linkcheck.title_similarity("Nord", "Nord | Guitars"), 0.5)

    def test_two_titles_that_both_tokenise_to_nothing_compare_by_string(self):
        # Nothing to tokenise on either side is not evidence of anything: compare the text.
        self.assertEqual(linkcheck.title_similarity("---", "---"), 1.0)
        self.assertEqual(linkcheck.title_similarity("404", " 404 "), 1.0)
        self.assertEqual(linkcheck.title_similarity("---", "404"), 0.0)
        self.assertEqual(linkcheck.title_similarity("", ""), 1.0)

    def test_one_side_tokenising_to_nothing_is_still_zero(self):
        self.assertEqual(linkcheck.title_similarity("---", "Nord"), 0.0)
        self.assertEqual(linkcheck.title_similarity(self.CJK_TITLE, ""), 0.0)
        self.assertEqual(linkcheck.title_similarity("", self.CJK_TITLE), 0.0)


class ReviewTitlesTests(unittest.TestCase):
    def test_a_first_sighting_is_recorded_without_alarm(self):
        changes, updated = linkcheck.review_titles({}, {"https://a/": "Nord"})
        self.assertEqual(changes, [])
        self.assertEqual(updated, {"https://a/": "Nord"})

    def test_an_unchanged_title_is_just_refreshed(self):
        changes, updated = linkcheck.review_titles({"https://a/": "Nord"}, {"https://a/": "Nord"})
        self.assertEqual(changes, [])
        self.assertEqual(updated["https://a/"], "Nord")

    def test_an_unchanged_cjk_title_is_not_reported(self):
        # The regression behind #16: 阿里巴巴普惠体 scored 0.0 against itself, so every
        # Chinese entry came out of review_titles as a suspected takeover.
        old = "阿里巴巴普惠体"
        changes, updated = linkcheck.review_titles({"https://a/": old}, {"https://a/": old})
        self.assertEqual(changes, [])
        self.assertEqual(updated["https://a/"], old)

    def test_a_cjk_takeover_is_flagged_and_the_old_title_is_kept(self):
        old = "阿里巴巴普惠体"
        changes, updated = linkcheck.review_titles(
            {"https://a/": old}, {"https://a/": GAMBLING_TITLE})
        self.assertEqual(len(changes), 1)
        self.assertLess(changes[0][3], linkcheck.TITLE_CHANGE_THRESHOLD)
        self.assertEqual(updated["https://a/"], old)

    def test_an_unchanged_title_of_pure_punctuation_is_not_reported(self):
        changes, _ = linkcheck.review_titles({"https://a/": "---"}, {"https://a/": "---"})
        self.assertEqual(changes, [])

    def test_a_takeover_is_flagged_and_the_old_title_is_kept(self):
        old = "TeenyIcons - 1px icon set"
        changes, updated = linkcheck.review_titles(
            {"https://a/": old}, {"https://a/": GAMBLING_TITLE})
        self.assertEqual(len(changes), 1)
        url, was, now, similarity = changes[0]
        self.assertEqual((url, was), ("https://a/", old))
        self.assertLess(similarity, linkcheck.TITLE_CHANGE_THRESHOLD)
        # Keeping the old fingerprint is what makes the alarm repeat next week instead of
        # quietly rebasing onto whatever took the domain over.
        self.assertEqual(updated["https://a/"], old)

    def test_accepting_re_baselines_the_flagged_entry(self):
        changes, updated = linkcheck.review_titles(
            {"https://a/": "TeenyIcons"}, {"https://a/": "Sòng Bạc 18BET"}, accept=True)
        self.assertEqual(changes, [])
        self.assertEqual(updated["https://a/"], "Sòng Bạc 18BET")

    def test_an_entry_we_could_not_reach_this_time_keeps_its_fingerprint(self):
        _, updated = linkcheck.review_titles({"https://gone/": "Nord"}, {})
        self.assertEqual(updated["https://gone/"], "Nord")

    def test_loading_a_missing_or_broken_state_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(linkcheck.load_titles(Path(tmp, "nope.json")), {})
            broken = Path(tmp, "broken.json")
            broken.write_text("{ not json", encoding="utf-8")
            self.assertEqual(linkcheck.load_titles(broken), {})


class TitleSinkTests(unittest.TestCase):
    """check_one keeps its (status, detail) shape; the title comes back out of band."""

    URL = "https://example.com/page"

    def run_check(self, value, expect_js=False):
        sink = []
        with mock.patch.object(linkcheck, "probe", fake_probe(value)), \
             mock.patch.object(linkcheck.time, "sleep", lambda *_: None):
            result = linkcheck.check_one(self.URL, expect_js, sink)
        return result, sink

    def test_a_healthy_page_reports_its_title(self):
        (status, _), sink = self.run_check((200, self.URL, "<title>Nord</title>"))
        self.assertEqual(status, "ok")
        self.assertEqual(sink, ["Nord"])

    def test_a_js_shell_reports_nothing_to_fingerprint(self):
        (status, _), sink = self.run_check((200, self.URL, "<div id=app></div>"))
        self.assertEqual(status, "blocked")
        self.assertEqual(sink, [])

    def test_a_cross_domain_redirect_reports_nothing(self):
        (status, _), sink = self.run_check((200, "https://magnific.com/", "<title>Magnific</title>"))
        self.assertEqual(status, "blocked")
        self.assertEqual(sink, [])

    def test_a_soft_404_reports_nothing(self):
        (status, _), sink = self.run_check((200, self.URL, "<title>Page not found</title>"))
        self.assertEqual(status, "broken")
        self.assertEqual(sink, [])

    def test_callers_that_do_not_ask_for_a_title_still_work(self):
        with mock.patch.object(linkcheck, "probe", fake_probe((200, self.URL, "<title>Nord</title>"))):
            self.assertEqual(linkcheck.check_one(self.URL)[0], "ok")


class DataIntegrationTests(unittest.TestCase):
    def test_every_collected_entry_has_a_url_and_a_slug(self):
        items = linkcheck.collect()
        self.assertTrue(items)
        for slug, name, url, js in items:
            self.assertTrue(url.startswith("http"), "%s/%s" % (slug, name))
            self.assertEqual(slug, slug.strip())


if __name__ == "__main__":
    unittest.main()
