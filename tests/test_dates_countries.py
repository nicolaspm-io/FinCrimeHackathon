"""Unit tests for system/dates.py and system/countries.py (POLICY principles 5-7)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "system"))

import countries as C  # noqa: E402
import dates as D  # noqa: E402
from contracts import ParsedDate  # noqa: E402


def iso(values):
    return [d.iso_str for d in values]


def P(raw, mode=D.BANK):
    return D.parse_date(raw, mode)


class Parsing(unittest.TestCase):
    def test_bank_formats(self):
        cases = {
            "2004-05-19": "2004-05-19", "19/05/2004": "2004-05-19", "05/06/2004": "2004-06-05",
            "7 October 1987": "1987-10-07", "07 October 1987": "1987-10-07",
            "October 7, 1987": "1987-10-07", "October 17, 1987": "1987-10-17",
            "October 1987": "1987-10", "1987": "1987", "1987-10": "1987-10",
            "13th of March, 1990": "1990-03-13", "Sept. 2014": "2014-09", "19.05.2004": "2004-05-19",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(P(raw).iso_str, want)

    def test_bank_slash_is_always_day_first(self):
        self.assertEqual(iso(D.parse_date_alternatives("03/04/1990", D.BANK)), ["1990-04-03"])

    def test_hit_slash_disambiguation(self):
        self.assertEqual(iso(D.parse_date_alternatives("6/20/1955", D.HIT)), ["1955-06-20"])  # H-2C6B2E
        self.assertEqual(iso(D.parse_date_alternatives("23/11/2021", D.HIT)), ["2021-11-23"])
        self.assertEqual(iso(D.parse_date_alternatives("05/06/2004", D.HIT)), ["2004-05-06", "2004-06-05"])
        self.assertEqual(iso(D.parse_date_alternatives("05/05/2004", D.HIT)), ["2004-05-05"])
        self.assertTrue(D.is_ambiguous("05/06/2004"))
        self.assertIsNone(D.parse_date("05/06/2004", D.HIT))

    def test_invalid_or_unsupported(self):
        for raw in ("31/02/2004", "2004-13-01", "12/3/99", "", None, "unknown", "1700", "20 Smarch 1990"):
            with self.subTest(raw=raw):
                self.assertEqual(D.parse_date_alternatives(raw), ())

    def test_leap_day(self):
        self.assertEqual(P("29/02/2000").iso_str, "2000-02-29")
        self.assertIsNone(P("29/02/1900"))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            D.parse_date("2000", "other")


class FindDates(unittest.TestCase):
    def test_kyc_note_shapes(self):
        text = "Sumsub completed identity verification for NILS MARTIN, born December 13, 1977, on 2023-02-19."
        self.assertEqual([(m[2], iso(m[3])) for m in D.find_dates(text)],
                         [("December 13, 1977", ["1977-12-13"]), ("2023-02-19", ["2023-02-19"])])
        text = "On 2024-03-16, Persona validated TEMITOPE NWOSU, DOB 22/09/1956, from Nigeria."
        self.assertEqual([iso(m[3]) for m in D.find_dates(text)], [["2024-03-16"], ["1956-09-22"]])

    def test_longest_shape_wins_and_numbers_are_not_dates(self):
        text = "In October 1987 ref 12345678, case 19999-01, id A1990B; Active Date: 07/26/2018."
        self.assertEqual([(m[2], iso(m[3])) for m in D.find_dates(text, D.HIT)],
                         [("October 1987", ["1987-10"]), ("07/26/2018", ["2018-07-26"])])

    def test_dotted_administrative_date_is_found_as_a_date(self):
        # H-FF3096: 22.11.2011 is a date (callers decide it is not a DOB)
        self.assertEqual([iso(m[3]) for m in D.find_dates("Alta: 22.11.2011", D.HIT)], [["2011-11-22"]])


class Ages(unittest.TestCase):
    def test_policy_example(self):
        self.assertEqual(D.birth_years_from_age(45, 2019), (1973, 1974))  # aged 45 in March 2019
        self.assertEqual(D.birth_years_from_age(45, ParsedDate(2019, 3)), (1973, 1974))

    def test_spec_from_age(self):
        spec = D.spec_from_age(36, ParsedDate(1987, 10), "SR-7B5B39", "sr")
        self.assertEqual(spec.possible_years, (1950, 1951))
        self.assertEqual(D.describe_years(spec), "1950 or 1951")


class DobComparison(unittest.TestCase):
    def test_policy_contradiction_and_agreement(self):
        years = D.spec_from_age(45, 2022, "H", "p1_prose")  # 1976 or 1977
        self.assertEqual(D.compare_dob(P("1979-11-08"), years), D.CONTRADICT)  # H-D861DA
        self.assertEqual(D.compare_dob(P("1978-01-01"), years), D.UNKNOWN)  # adjacent year
        self.assertEqual(D.compare_dob(P("1977-05-01"), years), D.AGREE)  # year alone agrees with a full date
        self.assertEqual(D.compare_dob(P("1968-03-05"), D.spec_from_age(55, 2023, "SR", "sr")), D.AGREE)

    def test_same_year_month_or_day_differs(self):
        spec = D.spec_from_raw("1955-06-20", "H", "hit_dobs")
        self.assertEqual(D.compare_dob(P("1955-11-19"), spec), D.CONTRADICT)  # month differs
        self.assertEqual(D.compare_dob(P("1955-06-21"), spec), D.CONTRADICT)  # day differs
        self.assertEqual(D.compare_dob(P("1955-06-20"), spec), D.AGREE)
        self.assertEqual(D.compare_dob(P("1955"), spec), D.AGREE)  # missing parts never contradict
        self.assertEqual(D.compare_dob(P("1956-11-19"), spec), D.UNKNOWN)  # +-1 year: neither

    def test_every_alternative_must_contradict(self):
        specs = [D.spec_from_raw("1960", "H", "hit_dobs"), D.spec_from_raw("1975", "H", "hit_dobs")]
        self.assertEqual(D.compare_dob(P("1975-02-02"), specs), D.AGREE)
        self.assertEqual(D.compare_dob(P("1990-02-02"), specs), D.CONTRADICT)
        self.assertEqual(D.compare_dob(P("1974-02-02"), specs), D.UNKNOWN)

    def test_ambiguous_slash_keeps_both_readings(self):
        spec = D.spec_from_raw("05/06/1980", "H", "p1_kv", D.HIT)
        self.assertTrue(spec.is_ambiguous_slash)
        self.assertEqual(D.compare_dob(P("1980-06-05"), spec), D.AGREE)
        self.assertEqual(D.compare_dob(P("1980-05-06"), spec), D.AGREE)
        self.assertEqual(D.compare_dob(P("1980-07-01"), spec), D.CONTRADICT)

    def test_nothing_to_compare(self):
        self.assertEqual(D.compare_dob(None, D.spec_from_raw("1970", "H", "hit_dobs")), D.UNKNOWN)
        self.assertEqual(D.compare_dob(P("1970"), []), D.UNKNOWN)

    def test_formats_do_not_matter(self):
        # family Costa: the same date written two ways must compare equal
        self.assertEqual(D.compare_dates(P("14/03/1971"), P("March 14, 1971")), D.AGREE)


class TemporalChecks(unittest.TestCase):
    def test_younger_than_16_at_report(self):
        report = ParsedDate(2025, 9, 10)
        self.assertTrue(D.certainly_younger_than(P("2010-01-01"), report))
        self.assertFalse(D.certainly_younger_than(P("2009-09-10"), report))  # turns 16 that day
        self.assertTrue(D.certainly_younger_than(P("2009-09-11"), report))

    def test_partial_dates_use_worst_case(self):
        # born some time in 2009, report some time in 2025: could already be 16 -> not certain
        self.assertFalse(D.certainly_younger_than(P("2009"), ParsedDate(2025)))
        self.assertTrue(D.certainly_younger_than(P("2010"), ParsedDate(2025)))
        self.assertFalse(D.certainly_younger_than(None, ParsedDate(2025)))
        self.assertTrue(D.any_certainly_younger_than([P("2010"), P("2011-02")], ParsedDate(2025)))
        self.assertFalse(D.any_certainly_younger_than([P("2010"), P("1990")], ParsedDate(2025)))

    def test_incorporated_after_report(self):
        self.assertTrue(D.certainly_after(P("2020-03-01"), ParsedDate(2019, 12, 31)))
        self.assertFalse(D.certainly_after(P("2019"), ParsedDate(2019, 6, 1)))

    def test_deceased_before_screening(self):
        screened = P("2026-11-06")
        self.assertTrue(D.certainly_before(D.parse_date("2022-03", D.HIT), screened))  # H-BD709E
        self.assertFalse(D.certainly_before(P("2026-11"), screened))
        self.assertFalse(D.certainly_before(None, screened))  # deceased_date null never clears


class Countries(unittest.TestCase):
    def test_codes_names_demonyms(self):
        for raw, want in {"GB": "United Kingdom", "UK": "United Kingdom", "GBR": "United Kingdom",
                          "British": "United Kingdom", "Dutch": "Netherlands", "the Netherlands": "Netherlands",
                          "NL": "Netherlands", "American": "United States", "USA": "United States",
                          "Cote D'Ivoire": "Côte d'Ivoire", "CI": "Côte d'Ivoire", "Ivorian": "Côte d'Ivoire",
                          "Türkiye": "Turkey", "Palestina": "Palestine", "Czechia": "Czech Republic",
                          "nigerian": "Nigeria", "Mexican national": "Mexico"}.items():
            with self.subTest(raw=raw):
                self.assertEqual(C.normalize_country(raw), want)

    def test_territories_are_their_own_country(self):
        self.assertEqual(C.normalize_country("Puerto Rico"), "Puerto Rico")
        self.assertEqual(C.normalize_country("Macao Special Administrative Region of China"), "Macao")
        self.assertEqual(C.normalize_country("Northern Mariana Islands [PEP]"), "Northern Mariana Islands")
        for a, b in (("Puerto Rico", "United States"), ("Jersey", "United Kingdom"),
                     ("British Virgin Islands", "United Kingdom"), ("Macao", "China"), ("Hong Kong", "China")):
            with self.subTest(pair=(a, b)):
                self.assertFalse(C.same_country(a, b))

    def test_regions_and_ambiguous_values_are_unknown(self):
        for raw in ("International", "Europe", "International [Associated]", "Congolese", "Korean",
                    "Congo", "NA", "N/A", "in", "", None, "Atlantis"):
            with self.subTest(raw=raw):
                self.assertIsNone(C.normalize_country(raw))

    def test_upper_case_only_for_two_letter_codes(self):
        self.assertEqual(C.normalize_country("IN"), "India")
        self.assertIsNone(C.normalize_country("in"))

    def test_locations(self):
        for raw, want in {"Belgrade, Serbia": "Serbia",
                          "Fayette County, Atlanta, Georgia, United States": "United States",
                          "Prague 4, Podolí, Prague, Czech Republic": "Czech Republic",
                          "Saipan, Northern Mariana Islands": "Northern Mariana Islands",
                          "66 Wellington St W, Toronto, Ontario, M5K 1A2, Canada [Business]": "Canada",
                          "Paris, Europe": None}.items():
            with self.subTest(raw=raw):
                self.assertEqual(C.country_from_location(raw), want)

    def test_place_of_birth_segments(self):
        self.assertEqual(C.place_of_birth_countries(
            "High Wycombe, BuckinghamshBZIRe, United Kingdom; Buckinghamshire, United Kingdom; United Kingdom"),
            ("United Kingdom",))
        self.assertEqual(C.place_of_birth_countries("Indiana, United States; Kampala, Uganda"),
                         ("Uganda", "United States"))

    def test_hit_countries_all_sources(self):
        hit = {"countries": ["Spain", "International"], "nationality": ["Belgrade, Serbia"], "citizenship": ["MX"],
               "addresses": ["1 Main St, Lagos, Nigeria [Business]", "Europe [Associated]"],
               "place_of_birth": "Saipan, Northern Mariana Islands"}
        self.assertEqual(C.hit_countries(hit),
                         ("Mexico", "Nigeria", "Northern Mariana Islands", "Serbia", "Spain"))
        self.assertIn(("Serbia", "nationality", "Belgrade, Serbia"), C.hit_country_sources(hit))

    def test_free_text_mentions(self):
        text = "A Nigerian politician who moved from New Mexico to the South African coast and Papua New Guinea"
        self.assertEqual(C.country_mentions(text), ("Nigeria", "Papua New Guinea", "South Africa"))
        self.assertEqual(C.country_mentions("He lives in IN and works in US"), ())  # codes never in text
        self.assertEqual(C.country_mentions("named in adverse media in Latin America and Europe"), ())

    def test_overlap_and_codes(self):
        self.assertEqual(C.countries_overlap(["Spain", "France"], ["France", "Italy"]), ("France",))
        self.assertEqual(C.iso2("Netherlands"), "NL")
        self.assertEqual(C.iso2("Dutch"), "NL")
        self.assertGreaterEqual(len(C.ALL_COUNTRIES), 249)


DEV = os.path.join(ROOT, "dev")


@unittest.skipUnless(os.path.isdir(DEV), "dev/ data not present")
class DevData(unittest.TestCase):
    """Every date and country value in dev/ is read (they are not part of the public submission)."""

    @staticmethod
    def rows(name):
        import json
        with open(os.path.join(DEV, f"{name}.jsonl"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_every_bank_and_hit_date_parses(self):
        failures = []
        for row in self.rows("customers"):
            if row.get("dob") and D.parse_date(row["dob"]) is None:
                failures.append(row["record_id"])
        for row in self.rows("kyc_checks"):
            for key in ("verified_dob", "checked_at"):
                if row.get(key) and D.parse_date(row[key]) is None:
                    failures.append(row["check_id"])
        for row in self.rows("alerts"):
            if row["customer"].get("dob") and D.parse_date(row["customer"]["dob"]) is None:
                failures.append(row["alert_id"])
        for row in self.rows("hits"):
            for value in list(row.get("dobs") or []) + [row.get("deceased_date")]:
                if value and not D.parse_date_alternatives(value, D.HIT):
                    failures.append(row["hit_id"])
        self.assertEqual(failures, [])

    def test_every_country_value_is_read_except_regions(self):
        unknown = set()
        for row in self.rows("customers"):
            for key in ("nationality", "residence_country"):
                if row.get(key) and not C.normalize_country(row[key]):
                    unknown.add(row[key])
        for row in self.rows("kyc_checks"):
            if row.get("document_country") and not C.normalize_country(row["document_country"]):
                unknown.add(row["document_country"])
        for row in self.rows("hits"):
            for key in ("countries", "nationality", "citizenship", "addresses"):
                for value in row.get(key) or []:
                    if not C.country_from_location(value):
                        unknown.add(value)
        self.assertEqual(unknown, {"International", "Europe", "International [Associated]", "Europe [Associated]"})


if __name__ == "__main__":
    unittest.main()
