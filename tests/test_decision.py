"""Unit tests for system/decision.py: all 11 reason codes and the guards against unsafe clearances."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "system"))

import dates as Dt  # noqa: E402
import decision as D  # noqa: E402
import names as N  # noqa: E402
from contracts import (CandidatePerson, DateSpec, Fact, FactStatus, ParsedDate, ResolvedHit,  # noqa: E402
                       VerifiedIdentity)

VARIANTS = N.build_variants({"groups": [["muhammad", "mohammed", "mohammad"]],
                             "company_legal_forms": ["sa", "se", "ag", "ltd", "llc"]})
SCREENED = "2026-11-06"
IND, COMP = "Individual", "Company"


def fact(field, value, source, status=FactStatus.VERIFIED):
    return Fact(field, value, str(value), status, "customer", source.split("-")[0], source, "")


def pending(field, source="NOTE-PENDIN"):
    return fact(field, None, source, FactStatus.PENDING_CLUE)


def person(record_ids=("CUS-000001",), entity=IND, vname=None, vdob=None, vcountry=None, vsource="KYC-000001",
           names=(), dobs=(), countries=(), clues=(), conflicts=()):
    verified = None
    if vname or vdob or vcountry or conflicts:
        verified = VerifiedIdentity(
            fact("name", vname, vsource) if vname else None,
            fact("dob", vdob, vsource) if vdob else None,
            fact("doc_country", vcountry, vsource) if vcountry else None,
            None, "2024-01-01", vsource, tuple(conflicts))
    rid = record_ids[0]
    return CandidatePerson(
        tuple(record_ids), entity, verified,
        tuple(fact("name", n, rid) for n in names),
        tuple(fact("dob", d, rid) for d in dobs),
        tuple(fact("country", c, s) for c, s in countries),
        tuple(clues))


def years(y1, y2, source="H-HIT001", origin="p1_prose"):
    return DateSpec((y1, y2), (), source, origin)


def listed(raw, origin="hit_dobs", source="H-HIT001"):
    return Dt.spec_from_raw(raw, source, origin, Dt.HIT)


def hit(entity=IND, names=("Michael Earl Tilley",), aliases=(), specs=(), countries=("United States",), report=None,
        deceased=None, sr=(), conflict=False, clues=()):
    return ResolvedHit("H-HIT001", "A-ALERT1", entity, tuple(names), tuple(aliases), tuple(specs), tuple(countries),
                       report, deceased, tuple(sr), conflict, tuple(clues))


def decide(h, *people, screened_type=None):
    return D.decide(h, people, SCREENED, VARIANTS, screened_type)


TILLEY = dict(vname="Michael Earl Tilley", vsource="NOTE-8548C1", countries=(("United States", "CUS-000001"),))


class ClearanceCodes(unittest.TestCase):
    def test_entity_type_mismatch(self):
        out = decide(hit(entity=IND), person(entity=COMP, names=("Tilley Holdings Ltd",)))
        self.assertEqual((out.decision, out.reason_code), ("DIFFERENT_ENTITY", "ENTITY_TYPE_MISMATCH"))

    def test_deceased_before_screening(self):
        out = decide(hit(deceased=ParsedDate(2022, 3)), person(**TILLEY))
        self.assertEqual(out.reason_code, "DECEASED")
        alive = decide(hit(deceased=ParsedDate(2026, 11)), person(**TILLEY))  # could be after 6 November
        self.assertNotEqual(alive.reason_code, "DECEASED")

    def test_temporal_impossibility_individual_and_company(self):
        out = decide(hit(countries=("Pakistan",), report=ParsedDate(2020, 1, 8)),
                     person(vdob=ParsedDate(2007, 8, 21), vsource="KYC-358C76",
                            countries=(("Singapore", "CUS-000001"),)))
        self.assertEqual(out.reason_code, "TEMPORAL_IMPOSSIBILITY")
        self.assertEqual([f.code for f in out.persons[0].findings], ["TEMPORAL_IMPOSSIBILITY", "LOCATION_MISMATCH"])
        self.assertEqual(out.evidence, ("CUS-000001", "KYC-358C76"))
        company = decide(hit(entity=COMP, names=("Acme SA",), report=ParsedDate(2019, 12, 31)),
                         person(entity=COMP, vname="Acme SA", vdob=ParsedDate(2021, 3, 1),
                                countries=(("United States", "CUS-000001"),)))
        self.assertEqual(company.reason_code, "TEMPORAL_IMPOSSIBILITY")

    def test_sixteenth_birthday_is_not_impossible(self):
        out = decide(hit(report=ParsedDate(2020, 1, 8)), person(vdob=ParsedDate(2004, 1, 8), **TILLEY))
        self.assertNotEqual(out.reason_code, "TEMPORAL_IMPOSSIBILITY")

    def test_dob_mismatch_cites_dob_source_and_search_result(self):
        out = decide(hit(specs=[years(1976, 1977)]), person(vdob=ParsedDate(1979, 11, 8), **TILLEY))
        self.assertEqual((out.decision, out.reason_code, out.evidence), ("DIFFERENT_ENTITY", "DOB_MISMATCH", ("NOTE-8548C1",)))
        sr = decide(hit(specs=[years(1972, 1973, "SR-A88751", "sr")], sr=["SR-A88751"]),
                    person(vdob=ParsedDate(1979, 4, 17), vsource="NOTE-CA9BBC", vname="Michael Earl Tilley",
                           countries=(("United States", "CUS-000001"),)))
        self.assertEqual(sr.evidence, ("NOTE-CA9BBC", "SR-A88751"))

    def test_location_mismatch(self):
        out = decide(hit(countries=("United States",)),
                     person(vname="Michael Earl Tilley", vcountry="Netherlands", vsource="KYC-A62617",
                            countries=(("Netherlands", "CUS-000001"),)))
        self.assertEqual((out.reason_code, out.evidence), ("LOCATION_MISMATCH", ("CUS-000001", "KYC-A62617")))

    def test_name_mismatch_verified(self):
        out = decide(hit(names=("Ridwan, Azeez",), countries=("Nigeria",)),
                     person(vname="CHINEDU AZEEZ", vsource="KYC-20EE17", names=("Ridwan Azeez",),
                            countries=(("Nigeria", "CUS-000001"),)))
        self.assertEqual((out.reason_code, out.evidence), ("NAME_MISMATCH_VERIFIED", ("KYC-20EE17",)))

    def test_declared_name_alone_never_clears_on_name(self):
        out = decide(hit(names=("Ridwan, Azeez",), countries=("Nigeria",)),
                     person(names=("CHINEDU AZEEZ",), countries=(("Nigeria", "CUS-000001"),)))
        self.assertEqual(out.decision, "REVIEW")

    def test_every_applicable_contradiction_is_collected(self):
        out = decide(hit(names=("Gallardo, Bartolo Diego",), specs=[years(1972, 1973, "SR-65C752", "sr")],
                         countries=("Peru",), sr=["SR-65C752"]),
                     person(vname="Joseph gallardo", vdob=ParsedDate(2002, 5, 13), vsource="KYC-04520F",
                            countries=(("Peru", "CUS-000001"),)))
        self.assertEqual([f.code for f in out.persons[0].findings], ["DOB_MISMATCH", "NAME_MISMATCH_VERIFIED"])
        self.assertEqual(out.evidence, ("KYC-04520F", "SR-65C752"))


class ConfirmationCodes(unittest.TestCase):
    def test_name_dob_location_match(self):
        out = decide(hit(names=("Elvira Velasco Morillo",), specs=[years(1967, 1968)], countries=("Spain",)),
                     person(vname="ELVIRA VELASCO MORILLO", vdob=ParsedDate(1968, 3, 5), vsource="NOTE-14E25A",
                            countries=(("Spain", "CUS-377745"),), record_ids=("CUS-377745",)))
        self.assertEqual((out.decision, out.reason_code), ("SAME_ENTITY", "NAME_DOB_LOCATION_MATCH"))
        self.assertEqual(out.customer_record_id, "CUS-377745")
        self.assertEqual(out.evidence, ("CUS-377745", "NOTE-14E25A"))

    def test_name_dob_match_when_countries_unknown(self):
        out = decide(hit(specs=[years(1976, 1977)], countries=()), person(vdob=ParsedDate(1977, 1, 11), **TILLEY))
        self.assertEqual(out.reason_code, "NAME_DOB_MATCH")

    def test_name_location_match_company(self):
        out = decide(hit(entity=COMP, names=("N26 Bank SE",), countries=("Germany",)),
                     person(entity=COMP, vname="N26 Bank AG", vsource="KYC-247853", record_ids=("CUS-245548",),
                            countries=(("Germany", "CUS-245548"),)))
        self.assertEqual((out.reason_code, out.evidence), ("NAME_LOCATION_MATCH", ("CUS-245548", "KYC-247853")))

    def test_birth_year_alone_agrees_with_full_date(self):
        out = decide(hit(specs=[listed("1994"), listed("1995")]), person(vdob=ParsedDate(1994, 11, 1), **TILLEY))
        self.assertEqual(out.reason_code, "NAME_DOB_LOCATION_MATCH")


class ReviewCodes(unittest.TestCase):
    def test_insufficient_data_cites_shared_country_sources(self):
        out = decide(hit(names=("Abdul Rahman Yasin",), countries=("United States",)),
                     person(names=("Abdul Rahman Yasin",), countries=(("United States", "NOTE-10AEAB"),
                                                                      ("Canada", "CUS-459031")),
                            record_ids=("CUS-459031",)))
        self.assertEqual((out.decision, out.reason_code), ("REVIEW", "INSUFFICIENT_DATA"))
        self.assertEqual(out.evidence, ("NOTE-10AEAB",))

    def test_adjacent_year_is_review(self):
        out = decide(hit(specs=[years(1976, 1977)]), person(vdob=ParsedDate(1978, 6, 1), **TILLEY))
        self.assertEqual(out.decision, "REVIEW")

    def test_ambiguous_customer_record(self):
        clear = person(record_ids=("CUS-000001",), vname="Michael Earl Tilley", vdob=ParsedDate(1990, 1, 1),
                       countries=(("United States", "CUS-000001"),))
        review = person(record_ids=("CUS-000002",), names=("Michael Earl Tilley",),
                        countries=(("United States", "CUS-000002"),))
        out = decide(hit(specs=[years(1976, 1977)]), clear, review)
        self.assertEqual((out.decision, out.reason_code, out.customer_record_id),
                         ("REVIEW", "AMBIGUOUS_CUSTOMER_RECORD", "AMBIGUOUS"))
        self.assertIn("CUS-000001", out.evidence)
        self.assertIn("CUS-000002", out.evidence)

    def test_unanimous_candidates_keep_the_decision(self):
        a = person(record_ids=("CUS-618916",), names=("Mohammad Nadeem",), countries=(("Pakistan", "KYC-8DE62A"),))
        b = person(record_ids=("CUS-885413",), names=("Mohammed Nadeem",), countries=(("Pakistan", "NOTE-BC6437"),))
        out = decide(hit(names=("Mohammad Nadeem",), countries=("Pakistan",)), a, b)
        self.assertEqual((out.decision, out.reason_code, out.customer_record_id), ("REVIEW", "INSUFFICIENT_DATA", "AMBIGUOUS"))
        self.assertEqual(out.evidence, ("KYC-8DE62A", "NOTE-BC6437"))


class Guards(unittest.TestCase):
    def test_name_and_dob_agree_but_country_differs_is_review(self):
        out = decide(hit(specs=[years(1976, 1977)], countries=("United Kingdom",)),
                     person(vdob=ParsedDate(1977, 1, 11), **TILLEY))
        self.assertEqual((out.decision, out.reason_code), ("REVIEW", "INSUFFICIENT_DATA"))
        self.assertEqual(out.why, D.WHY_NAME_DOB_AGREE_COUNTRY_DIFFERS)

    def test_pending_residence_clue_blocks_location_mismatch(self):
        p = person(vname="Michael Earl Tilley", countries=(("United Kingdom", "CUS-000001"),),
                   clues=[pending("residence", "NOTE-F302A4")])
        self.assertEqual(decide(hit(countries=("Nigeria",)), p).decision, "REVIEW")

    def test_pending_hit_dob_clue_blocks_dob_mismatch_and_confirmation(self):
        clue = Fact("dob", None, "aged 30 in ...", FactStatus.PENDING_CLUE, "hit", "HIT", "H-HIT001", "profile_notes#p1")
        h = hit(specs=[years(1960, 1961, "SR-000001", "sr")], clues=[clue])
        self.assertEqual(decide(h, person(vdob=ParsedDate(1990, 1, 1), **TILLEY)).decision, "REVIEW")
        self.assertEqual(decide(h, person(vdob=ParsedDate(1960, 5, 1), **TILLEY)).decision, "REVIEW")

    def test_pending_kyc_clue_blocks_name_dob_and_location(self):
        p = person(vname="CHINEDU AZEEZ", vdob=ParsedDate(1990, 1, 1), countries=(("Kenya", "CUS-000001"),),
                   clues=[pending("kyc")])
        out = decide(hit(names=("Ridwan, Azeez",), specs=[years(1960, 1961)], countries=("Nigeria",)), p)
        self.assertEqual(out.decision, "REVIEW")

    def test_pending_kyc_note_clue_from_customer_notes_blocks_clearance(self):
        # customer_notes.py emits field="kyc_note" for unparsed KYC notes; it must guard like "kyc".
        p = person(vname="CHINEDU AZEEZ", vdob=ParsedDate(1990, 1, 1), countries=(("Kenya", "CUS-000001"),),
                   clues=[pending("kyc_note")])
        out = decide(hit(names=("Ridwan, Azeez",), specs=[years(1960, 1961)], countries=("Nigeria",)), p)
        self.assertEqual(out.decision, "REVIEW")

    def test_decision_4_p1_key_value_vs_search_result(self):
        specs = [listed("6/20/1955", "p1_kv"), years(1955, 1956, "SR-1054CF", "sr")]
        h = hit(specs=specs, sr=["SR-1054CF"], conflict=True)
        split = decide(h, person(vdob=ParsedDate(1955, 11, 19), **TILLEY))  # H-2C6B2E
        self.assertEqual((split.decision, split.reason_code, split.why), ("REVIEW", "INSUFFICIENT_DATA", D.WHY_P1_SR_CONFLICT))
        agree = decide(h, person(vdob=ParsedDate(1955, 6, 20), **TILLEY))  # H-FE02A6
        self.assertEqual(agree.decision, "SAME_ENTITY")
        both = decide(h, person(vdob=ParsedDate(1980, 1, 1), **TILLEY))
        self.assertEqual(both.reason_code, "DOB_MISMATCH")

    def test_one_real_name_part_neither_confirms_nor_clears(self):
        p = person(vname="NADEEM UNKNOWN", vdob=ParsedDate(1977, 1, 11), countries=(("United States", "CUS-000001"),))
        out = decide(hit(names=("Mohammad Nadeem",), specs=[years(1976, 1977)]), p)
        self.assertEqual(out.decision, "REVIEW")
        other = decide(hit(names=("Ali Khan",), specs=[years(1976, 1977)]), p)
        self.assertEqual(other.decision, "REVIEW")  # verified name too short to clear on name

    def test_conflicting_approved_dobs_are_unknown(self):
        p = person(vdob=ParsedDate(1990, 1, 1), conflicts=[fact("dob", "1969-01-01", "KYC-000002")], **TILLEY)
        self.assertEqual(decide(hit(specs=[years(1960, 1961)]), p).decision, "REVIEW")

    def test_declared_dobs_that_disagree_are_unknown(self):
        p = person(names=("Michael Earl Tilley",), dobs=("01/01/1990", "1969-05-05"),
                   countries=(("United States", "CUS-000001"),))
        self.assertEqual(decide(hit(specs=[years(1960, 1961)]), p).decision, "REVIEW")

    def test_declared_dob_is_used_without_a_verified_one(self):
        p = person(names=("Michael Earl Tilley",), dobs=("08/11/1979",), countries=(("United States", "CUS-000001"),))
        out = decide(hit(specs=[years(1976, 1977)]), p)
        self.assertEqual((out.reason_code, out.evidence), ("DOB_MISMATCH", ("CUS-000001",)))


class NoCustomer(unittest.TestCase):
    def test_null_customer_is_review(self):
        out = decide(hit(specs=[years(1960, 1961)]))
        self.assertEqual((out.decision, out.reason_code, out.customer_record_id), ("REVIEW", "INSUFFICIENT_DATA", None))

    def test_customer_independent_contradictions_still_clear(self):
        self.assertEqual(decide(hit(deceased=ParsedDate(2021, 8, 2))).reason_code, "DECEASED")
        self.assertEqual(decide(hit(entity=IND), screened_type=COMP).reason_code, "ENTITY_TYPE_MISMATCH")


class UnsafeClearanceGuards(unittest.TestCase):
    def test_year_and_full_date_that_agree_are_one_date(self):
        agreed = person(names=("Maria Garcia",), dobs=("2004", "19/05/2004"),
                        countries=(("Spain", "CUS-000001"),))
        same = decide(hit(names=("Maria Garcia",), specs=[listed("2004-05-19")], countries=("Spain",)), agreed)
        self.assertEqual(same.decision, "SAME_ENTITY")
        blocked = decide(hit(names=("Maria Garcia",), specs=[listed("2004-05-19")], countries=("France",)), agreed)
        self.assertEqual((blocked.decision, blocked.why), ("REVIEW", D.WHY_NAME_DOB_AGREE_COUNTRY_DIFFERS))

    def test_non_countries_and_raw_country_forms_do_not_clear(self):
        other = person(names=("Other Person",), countries=(("Spain", "CUS-000001"),))
        for raw in ("International", "Europe"):
            out = decide(hit(names=("Someone Else",), countries=(raw,)), other)
            self.assertNotEqual(out.reason_code, "LOCATION_MISMATCH", raw)
        united = person(names=("Other Person",), countries=(("United Kingdom", "CUS-000001"),))
        for raw in ("British", "GB"):
            out = decide(hit(names=("Someone Else",), countries=(raw,)), united)
            self.assertNotEqual(out.reason_code, "LOCATION_MISMATCH", raw)
        spain = decide(hit(names=("Someone Else",), countries=("Spain [Business]",)), other)
        self.assertNotEqual(spain.reason_code, "LOCATION_MISMATCH")
        macao = person(names=("Other Person",), countries=(("Macao", "CUS-000001"),))
        out = decide(hit(names=("Someone Else",), countries=("Macao Special Administrative Region of China",)), macao)
        self.assertNotEqual(out.reason_code, "LOCATION_MISMATCH")

    def test_pending_birth_or_name_blocks_the_location_clearance_it_could_overturn(self):
        named = person(vname="Maria Garcia", countries=(("Spain", "CUS-000001"),), clues=[pending("dob")])
        self.assertEqual(decide(hit(names=("Maria Garcia",), countries=("France",)), named).decision, "REVIEW")
        born = person(names=("Other Person",), vdob=ParsedDate(1980, 5, 1), countries=(("Spain", "CUS-000001"),),
                      clues=[pending("name")])
        self.assertEqual(decide(hit(names=("Maria Garcia",), specs=[listed("1980-05-01")], countries=("France",)), born).decision,
                         "REVIEW")
        clue = Fact("dob", None, "aged 30", FactStatus.PENDING_CLUE, "hit", "HIT", "H-HIT001", "profile_notes#p1")
        self.assertEqual(decide(hit(names=("Maria Garcia",), countries=("France",), clues=[clue]),
                                person(vname="Maria Garcia", countries=(("Spain", "CUS-000001"),))).decision, "REVIEW")

    def test_pending_nationality_and_hit_country_block_location(self):
        customer = person(names=("Other Person",), countries=(("Spain", "CUS-000001"),), clues=[pending("nationality")])
        self.assertEqual(decide(hit(names=("Someone Else",), countries=("France",)), customer).decision, "REVIEW")
        clue = Fact("country", None, "born in ...", FactStatus.PENDING_CLUE, "hit", "HIT", "H-HIT001", "profile_notes#p1")
        out = decide(hit(names=("Someone Else",), countries=("France",), clues=[clue]),
                     person(names=("Other Person",), countries=(("Spain", "CUS-000001"),)))
        self.assertEqual(out.decision, "REVIEW")

    def test_pending_name_does_not_confirm_the_declared_name(self):
        p = person(names=("Maria Garcia",), vdob=ParsedDate(1980, 5, 1), countries=(("Spain", "CUS-000001"),),
                   clues=[pending("name")])
        out = decide(hit(names=("Maria Garcia",), specs=[listed("1980-05-01")], countries=("Spain",)), p)
        self.assertNotEqual(out.decision, "SAME_ENTITY")

    def test_unknown_customer_type_is_not_confirmed_as_a_company(self):
        out = decide(hit(entity=COMP, names=("Acme Holdings",), countries=("Germany",)),
                     person(entity="", vname="Acme Holdings", countries=(("Germany", "CUS-000001"),)))
        self.assertNotEqual(out.decision, "SAME_ENTITY")

    def test_match_evidence_cites_the_name_and_country_sources(self):
        from contracts import CandidatePerson, VerifiedIdentity
        verified = VerifiedIdentity(fact("name", "Maria Garcia", "KYC-NAME"),
                                    fact("dob", ParsedDate(1980, 5, 1), "NOTE-DOB"),
                                    None, None, "2024-01-01", "KYC-NAME")
        p = CandidatePerson(("CUS-000001",), IND, verified, (), (), (fact("country", "Spain", "NOTE-CTY"),), ())
        out = decide(hit(names=("Maria Garcia",), specs=[years(1980, 1981)], countries=("Spain",)), p)
        self.assertEqual(out.decision, "SAME_ENTITY")
        self.assertEqual(out.evidence, ("CUS-000001", "KYC-NAME", "NOTE-CTY", "NOTE-DOB"))


class Determinism(unittest.TestCase):
    def test_same_input_same_outcome(self):
        args = (hit(specs=[years(1976, 1977)], countries=("United Kingdom",)), person(vdob=ParsedDate(1977, 1, 11), **TILLEY))
        first = decide(*args)
        for _ in range(3):
            self.assertEqual(decide(*args), first)

    def test_every_code_is_reachable(self):
        self.assertEqual(set(D.CODE_PRIORITY), set(__import__("contracts").REASON_CODE_TO_DECISION))


if __name__ == "__main__":
    unittest.main()
