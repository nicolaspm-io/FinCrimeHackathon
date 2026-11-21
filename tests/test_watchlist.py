"""Unit tests for system/watchlist.py (hit resolution and the POLICY 5 cascade)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "system"))

import dates as D  # noqa: E402
import watchlist as W  # noqa: E402


def make_hit(**fields):
    hit = {"hit_id": "H-AAAAAA", "alert_id": "A-AAAAAA", "entity_type": "Individual", "name": "Grace Wright",
           "aliases": [], "dobs": [], "countries": ["Nigeria"], "nationality": [], "citizenship": [],
           "addresses": [], "place_of_birth": None, "categories": ["PEP"], "primary_category": None,
           "profile_notes": None, "deceased_date": None, "life_status": None,
           "first_name": None, "middle_name": None, "last_name": None, "best_matching_name": None}
    hit.update(fields)
    return hit


SR_OK = {"result_id": "SR-000001", "query": "grace wright", "title": "",
         "snippet": "In May 2019, Grace Wright, 43, a politically exposed person in Nigeria, appeared."}
SR_OTHER = {"result_id": "SR-000002", "query": "grace wright", "title": "",
            "snippet": "In May 2019, Grace Wright, 25, a politically exposed person in Kenya, appeared."}


class Cascade(unittest.TestCase):
    def test_dobs_field_wins(self):
        hit = make_hit(dobs=["1994", "1995"], profile_notes="Grace Wright, aged 30, in May 2000.")
        resolved, detail = W.resolve_hit(hit, search_results=[SR_OK])
        self.assertEqual(D.describe_years(resolved.dob_specs), "1994 or 1995")
        self.assertEqual(detail.dob_origin, "hit_dobs")
        self.assertEqual(resolved.sr_used_ids, ())

    def test_paragraph_one_before_search_results(self):
        hit = make_hit(profile_notes="Grace Wright, aged 30, was charged in May 2000.\n\nGrace Wright was 50 in 2020.")
        resolved, detail = W.resolve_hit(hit, search_results=[SR_OK])
        self.assertEqual(D.describe_years(resolved.dob_specs), "1969 or 1970")
        self.assertEqual(detail.dob_origin, "p1_prose")
        self.assertEqual(resolved.sr_used_ids, ())  # search results are only used when P1 gives nothing

    def test_search_results_last_and_attributed_only(self):
        resolved, detail = W.resolve_hit(make_hit(), search_results=[SR_OTHER, SR_OK])
        self.assertEqual(D.describe_years(resolved.dob_specs), "1975 or 1976")
        self.assertEqual(resolved.sr_used_ids, ("SR-000001",))
        self.assertEqual(detail.sr_attributed, ("SR-000001",))
        self.assertEqual(detail.sr_considered, ("SR-000001", "SR-000002"))

    def test_company_uses_only_dobs(self):
        company = make_hit(entity_type="Company", name="Acme Ltd", profile_notes="Acme Ltd was founded in 1990.")
        resolved, _ = W.resolve_hit(company, search_results=[SR_OK])
        self.assertEqual(resolved.dob_specs, ())
        company["dobs"] = ["2001-05-01"]
        resolved, _ = W.resolve_hit(company)
        self.assertEqual(D.describe_years(resolved.dob_specs), "2001-05-01")

    def test_decision_4_key_value_and_search_result(self):
        sr = {"result_id": "SR-1054CF", "query": "x", "title": "",
              "snippet": "In June 2011, Grace Wright, 55, a politically exposed person in Nigeria, was listed."}
        hit = make_hit(profile_notes="Name: Grace Wright\nSex: F\nDOB: 6/20/1955\nHeight: 5' 10\"")
        resolved, detail = W.resolve_hit(hit, search_results=[sr])
        self.assertTrue(resolved.p1_kv_sr_conflict)
        self.assertEqual(detail.dob_origin, "p1_kv+sr")
        self.assertEqual({s.origin for s in resolved.dob_specs}, {"p1_kv", "sr"})
        self.assertEqual(resolved.sr_used_ids, ("SR-1054CF",))

    def test_key_value_alone_is_not_a_conflict(self):
        hit = make_hit(profile_notes="Name: Grace Wright\nDOB: 6/20/1955")
        resolved, _ = W.resolve_hit(hit, search_results=[])
        self.assertFalse(resolved.p1_kv_sr_conflict)


class OtherFacts(unittest.TestCase):
    def test_names_aliases_countries(self):
        hit = make_hit(name="Wright, Grace", first_name="Grace", last_name="Wright", best_matching_name="Wright, Grace",
                       aliases=["Grace A. Wright (AKA)", "Grace A. Wright (AKA)"], nationality=["Lagos, Nigeria"],
                       addresses=["1 Road, Accra, Ghana [Business]", "International [Associated]"],
                       place_of_birth="Saipan, Northern Mariana Islands")
        resolved, detail = W.resolve_hit(hit)
        self.assertEqual(resolved.names, ("Wright, Grace", "Grace Wright"))
        self.assertEqual(resolved.aliases, ("Grace A. Wright (AKA)",))
        self.assertEqual(resolved.countries, ("Ghana", "Nigeria", "Northern Mariana Islands"))
        self.assertIn(("Ghana", "addresses", "1 Road, Accra, Ghana [Business]"), detail.country_sources)

    def test_deceased_only_from_a_date(self):
        resolved, _ = W.resolve_hit(make_hit(life_status="Deceased", deceased_date=None))
        self.assertIsNone(resolved.deceased_date)
        resolved, _ = W.resolve_hit(make_hit(life_status="Deceased", deceased_date="2022-03"))
        self.assertEqual(resolved.deceased_date.iso_str, "2022-03")
        resolved, _ = W.resolve_hit(make_hit(deceased_date="05/06/2021"))  # ambiguous: keep the later reading
        self.assertEqual(resolved.deceased_date.iso_str, "2021-06-05")

    def test_earliest_report(self):
        notes = "According to A; September 10, 2025: x.\nAccording to B; January 8, 2020: y."
        resolved, detail = W.resolve_hit(make_hit(profile_notes=notes))
        self.assertEqual(resolved.earliest_report_date.iso_str, "2020-01-08")
        self.assertEqual(len(detail.dated_reports), 2)


DEV = os.path.join(ROOT, "dev")


@unittest.skipUnless(os.path.isdir(DEV), "dev/ data not present")
class DevHits(unittest.TestCase):
    """The hit-side facts behind the practice labels' reference reasons."""

    @classmethod
    def setUpClass(cls):
        import data
        cls.ds = data.load_dataset(DEV)
        cls.resolved = {h["hit_id"]: W.resolve_hit(h, cls.ds) for h in cls.ds.hits}

    def check(self, hit_id, years, origin, sr=()):
        resolved, detail = self.resolved[hit_id]
        self.assertEqual(D.describe_years(resolved.dob_specs), years, hit_id)
        self.assertEqual(detail.dob_origin, origin, hit_id)
        self.assertEqual(resolved.sr_used_ids, tuple(sr), hit_id)

    def test_label_reference_facts(self):
        self.check("H-D861DA", "1976 or 1977", "p1_prose")
        self.check("H-4AA437", "1986 or 1987", "p1_prose")
        self.check("H-582410", "1967 or 1968", "p1_prose")
        self.check("H-622EDD", "1972 or 1973", "sr", ["SR-A88751"])
        self.check("H-0181BD", "1985 or 1986", "sr", ["SR-590704"])
        self.check("H-80B4B5", "1976 or 1977", "sr", ["SR-01BFC3"])
        self.check("H-F9DBF9", "1959 or 1960", "sr", ["SR-AE2CE1"])
        self.check("H-FC22E2", "1972 or 1973", "sr", ["SR-65C752"])
        self.check("H-58A39C", "1962-09-10", "hit_dobs")
        self.check("H-4D76E3", "1968-02", "hit_dobs")

    def test_h_2c6b2e_and_h_fe02a6_carry_the_conflict_flag(self):
        flagged = sorted(h for h, (r, _) in self.resolved.items() if r.p1_kv_sr_conflict)
        self.assertEqual(flagged, ["H-2C6B2E", "H-FE02A6"])
        self.assertEqual(self.resolved["H-2C6B2E"][0].sr_used_ids, ("SR-1054CF",))

    def test_temporal_and_deceased_facts(self):
        self.assertEqual(self.resolved["H-13E1CF"][0].earliest_report_date.iso_str, "2020-01-08")
        self.assertEqual(self.resolved["H-BD709E"][0].deceased_date.iso_str, "2022-03")
        for hit_id in ("H-A0E7C9", "H-DCDB32"):
            self.assertIsNone(self.resolved[hit_id][0].deceased_date)

    def test_h_ff3096_has_no_birth_date(self):
        self.assertEqual(self.resolved["H-FF3096"][0].dob_specs, ())

    def test_every_hit_resolves_and_is_stable(self):
        import watchlist
        self.assertEqual(len(self.resolved), 819)
        again = {h["hit_id"]: watchlist.resolve_hit(h, self.ds) for h in self.ds.hits}
        self.assertEqual(again, self.resolved)


if __name__ == "__main__":
    unittest.main()
