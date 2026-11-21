"""Unit tests for system/profile_text.py (hit free text: paragraph 1, dated reports, snippets)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "system"))

import dates as D  # noqa: E402
import profile_text as T  # noqa: E402
from contracts import ExtractionResult, FactStatus  # noqa: E402


def years(finding):
    return D.describe_years(finding.specs)


class ListingPhrase(unittest.TestCase):
    def test_primary_then_first_category(self):
        self.assertEqual(T.listing_phrase({"primary_category": "PEP", "categories": ["Sanctions"]}),
                         "a politically exposed person")
        self.assertEqual(T.listing_phrase({"primary_category": None, "categories": ["Sanctions", "PEP"]}),
                         "a sanctioned individual")
        self.assertEqual(T.listing_phrase({"categories": ["Adverse Media"]}), "named in adverse media")
        self.assertEqual(T.listing_phrase({"categories": ["SOE"]}), "a state-owned enterprise official")
        self.assertEqual(T.listing_phrase({"categories": ["Enforcement"]}), "the subject of enforcement action")
        self.assertEqual(T.listing_phrase({"categories": ["Registration"]}), "named on a watchlist")


class Paragraphs(unittest.TestCase):
    def test_blank_lines_split_paragraphs(self):
        self.assertEqual(T.paragraphs("A b.\nC d.\n\nE f.\n  \nG"), ["A b.\nC d.", "E f.", "G"])
        self.assertEqual(T.first_paragraph(None), "")


class ParagraphOne(unittest.TestCase):
    NAMES = ["Michael Earl Tilley", "Elvira Velasco Morillo", "Tomasz Paweł Drozd", "Pere Punti", "Abdul Rahman Yasin"]

    def p1(self, text):
        return T.parse_p1(text, "H-TEST01", self.NAMES)

    def test_age_phrasings(self):
        cases = {
            "Investigators conducted questioning of Michael Earl Tilley in January 2023, he was 46.": "1976 or 1977",
            "Formal charges were brought against Elvira Velasco Morillo, aged 30, in November 1998.": "1967 or 1968",
            "November 1998 saw Elvira Velasco Morillo, a 30-year-old, face charges.": "1967 or 1968",
            "In January 2025, investigators questioned 38-year-old Tomasz Paweł Drozd.": "1986 or 1987",
            "November 1981 marked the date Pere Punti, then 37, was placed under charge.": "1943 or 1944",
            "Pere Punti reached 37 years of age by November 1981, the time he was charged.": "1943 or 1944",
            "The age of Tomasz Paweł Drozd was 38 when he was questioned in January 2025.": "1986 or 1987",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                finding = self.p1(text)
                self.assertEqual(years(finding), want)
                self.assertEqual(finding.specs[0].origin, T.ORIGIN_P1_PROSE)

    def test_birth_sentences(self):
        self.assertEqual(years(self.p1("Abdul Rahman Yasin was born on 10 April 1960.")), "1960-04-10")
        self.assertEqual(years(self.p1("Pere Punti was born in 1952.")), "1952")

    def test_key_value_lines(self):
        text = "Name: Michael Earl Tilley\nSex: M\nDOB: 6/20/1955\nHeight: 5' 10\""
        finding = self.p1(text)
        self.assertEqual(years(finding), "1955-06-20")
        self.assertEqual(finding.specs[0].origin, T.ORIGIN_P1_KV)
        both = self.p1("Name: Pere Punti\nDate of Birth: 05/06/1950")
        self.assertTrue(both.specs[0].is_ambiguous_slash)
        self.assertEqual(years(both), "1950-05-06 or 1950-06-05")

    def test_sentences_about_someone_else_are_ignored(self):
        self.assertFalse(self.p1("John Smith, aged 40, was arrested in May 2010.").found)

    def test_unreadable_cue_is_pending_not_guessed(self):
        finding = self.p1("In 2019 Elvira Velasco Morillo, who was aged 30 in March 2018, was charged.")
        self.assertFalse(finding.found)
        self.assertEqual(len(finding.pending), 1)
        self.assertEqual(finding.pending[0].status, FactStatus.PENDING_CLUE)

    def test_no_cue_no_finding(self):
        self.assertEqual(self.p1("Michael Earl Tilley is a politician from Ohio."), T.DobFinding())
        # born without a date: nothing to read and nothing pending
        self.assertEqual(self.p1("Abdul Rahman Yasin was born in the United States."), T.DobFinding())

    def test_administrative_dates_are_not_births(self):
        # H-FF3096: "Alta: 22.11.2011" is a registration date, not a birth date
        self.assertFalse(self.p1("Abdul Rahman Yasin\nAlta: 22.11.2011\nEstado: activo").found)

    def test_prison_terms_are_not_ages(self):
        finding = self.p1("Elvira Velasco Morillo, aged 30, was sentenced to 12 years in prison in November 1998.")
        self.assertEqual(years(finding), "1967 or 1968")


class DatedReports(unittest.TestCase):
    NOTES = ("According to OFAC; September 10, 2025: designated.\n"
             "According to the U.S. Courts; On April 23, 2013, X was sentenced.\n"
             "According to dawn.com; June 2014: arrested.\n"
             "  According to Reuters; January 8, 2020: charged.\n"
             "As reported on March 3, 2001 by Reuters: fined.\n"
             "According to X; February 30, 2020: not a real day.")

    def test_only_exact_form_counts(self):
        reports = T.dated_reports(self.NOTES)
        self.assertEqual([d.iso_str for d, _ in reports], ["2025-09-10", "2020-01-08"])

    def test_earliest(self):
        self.assertEqual(T.earliest_report_date(self.NOTES).iso_str, "2020-01-08")
        self.assertIsNone(T.earliest_report_date(""))


class SearchResults(unittest.TestCase):
    def sr(self, snippet, rid="SR-000001"):
        return {"result_id": rid, "query": "x", "title": "", "snippet": snippet}

    def test_attribution_needs_phrase_and_country(self):
        phrase, countries = "a politically exposed person", ("Nigeria",)
        ok = T.attribute_search_result(self.sr("In May 2019, Grace Wright, 43, a politically exposed person in Nigeria, appeared."), phrase, countries)
        self.assertTrue(ok.attributed)
        self.assertEqual(years(ok.finding), "1975 or 1976")
        wrong_country = T.attribute_search_result(self.sr("In May 2019, Grace Wright, 43, a politically exposed person in Kenya."), phrase, countries)
        self.assertFalse(wrong_country.attributed)
        self.assertTrue(wrong_country.phrase_ok)
        wrong_phrase = T.attribute_search_result(self.sr("In May 2019, Grace Wright, 43, a sanctioned individual in Nigeria."), phrase, countries)
        self.assertFalse(wrong_phrase.attributed)

    def test_country_by_demonym_or_possessive(self):
        phrase = "a state-owned enterprise official"
        res = T.attribute_search_result(self.sr("Ireland's May 2025 published list highlights Lawal Balogun, 75, who is a state-owned enterprise official."), phrase, ("Ireland",))
        self.assertTrue(res.attributed)
        self.assertEqual(years(res.finding), "1949 or 1950")
        res = T.attribute_search_result(self.sr("A Nigerian report from June 2010 names Ade Bello, 50, a state-owned enterprise official."), phrase, ("Nigeria",))
        self.assertTrue(res.attributed)

    def test_snippet_age_shapes(self):
        cases = {
            "During November 2012, 22-year-old Maryam Sani appeared in a notice.": "1989 or 1990",
            "Denmark regulatory records in March 1999 formally documented 22, Marco Kowalski, as listed.": "1976 or 1977",
            "Shakil Bhuiyan, 27, was highlighted in a September 2014 report from Finland.": "1986 or 1987",
            "Ana Ruiz was born in 1971 and listed in Spain in 2010.": "",  # two years and birth word -> see below
        }
        for text, want in list(cases.items())[:3]:
            with self.subTest(text=text):
                self.assertEqual(years(T.parse_snippet_dob(text, "SR-1")), want)

    def test_unreadable_snippet_is_pending(self):
        finding = T.parse_snippet_dob("Maria Lopez, 41, was listed alongside Jose Diaz, 39, in May 2001.", "SR-2")
        self.assertFalse(finding.found)
        self.assertTrue(finding.pending)


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def extract_hit_p1_dob(self, text, source_id, *, reference_date=""):
        self.calls += 1
        return self.result

    def extract_sr_facts(self, text, source_id, *, category_phrase="", hit_countries=()):
        self.calls += 1
        return self.result


class LLMFallback(unittest.TestCase):
    P1 = "In 2019 Elvira Velasco Morillo, who was aged 30 in March 2018, was charged."

    def finding(self):
        return T.parse_p1(self.P1, "H-TEST02", ["Elvira Velasco Morillo"])

    def test_validated_answer_is_used(self):
        llm = FakeLLM(ExtractionResult("hit_p1_dob", "H-TEST02", FactStatus.VERIFIED,
                                       {"age": 30, "reference_date": "March 2018"}, "aged 30 in March 2018"))
        resolved = T.resolve_pending_p1(self.finding(), self.P1, "H-TEST02", llm)
        self.assertEqual(years(resolved), "1987 or 1988")
        self.assertEqual(llm.calls, 1)

    def test_hallucinated_answer_is_rejected(self):
        for payload, quote in (({"age": 31, "reference_date": "March 2018"}, None),      # 31 not in text
                               ({"birth_date": "1950-01-01"}, None),                    # year not in text
                               ({"age": 30, "reference_date": "March 2018"}, "aged 31")):  # quote not in text
            llm = FakeLLM(ExtractionResult("hit_p1_dob", "H-TEST02", FactStatus.VERIFIED, payload, quote))
            with self.subTest(payload=payload, quote=quote):
                self.assertFalse(T.resolve_pending_p1(self.finding(), self.P1, "H-TEST02", llm).found)

    def test_degraded_or_absent_fallback_keeps_pending(self):
        llm = FakeLLM(ExtractionResult("hit_p1_dob", "H-TEST02", FactStatus.PENDING_CLUE, error="429"))
        self.assertTrue(T.resolve_pending_p1(self.finding(), self.P1, "H-TEST02", llm).pending)
        self.assertTrue(T.resolve_pending_p1(self.finding(), self.P1, "H-TEST02", None).pending)

    def test_not_called_when_nothing_is_pending(self):
        llm = FakeLLM(None)
        found = T.parse_p1("Elvira Velasco Morillo, aged 30, in November 1998.", "H", ["Elvira Velasco Morillo"])
        T.resolve_pending_p1(found, "", "H", llm)
        self.assertEqual(llm.calls, 0)


if __name__ == "__main__":
    unittest.main()
