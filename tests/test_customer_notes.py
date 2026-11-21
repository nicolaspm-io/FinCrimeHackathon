"""Unit and dev acceptance tests for system/customer_notes.py (Track D)."""
from __future__ import annotations

from collections import Counter
import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_DIR = os.path.join(REPO_ROOT, "system")
DEV_DIR = os.path.join(REPO_ROOT, "dev")

if SYSTEM_DIR not in sys.path:
    sys.path.insert(0, SYSTEM_DIR)

from contracts import ExtractionResult, FactStatus, TASK_KYC_NOTE, TASK_RESIDENCE_NOTE
from customer_notes import (
    detect_kyc_status,
    parse_customer_notes,
    parse_kyc_note,
    parse_residence_note,
)


class _StubLLMFallback:
    """Deterministic stub implementing LLMFallback for note parser tests."""

    def __init__(self, *, kyc_result=None, res_result=None) -> None:
        self.kyc_result = kyc_result
        self.res_result = res_result
        self.kyc_calls = 0
        self.res_calls = 0

    def extract_kyc_note(self, text: str, source_id: str, *, created_at: str = "") -> ExtractionResult:
        self.kyc_calls += 1
        return self.kyc_result or ExtractionResult(
            task=TASK_KYC_NOTE,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            error="stub_pending",
        )

    def extract_residence_countries(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        self.res_calls += 1
        return self.res_result or ExtractionResult(
            task=TASK_RESIDENCE_NOTE,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            error="stub_pending",
        )

    def extract_hit_p1_dob(self, text: str, source_id: str, *, reference_date: str = "") -> ExtractionResult:
        raise NotImplementedError

    def extract_sr_facts(
        self, text: str, source_id: str, *, category_phrase: str = "", hit_countries: tuple[str, ...] = ()
    ) -> ExtractionResult:
        raise NotImplementedError


class KycNoteParserTest(unittest.TestCase):
    """Tests for KYC-channel note extraction and negative status protection."""

    def test_approved_formats_ddmmyyyy_demonyms_and_placeholders(self) -> None:
        n1 = parse_kyc_note(
            {
                "note_id": "N-1",
                "created_at": "2024-07-12",
                "channel": "kyc",
                "text": "Onfido verified the Bangladeshi passport for KARIM KHAN on 2024-07-12; subject date of birth is December 5, 1992.",
            }
        )
        self.assertEqual(n1.status, "approved")
        self.assertEqual(n1.verified_name, "KARIM KHAN")
        self.assertEqual(n1.verified_dob.iso_str, "1992-12-05")
        self.assertEqual(n1.doc_country, "Bangladesh")
        self.assertEqual(n1.checked_at, "2024-07-12")
        self.assertEqual(n1.pending_clues, ())

        # Bank slash dates are strictly DD/MM/YYYY
        n2 = parse_kyc_note(
            {
                "note_id": "N-2",
                "created_at": "2025-04-09",
                "channel": "kyc",
                "text": "EVA GARCIA's identity was confirmed through a national ID from Italy via Sumsub on 2025-04-09. Recorded DOB: 08/09/1951.",
            }
        )
        self.assertEqual(n2.status, "approved")
        self.assertEqual(n2.verified_name, "EVA GARCIA")
        self.assertEqual(n2.verified_dob.iso_str, "1951-09-08")
        self.assertEqual(n2.doc_country, "Italy")

        # Placeholder & Latin Extended-A tokens
        n3 = parse_kyc_note(
            {
                "note_id": "N-3",
                "created_at": "2025-02-20",
                "channel": "kyc",
                "text": "Sumsub verified the identity of PAWEŁ N/A N/A on 2025-02-20, utilizing a driving licence from Spain showing 20 September 1962 as the birth date.",
            }
        )
        self.assertEqual(n3.verified_name, "PAWEŁ N/A N/A")
        self.assertEqual(n3.verified_dob.iso_str, "1962-09-20")
        self.assertEqual(n3.doc_country, "Spain")

        # Mixed-case unseen paraphrase
        n4 = parse_kyc_note(
            {
                "note_id": "N-4",
                "created_at": "2024-03-01",
                "channel": "kyc",
                "text": "Verification passed for Maria Elena Garcia, born 14/03/1988, using a Spanish passport.",
            }
        )
        self.assertEqual(n4.status, "approved")
        self.assertEqual(n4.verified_name, "Maria Elena Garcia")
        self.assertEqual(n4.verified_dob.iso_str, "1988-03-14")
        self.assertEqual(n4.doc_country, "Spain")

    def test_rejected_expired_and_pending_never_treated_as_approved(self) -> None:
        self.assertEqual(
            detect_kyc_status("Identity verification failed for JOHN SMITH, DOB 01/02/1980."),
            "rejected",
        )
        self.assertEqual(
            detect_kyc_status("Verification completed: document expired and no longer valid."),
            "expired",
        )
        self.assertEqual(
            detect_kyc_status("Verification submitted for JANE DOE; currently under review."),
            "unknown",
        )

        # Even if LLM falsely returned approved, local rejected status MUST NOT flip
        stub = _StubLLMFallback(
            kyc_result=ExtractionResult(
                task=TASK_KYC_NOTE,
                source_id="N-REJ",
                status=FactStatus.VERIFIED,
                payload={"status": "approved", "verified_name": "JOHN SMITH"},
            )
        )
        parsed = parse_kyc_note(
            {
                "note_id": "N-REJ",
                "created_at": "2024-01-10",
                "channel": "kyc",
                "text": "Verification failed for JOHN SMITH, DOB 05/06/1982.",
            },
            llm_fallback=stub,
        )
        self.assertEqual(parsed.status, "rejected")
        self.assertEqual(stub.kyc_calls, 0)

    def test_kyc_llm_fallback_and_pending_clue(self) -> None:
        text = "Onboarding review note: customer submitted French papers on 2024-05-01."
        no_llm = parse_kyc_note(
            {"note_id": "N-UNK", "created_at": "2024-05-01", "channel": "kyc", "text": text}
        )
        self.assertEqual(no_llm.status, "unknown")
        self.assertEqual(len(no_llm.pending_clues), 1)
        self.assertEqual(no_llm.pending_clues[0].status, FactStatus.PENDING_CLUE)

        stub = _StubLLMFallback(
            kyc_result=ExtractionResult(
                task=TASK_KYC_NOTE,
                source_id="N-UNK",
                status=FactStatus.VERIFIED,
                payload={
                    "status": "approved",
                    "verified_name": "LUC MOREL",
                    "verified_dob": "1984-07-09",
                    "document_country": "France",
                    "document_number": "FR-991",
                    "checked_at": "2024-05-01",
                },
            )
        )
        with_llm = parse_kyc_note(
            {"note_id": "N-UNK", "created_at": "2024-05-01", "channel": "kyc", "text": text},
            llm_fallback=stub,
        )
        self.assertEqual(stub.kyc_calls, 1)
        self.assertEqual(with_llm.status, "approved")
        self.assertEqual(with_llm.verified_name, "LUC MOREL")
        self.assertEqual(with_llm.verified_dob.iso_str, "1984-07-09")
        self.assertEqual(with_llm.doc_country, "France")
        self.assertEqual(with_llm.doc_number, "FR-991")
        self.assertEqual(with_llm.pending_clues, ())


class ResidenceNoteParserTest(unittest.TestCase):
    """Tests for non-KYC support residence note extraction."""

    def test_boilerplate_transfer_only_and_transfer_plus_residence(self) -> None:
        bp = parse_residence_note(
            {
                "note_id": "N-BP",
                "created_at": "2025-01-01",
                "channel": "phone",
                "text": "Customer called to ask how long an international transfer takes. Gave the usual timelines.",
            }
        )
        self.assertEqual(bp, ((), ()))

        xfer = parse_residence_note(
            {
                "note_id": "N-XF",
                "created_at": "2025-01-01",
                "channel": "email",
                "text": "Customer sent money via wire transfer to Peru last week.",
            }
        )
        self.assertEqual(xfer, ((), ()))

        # Transfer + residence in same note must extract India
        both_facts, both_clues = parse_residence_note(
            {
                "note_id": "N-IND",
                "created_at": "2026-02-24",
                "channel": "email",
                "text": "The customer noted that their past international transfer originated from their time living and working in India between 2013 and 2014.",
            }
        )
        self.assertEqual(both_clues, ())
        self.assertEqual(len(both_facts), 1)
        self.assertEqual(both_facts[0].value, "India")
        self.assertEqual(both_facts[0].source_type, "NOTE")

    def test_third_party_negation_and_ambiguous_fallback(self) -> None:
        # Third-party and negated mentions must never emit customer countries
        self.assertEqual(
            parse_residence_note(
                {
                    "note_id": "N-BRO",
                    "created_at": "2025-01-01",
                    "channel": "email",
                    "text": "Customer mentioned his brother lived in Peru for five years.",
                }
            ),
            ((), ()),
        )
        self.assertEqual(
            parse_residence_note(
                {
                    "note_id": "N-NEG",
                    "created_at": "2025-01-01",
                    "channel": "email",
                    "text": "Customer confirmed they never lived in Brazil.",
                }
            ),
            ((), ()),
        )

        # Ambiguous non-transfer note emits PENDING_CLUE without LLM, or resolves with LLM
        amb_note = {
            "note_id": "N-AMB",
            "created_at": "2025-02-01",
            "channel": "phone",
            "text": "Customer spent 2015 in Colombia.",
        }
        f_no_llm, c_no_llm = parse_residence_note(amb_note)
        self.assertEqual(f_no_llm, ())
        self.assertEqual(len(c_no_llm), 1)
        self.assertEqual(c_no_llm[0].status, FactStatus.PENDING_CLUE)

        stub = _StubLLMFallback(
            res_result=ExtractionResult(
                task=TASK_RESIDENCE_NOTE,
                source_id="N-AMB",
                status=FactStatus.VERIFIED,
                payload={
                    "lived_countries": [
                        {
                            "country": "Colombia",
                            "subject": "customer",
                            "evidence_quote": "Customer spent 2015 in Colombia.",
                        }
                    ]
                },
            )
        )
        f_llm, c_llm = parse_residence_note(amb_note, llm_fallback=stub)
        self.assertEqual(stub.res_calls, 1)
        self.assertEqual(c_llm, ())
        self.assertEqual([f.value for f in f_llm], ["Colombia"])

    def test_parse_customer_notes_splits_channels(self) -> None:
        kyc, res, clues = parse_customer_notes(
            [
                {
                    "note_id": "N-2",
                    "created_at": "2024-01-01",
                    "channel": "email",
                    "text": "Client resided in Portugal from 2015 to 2018.",
                },
                {
                    "note_id": "N-1",
                    "created_at": "2024-01-02",
                    "channel": "kyc",
                    "text": "Onfido approved JANE DOE (DOB 1990-02-03) on 2024-01-02 via Spain passport.",
                },
            ]
        )
        self.assertEqual(len(kyc), 1)
        self.assertEqual(kyc[0].verified_name, "JANE DOE")
        self.assertEqual([f.value for f in res], ["Portugal"])
        self.assertEqual(clues, ())


@unittest.skipUnless(os.path.isdir(DEV_DIR), "dev/ dataset not present")
class DevNotesAcceptanceTest(unittest.TestCase):
    """Fast dev/notes.jsonl acceptance checks."""

    def test_dev_residence_and_kyc_notes(self) -> None:
        notes_path = os.path.join(DEV_DIR, "notes.jsonl")
        with open(notes_path, encoding="utf-8") as fh:
            notes = [json.loads(line) for line in fh if line.strip()]

        kyc_notes = [n for n in notes if n.get("channel") == "kyc"]
        other_notes = [n for n in notes if n.get("channel") != "kyc"]
        self.assertEqual(len(kyc_notes), 22575)
        self.assertEqual(len(other_notes), 9320)

        # All 9,320 non-KYC notes: 6,253 boilerplate -> empty; 3,067 unique -> verified countries
        freq = Counter(n["text"] for n in other_notes)
        boilerplate_hits = 0
        unique_verified = 0
        note_7f183d_countries = []
        for n in other_notes:
            facts, clues = parse_residence_note(n)
            if freq[n["text"]] > 1:
                if facts or clues:
                    boilerplate_hits += 1
            else:
                if facts and not clues:
                    unique_verified += 1
                if n["note_id"] == "NOTE-7F183D":
                    note_7f183d_countries = [f.value for f in facts]

        self.assertEqual(boilerplate_hits, 0)
        self.assertEqual(unique_verified, 3067)
        self.assertEqual(note_7f183d_countries, ["India"])

        # Specific tricky KYC notes + deterministic 250-note sample
        by_id = {n["note_id"]: n for n in kyc_notes}
        p_071 = parse_kyc_note(by_id["NOTE-071E74"])
        self.assertEqual(p_071.status, "approved")
        self.assertEqual(p_071.verified_name, "LIAM WRIGHT")
        self.assertEqual(p_071.verified_dob.iso_str, "1963-10-06")
        self.assertEqual(p_071.doc_country, "United Kingdom")

        for nid, exp_name in (
            ("NOTE-0C692B", "N/A GILBERT"),
            ("NOTE-149FF8", "PAWEŁ N/A N/A"),
            ("NOTE-2A1C15", "MILAN SETFANOVIĆ"),
            ("NOTE-F5EC65", "PROKOP ONDŘEJ"),
        ):
            parsed = parse_kyc_note(by_id[nid])
            self.assertEqual(parsed.verified_name, exp_name)
            self.assertIsNotNone(parsed.verified_dob)
            self.assertIsNotNone(parsed.doc_country)

        step = max(1, len(kyc_notes) // 250)
        sample = kyc_notes[::step][:250]
        for n in sample:
            p = parse_kyc_note(n)
            self.assertNotIn(p.status, ("rejected", "expired"))
            self.assertTrue(p.verified_name)
            self.assertIsNotNone(p.verified_dob)
            self.assertIsNotNone(p.doc_country)
            self.assertTrue(p.checked_at)

    def test_hygiene_no_leakage_and_lf_endings(self) -> None:
        mod_path = os.path.join(SYSTEM_DIR, "customer_notes.py")
        test_path = os.path.abspath(__file__)
        for path in (mod_path, test_path):
            with open(path, "rb") as fh:
                self.assertNotIn(b"\r\n", fh.read(), f"CRLF found in {path}")

        with open(mod_path, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("dev/", "practice_labels"):
            self.assertNotIn(forbidden, source)
        self.assertIsNone(
            re.search(r"\b(?:H|CUS|NOTE|A)-[0-9A-Fa-f]{4,}\b", source)
        )


if __name__ == "__main__":
    unittest.main()
