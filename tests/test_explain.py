"""Unit tests for system/explain.py: reference-style reasons and minimal evidence."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "system"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import explain as E  # noqa: E402
from contracts import ParsedDate  # noqa: E402
from eval_dev import reason_issues, sentences  # noqa: E402
from test_decision import COMP, TILLEY, decide, hit, listed, person, years  # noqa: E402


def reason(h, *people, **kw):
    out = decide(h, *people, **kw)
    return E.to_hit_decision(out, h)


class ReferenceWording(unittest.TestCase):
    def test_dob_mismatch_profile_notes(self):
        r = reason(hit(specs=[years(1976, 1977)]), person(vdob=ParsedDate(1979, 11, 8), **TILLEY))
        self.assertEqual(r.reason, "The customer's date of birth is 1979-11-08 (NOTE-8548C1) but the hit's "
                                   "profile notes put its birth year at 1976 or 1977.")

    def test_dob_mismatch_search_results_and_listed(self):
        r = reason(hit(specs=[years(1972, 1973, "SR-A88751", "sr")], sr=["SR-A88751"]),
                   person(vdob=ParsedDate(1979, 4, 17), **TILLEY))
        self.assertIn("but the hit's search results put its birth year at 1972 or 1973.", r.reason)
        r = reason(hit(specs=[listed("1962-09-10")]), person(vdob=ParsedDate(1965, 3, 28), **TILLEY))
        self.assertIn("but the hit's listed date of birth is 1962-09-10.", r.reason)

    def test_location_and_name_sentences(self):
        r = reason(hit(names=("John Whitfield",), countries=("United Kingdom",)),
                   person(vname="Jonathan Reyes Whitfield", vsource="KYC-F112AA", record_ids=("CUS-921880", "CUS-277574"),
                          countries=(("Peru", "CUS-921880"), ("United States", "CUS-277574"))))
        self.assertEqual(r.reason, "The customer is linked only to Peru, United States but the hit is linked only to "
                                   "United Kingdom. The customer's verified name is Jonathan Reyes Whitfield "
                                   "(KYC-F112AA), which does not match the hit's name John Whitfield.")
        self.assertEqual(r.evidence, ("CUS-277574", "CUS-921880", "KYC-F112AA"))

    def test_temporal_sentence(self):
        r = reason(hit(countries=("Pakistan",), report=ParsedDate(2020, 1, 8)),
                   person(vdob=ParsedDate(2007, 8, 21), vsource="KYC-358C76", countries=(("Pakistan", "CUS-000001"),)))
        self.assertEqual(r.reason, "The customer was born in 2007 (KYC-358C76), so they were at most 13 when the "
                                   "hit's profile reports activity on 2020-01-08.")

    def test_confirmation_sentences(self):
        r = reason(hit(names=("Elvira Velasco Morillo",), specs=[years(1967, 1968)], countries=("Spain",)),
                   person(vname="ELVIRA VELASCO MORILLO", vdob=ParsedDate(1968, 3, 5), vsource="NOTE-14E25A",
                          countries=(("Spain", "CUS-377745"),), record_ids=("CUS-377745",)))
        self.assertEqual(r.reason, "The customer's date of birth 1968-03-05 (NOTE-14E25A) agrees with the hit's "
                                   "1967 or 1968. Both the customer and the hit are linked to Spain. The customer's "
                                   "name agrees with the hit's name Elvira Velasco Morillo.")
        listed_years = reason(hit(specs=[listed("1994"), listed("1995")]), person(vdob=ParsedDate(1994, 11, 1), **TILLEY))
        self.assertIn("agrees with the hit's 1994, 1995.", listed_years.reason)

    def test_company_confirmation(self):
        r = reason(hit(entity=COMP, names=("N26 Bank SE",), countries=("Germany",)),
                   person(entity=COMP, vname="N26 Bank AG", vsource="KYC-247853", record_ids=("CUS-245548",),
                          countries=(("Germany", "CUS-245548"),)))
        self.assertEqual(r.reason, "Both the customer and the hit are linked to Germany. The customer's name agrees "
                                   "with the hit's name N26 Bank SE.")

    def test_review_sentence(self):
        r = reason(hit(names=("Abdul Rahman Yasin",), countries=("United States",)),
                   person(names=("Abdul Rahman Yasin",), countries=(("United States", "NOTE-10AEAB"),)))
        self.assertEqual(r.reason, "Both the customer and the hit are linked to United States. The customer's name "
                                   "agrees with the hit's name Abdul Rahman Yasin. Nothing in the customer's records "
                                   "contradicts the hit, and the date of birth cannot be compared with it, so the "
                                   "hit can be neither cleared nor confirmed.")


class EveryPathIsWellFormed(unittest.TestCase):
    """Every decision path yields 1-4 sentences, no template leaks, and IDs that are cited."""

    def cases(self):
        T = TILLEY
        yield reason(hit(entity="Individual"), person(entity=COMP, names=("Tilley Ltd",)))
        yield reason(hit(deceased=ParsedDate(2022, 3)), person(**T))
        yield reason(hit(report=ParsedDate(2020, 1, 8)), person(vdob=ParsedDate(2010, 1, 1), **T))
        yield reason(hit(entity=COMP, names=("Acme SA",), report=ParsedDate(2019, 1, 1)),
                     person(entity=COMP, vname="Acme SA", vdob=ParsedDate(2021, 1, 1), countries=(("United States", "CUS-000001"),)))
        yield reason(hit(specs=[years(1976, 1977)], countries=("United Kingdom",)), person(vdob=ParsedDate(1977, 1, 1), **T))
        yield reason(hit(specs=[listed("6/20/1955", "p1_kv"), years(1955, 1956, "SR-1054CF", "sr")], sr=["SR-1054CF"],
                         conflict=True), person(vdob=ParsedDate(1955, 11, 19), **T))
        yield reason(hit(specs=[years(1976, 1977)], countries=()), person(vdob=ParsedDate(1977, 1, 1), **T))
        yield reason(hit(specs=[years(1960, 1961)]))
        yield reason(hit(deceased=ParsedDate(2021, 1, 1)))
        yield reason(hit(specs=[years(1976, 1977)]),
                     person(record_ids=("CUS-000001",), vname="Michael Earl Tilley", vdob=ParsedDate(1990, 1, 1),
                            countries=(("United States", "CUS-000001"),)),
                     person(record_ids=("CUS-000002",), names=("Michael Earl Tilley",),
                            countries=(("United States", "CUS-000002"),)))
        yield reason(hit(names=("Ali Khan",)), person(names=("NADEEM UNKNOWN",), countries=(("United States", "CUS-000001"),)))

    def test_well_formed(self):
        for r in self.cases():
            with self.subTest(code=r.reason_code, reason=r.reason):
                self.assertTrue(1 <= len(sentences(r.reason)) <= 4)
                self.assertEqual(reason_issues(r.to_dict()), [])
                self.assertEqual(list(r.evidence), sorted(set(r.evidence)))
                self.assertEqual(set(r.to_dict()), {"hit_id", "alert_id", "decision", "reason_code",
                                                    "customer_record_id", "reason", "evidence"})


if __name__ == "__main__":
    unittest.main()
