"""Unit tests for the entry gate (scripts/check_entry.py).

The rules here are regex and cross-field logic, which is exactly the kind of code that
regresses silently: a widened FLUFF pattern or a dropped coherence rule changes what CI
accepts without changing any output that a reviewer would look at. Stdlib only.
License: CC0.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import check_entry  # noqa: E402

BASE = {
    "name": "Test Resource",
    "url": "https://example.com/",
    "description": "Free assets for video editors.",
    "description_zh": "供视频剪辑使用的免费素材。",
    "license": "CC0",
    "license_family": "cc0",
    "attribution": "not-required",
    "monetization": "allowed",
    # Provenance travels with every entry: the date it was read, and the page it was read at.
    "license_checked": "2026-10-17",
    "license_source": "https://example.com/license",
}


def entry(**over):
    e = dict(BASE)
    e.update(over)
    return e


class LengthAndCharsTests(unittest.TestCase):
    def test_rejects_pipe_which_breaks_the_markdown_table(self):
        errors, warnings = [], []
        check_entry.check_length_and_chars(entry(description="Free | paid assets"), "w", errors, warnings)
        self.assertTrue(any("'|'" in e or "|" in e for e in errors), errors)

    def test_rejects_over_long_description(self):
        errors, warnings = [], []
        check_entry.check_length_and_chars(entry(description="x" * (check_entry.MAX_DESCRIPTION + 1)),
                                           "w", errors, warnings)
        self.assertTrue(any("limit" in e for e in errors), errors)

    def test_flags_untrimmed_value_as_a_warning_not_an_error(self):
        errors, warnings = [], []
        check_entry.check_length_and_chars(entry(notes=" needs credit "), "w", errors, warnings)
        self.assertEqual(errors, [])
        self.assertTrue(any("whitespace" in w for w in warnings), warnings)


class VoiceTests(unittest.TestCase):
    def test_rejects_marketing_superlatives(self):
        for word in ("stunning", "curated", "world-class", "everything you need"):
            errors = []
            check_entry.check_voice(entry(description="A %s library of clips." % word), "w", errors)
            self.assertTrue(errors, "expected %r to be rejected" % word)

    def test_allowlisted_words_are_not_fluff(self):
        # "seamless" is a texturing term of art and "premium" is usually a product name.
        for phrase in ("Seamless tileable textures.", "Flaticon Premium free tier."):
            errors = []
            check_entry.check_voice(entry(description=phrase), "w", errors)
            self.assertEqual(errors, [], phrase)

    def test_rejects_unverifiable_quantity_english_and_chinese(self):
        errors = []
        check_entry.check_voice(entry(description="Thousands of free clips."), "w", errors)
        self.assertTrue(errors)
        errors = []
        check_entry.check_voice(entry(description_zh="数千个免费音效。"), "w", errors)
        self.assertTrue(errors)

    def test_exact_floored_figures_are_allowed(self):
        errors = []
        check_entry.check_voice(entry(description="2,000+ free icons, CC0."), "w", errors)
        self.assertEqual(errors, [])

    def test_license_field_is_never_style_checked(self):
        # A quoted first-party licence name must survive verbatim ("Flaticon Free License").
        errors = []
        check_entry.check_voice(entry(license="Premium Quality License", description="x"), "w", errors)
        self.assertEqual(errors, [])


class CoherenceTests(unittest.TestCase):
    def test_conditional_needs_a_recorded_condition(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(monetization="conditional", notes=None,
                                          attribution="not-required"), "w", errors, warnings)
        self.assertTrue(any("conditional" in e for e in errors), errors)

    def test_conditional_is_explained_by_required_attribution_alone(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(monetization="conditional", notes=None,
                                          attribution="required", license_family="site-free"),
                                    "w", errors, warnings)
        self.assertEqual(errors, [])

    def test_conditional_with_notes_is_accepted(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(monetization="conditional", notes="Free tier needs a link.",
                                          license_family="site-free", attribution="not-required"),
                                    "w", errors, warnings)
        self.assertEqual(errors, [])

    def test_noncommercial_cannot_allow_monetization(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(license_family="noncommercial", monetization="allowed"),
                                    "w", errors, warnings)
        self.assertTrue(any("non-commercial" in e or "noncommercial" in e for e in errors), errors)

    def test_contains_cc0_only_makes_sense_with_mixed(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(contains_cc0=True, license_family="cc0"), "w", errors, warnings)
        self.assertTrue(any("mixed" in e for e in errors), errors)

    def test_cc0_cannot_require_attribution(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(license_family="cc0", attribution="required"), "w", errors, warnings)
        self.assertTrue(any("attribution" in e for e in errors), errors)

    def test_missing_description_zh_is_a_warning_not_an_error(self):
        # Deliberately not a required field: somebody who cannot write Chinese can still
        # land an entry, and that row renders in English on the Chinese page instead.
        self.assertNotIn("description_zh", check_entry.REQUIRED_ENTRY_FIELDS)
        errors, warnings = [], []
        e = entry()
        del e["description_zh"]
        check_entry.check_coherence(e, "w", errors, warnings)
        self.assertEqual(errors, [])
        self.assertTrue(any("description_zh" in w for w in warnings), warnings)

    def test_notes_without_notes_zh_is_only_a_warning(self):
        errors, warnings = [], []
        check_entry.check_coherence(entry(notes="Credit required."), "w", errors, warnings)
        self.assertEqual(errors, [])
        self.assertTrue(any("notes_zh" in w for w in warnings), warnings)


class UrlTests(unittest.TestCase):
    def test_requires_https_and_no_fragment(self):
        errors = []
        check_entry.check_url(entry(url="http://example.com/"), "w", errors, {}, set())
        self.assertTrue(any("https" in e for e in errors))
        errors = []
        check_entry.check_url(entry(url="https://example.com/#free"), "w", errors, {}, set())
        self.assertTrue(any("fragment" in e for e in errors))

    def test_tracking_parameters_are_rejected(self):
        errors = []
        check_entry.check_url(entry(url="https://example.com/?utm_source=directory"), "w", errors, {}, set())
        self.assertTrue(any("tracking" in e for e in errors), errors)

    def test_duplicate_url_ignores_www_case_and_trailing_slash(self):
        errors = []
        seen = {}
        check_entry.check_url(entry(url="https://www.Example.com/free"), "a", errors, seen, set())
        check_entry.check_url(entry(name="Other", url="https://example.com/free/"), "b", errors, seen, set())
        self.assertTrue(any("duplicates" in e for e in errors), errors)

    def test_duplicate_name_is_case_insensitive(self):
        errors = []
        check_entry.check_url(entry(name="Poly Haven"), "a", errors, {}, {"poly haven"})
        self.assertTrue(any("name" in e and "duplicates" in e for e in errors), errors)

    def test_distinct_query_strings_stay_distinct(self):
        # kenney.nl/assets vs kenney.nl/assets?type=audio are two legitimate rows.
        errors = []
        seen = {}
        check_entry.check_url(entry(url="https://kenney.nl/assets"), "a", errors, seen, set())
        check_entry.check_url(entry(name="Other", url="https://kenney.nl/assets?type=audio"),
                              "b", errors, seen, set())
        self.assertEqual(errors, [])


class EvidenceTests(unittest.TestCase):
    def test_requires_a_verbatim_quote(self):
        errors = []
        check_entry.check_evidence({"entry": entry(), "evidence": {"quote": "free", "source": "https://x/"}},
                                   "c", errors)
        self.assertTrue(any("quote" in e for e in errors), errors)

    def test_requires_an_http_source(self):
        errors = []
        check_entry.check_evidence({"entry": entry(),
                                    "evidence": {"quote": "x" * 30, "source": "i read it somewhere"}},
                                   "c", errors)
        self.assertTrue(any("source" in e for e in errors), errors)

    def test_requires_signup_how_unless_unknown(self):
        errors = []
        check_entry.check_evidence({"entry": entry(signup="required"),
                                    "evidence": {"quote": "x" * 30, "source": "https://x/"}},
                                   "c", errors)
        self.assertTrue(any("signup_how" in e for e in errors), errors)
        errors = []
        check_entry.check_evidence({"entry": entry(signup="unknown"),
                                    "evidence": {"quote": "x" * 30, "source": "https://x/"}},
                                   "c", errors)
        self.assertEqual(errors, [])


class LicenceRiskTests(unittest.TestCase):
    """The backlog is worked in risk order, so the ordering is part of the contract."""

    def test_monetized_with_no_attribution_is_the_top_risk(self):
        score, reason = check_entry.licence_risk(
            {"monetization": "allowed", "attribution": "not-required"})
        self.assertEqual(score, 3)
        self.assertIn("monetized", reason)
        self.assertIn("no attribution", reason)

    def test_a_cc0_claim_adds_weight(self):
        score, reason = check_entry.licence_risk(
            {"monetization": "allowed", "attribution": "not-required", "license_family": "cc0"})
        self.assertEqual(score, 4)
        self.assertIn("CC0", reason)

    def test_an_entry_that_already_sends_you_to_the_source_scores_zero(self):
        score, reason = check_entry.licence_risk(
            {"monetization": "conditional", "attribution": "required"})
        self.assertEqual(score, 0)
        self.assertEqual(reason, "no strong claim")

    def test_the_shipped_backlog_is_ordered_by_risk(self):
        _, _, missing, _ = check_entry.license_audit(365)
        scores = [row[0] for row in missing]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_every_unverified_row_explains_itself(self):
        _, _, missing, _ = check_entry.license_audit(365)
        for score, where, reason in missing:
            self.assertTrue(reason.strip(), where)
            self.assertTrue(0 <= score <= 4, where)


class DataDirectoryTests(unittest.TestCase):
    """The shipped data must pass its own gate, offline."""

    def test_every_entry_in_data_passes_the_gate(self):
        _, errors, _ = check_entry.audit_all(offline=True)
        self.assertEqual(errors, [], "\n".join(errors[:10]))


if __name__ == "__main__":
    unittest.main()
