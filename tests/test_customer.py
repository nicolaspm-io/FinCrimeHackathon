"""Unit and dev acceptance tests for system/customer.py (Track D)."""
from __future__ import annotations

from collections import defaultdict
import json
import os
import re
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_DIR = os.path.join(REPO_ROOT, "system")
DEV_DIR = os.path.join(REPO_ROOT, "dev")

if SYSTEM_DIR not in sys.path:
    sys.path.insert(0, SYSTEM_DIR)

from contracts import ExtractionResult, FactStatus, TASK_KYC_NOTE, TASK_RESIDENCE_NOTE
from customer import CustomerGraph, CustomerResolver
from data import load_dataset


class _StubCustomerLLM:
    """Stub LLMFallback verifying customer hook propagation."""

    def __init__(self, *, pending: bool = False) -> None:
        self.pending = pending
        self.kyc_calls = 0
        self.res_calls = 0

    def extract_kyc_note(self, text: str, source_id: str, *, created_at: str = "") -> ExtractionResult:
        self.kyc_calls += 1
        if self.pending:
            return ExtractionResult(TASK_KYC_NOTE, source_id, FactStatus.PENDING_CLUE, error="stub")
        return ExtractionResult(
            TASK_KYC_NOTE,
            source_id,
            FactStatus.VERIFIED,
            payload={
                "status": "approved",
                "verified_name": "ELENA ROSSI",
                "verified_dob": "1986-07-14",
                "document_country": "Italy",
                "checked_at": "2024-06-01",
            },
        )

    def extract_residence_countries(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        self.res_calls += 1
        if self.pending:
            return ExtractionResult(TASK_RESIDENCE_NOTE, source_id, FactStatus.PENDING_CLUE, error="stub")
        return ExtractionResult(
            TASK_RESIDENCE_NOTE,
            source_id,
            FactStatus.VERIFIED,
            payload={
                "lived_countries": [
                    {
                        "country": "Brazil",
                        "subject": "customer",
                        "evidence_quote": text,
                    }
                ]
            },
        )

    def extract_hit_p1_dob(self, text: str, source_id: str, *, reference_date: str = "") -> ExtractionResult:
        raise NotImplementedError

    def extract_sr_facts(
        self, text: str, source_id: str, *, category_phrase: str = "", hit_countries: tuple[str, ...] = ()
    ) -> ExtractionResult:
        raise NotImplementedError


def _write_synth_dir(
    folder: str,
    *,
    customers: list[dict[str, object]],
    accounts: list[dict[str, object]],
    onboarding: list[dict[str, object]],
    kyc_checks: list[dict[str, object]],
    notes: list[dict[str, object]],
    alerts: list[dict[str, object]],
    hits: list[dict[str, object]],
) -> None:
    """Write a complete synthetic screening folder."""
    tables = {
        "customers": customers,
        "accounts": accounts,
        "onboarding": onboarding,
        "kyc_checks": kyc_checks,
        "notes": notes,
        "alerts": alerts,
        "hits": hits,
        "search_results": [],
    }
    for name, rows in tables.items():
        with open(os.path.join(folder, f"{name}.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row))
                fh.write("\n")
    with open(os.path.join(folder, "name_variants.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "groups": [["robert", "bob"], ["muhammad", "mohamed"]],
                "company_legal_forms": ["ltd", "gmbh", "sa"],
            },
            fh,
        )


class SyntheticCustomerTest(unittest.TestCase):
    """Synthetic unit tests for CustomerGraph and CustomerResolver."""

    def test_union_find_phone_dob_docs_and_no_family_or_address_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_synth_dir(
                tmp,
                customers=[
                    {
                        "record_id": "CUS-000001",
                        "entity_type": "Individual",
                        "full_name": "Marcos Costa",
                        "dob": "1985-09-12",
                        "nationality": "Brazil",
                        "residence_country": "France",
                        "city": "Lyon",
                        "address": "10 Rue de Lyon",
                        "email": "m1@example.com",
                        "phone": "+33 6 11 22 33 44",
                    },
                    {
                        "record_id": "CUS-000002",
                        "entity_type": "Individual",
                        "full_name": "Costa Marcos",
                        "dob": "12/09/1985",
                        "nationality": "Brazil",
                        "residence_country": "Brazil",
                        "city": "Rio",
                        "address": "10 Rue de Lyon",
                        "email": "m2@example.com",
                        "phone": "",  # phone comes from accounts.phone
                    },
                    {
                        "record_id": "CUS-000003",
                        "entity_type": "Individual",
                        "full_name": "M. Costa",
                        "dob": "September 12, 1985",
                        "nationality": "Brazil",
                        "residence_country": "Spain",
                        "city": "Madrid",
                        "address": "10 Rue de Lyon",
                        "email": "m3@example.com",
                        "phone": "+33 6 99 88 77 66",  # distinct phone, merges via approved doc
                    },
                    {
                        "record_id": "CUS-000004",
                        "entity_type": "Individual",
                        "full_name": "Camille Costa",
                        "dob": "1960-02-10",  # family member sharing phone + address!
                        "nationality": "France",
                        "residence_country": "France",
                        "city": "Lyon",
                        "address": "10 Rue de Lyon",
                        "email": "fam@example.com",
                        "phone": "+33 6 11 22 33 44",
                    },
                    {
                        "record_id": "CUS-000005",
                        "entity_type": "Individual",
                        "full_name": "Partial Dob Cousin",
                        "dob": "1985",  # year-only DOB must NOT merge on phone+full_dob
                        "nationality": "France",
                        "residence_country": "France",
                        "city": "Lyon",
                        "address": "10 Rue de Lyon",
                        "email": "part@example.com",
                        "phone": "+33 6 11 22 33 44",
                    },
                    {
                        "record_id": "CUS-000006",
                        "entity_type": "Individual",
                        "full_name": "Rejected Doc Holder",
                        "dob": "1991-01-01",
                        "nationality": "Spain",
                        "residence_country": "Spain",
                        "city": "Madrid",
                        "address": "Other",
                        "email": "rej@example.com",
                        "phone": "+34 600 000 001",
                    },
                ],
                accounts=[
                    {"account_id": "ACC-1", "customer_id": "CUS-000001", "login_handle": "m1", "phone": ""},
                    {"account_id": "ACC-2", "customer_id": "CUS-000002", "login_handle": "m2", "phone": "+33611223344"},
                    {"account_id": "ACC-3", "customer_id": "CUS-000003", "login_handle": "m3", "phone": ""},
                    {"account_id": "ACC-4", "customer_id": "CUS-000004", "login_handle": "fam", "phone": ""},
                    {"account_id": "ACC-5", "customer_id": "CUS-000005", "login_handle": "part", "phone": ""},
                    {"account_id": "ACC-6", "customer_id": "CUS-000006", "login_handle": "rej", "phone": ""},
                ],
                onboarding=[
                    {"account_id": "ACC-2", "customer_id": "CUS-000002", "kyc_applicant_id": "APP-2"},
                    {"account_id": "ACC-3", "customer_id": "CUS-000003", "kyc_applicant_id": "APP-3"},
                    {"account_id": "ACC-6", "customer_id": "CUS-000006", "kyc_applicant_id": "APP-6"},
                ],
                kyc_checks=[
                    {
                        "check_id": "KYC-2",
                        "applicant_id": "APP-2",
                        "verified_name": "MARCOS COSTA",
                        "verified_dob": "1985-09-12",
                        "document_country": "Brazil",
                        "document_number": "DOC-SHARED-1",
                        "result": "approved",
                        "checked_at": "2024-01-01",
                    },
                    {
                        "check_id": "KYC-3",
                        "applicant_id": "APP-3",
                        "verified_name": "MARCOS COSTA",
                        "verified_dob": "1985-09-12",
                        "document_country": "Brazil",
                        "document_number": "DOC-SHARED-1",
                        "result": "approved",
                        "checked_at": "2024-05-01",
                    },
                    {
                        "check_id": "KYC-6",
                        "applicant_id": "APP-6",
                        "verified_name": "OTHER PERSON",
                        "verified_dob": "1991-01-01",
                        "document_country": "Spain",
                        "document_number": "DOC-SHARED-1",
                        "result": "rejected",
                        "checked_at": "2024-06-01",
                    },
                ],
                notes=[],
                alerts=[],
                hits=[],
            )
            ds = load_dataset(tmp)
            graph = CustomerGraph(ds)
            self.assertEqual(
                graph.cluster_of("CUS-000001"),
                ("CUS-000001", "CUS-000002", "CUS-000003"),
            )
            self.assertEqual(graph.cluster_of("CUS-000004"), ("CUS-000004",))
            self.assertEqual(graph.cluster_of("CUS-000005"), ("CUS-000005",))
            self.assertEqual(graph.cluster_of("CUS-000006"), ("CUS-000006",))

    def test_joint_account_kyc_isolation_approved_before_latest_and_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_synth_dir(
                tmp,
                customers=[
                    {
                        "record_id": "CUS-000010",
                        "entity_type": "Individual",
                        "full_name": "Robert Miller",
                        "preferred_name": "Bob",
                        "dob": "1980-04-05",
                        "nationality": "United Kingdom",
                        "residence_country": "United Kingdom",
                        "city": "London",
                        "address": "1 Baker St",
                        "email": "bob@example.com",
                        "phone": "+44111111111",
                    },
                    {
                        "record_id": "CUS-000011",
                        "entity_type": "Individual",
                        "full_name": "Alice Miller",
                        "dob": "1982-08-09",
                        "nationality": "Canada",
                        "residence_country": "United Kingdom",
                        "city": "London",
                        "address": "1 Baker St",
                        "email": "alice@example.com",
                        "phone": "+44222222222",
                    },
                    {
                        "record_id": "CUS-000020",
                        "entity_type": "Individual",
                        "full_name": "Elena Rossi",
                        "dob": None,
                        "nationality": "Italy",
                        "residence_country": "Italy",
                        "city": "Rome",
                        "address": "Via Roma 5",
                        "email": "elena@example.com",
                        "phone": "+390000000",
                    },
                ],
                accounts=[
                    {"account_id": "ACC-J", "customer_id": "CUS-000010", "login_handle": "bobm", "phone": ""},
                    {"account_id": "ACC-J", "customer_id": "CUS-000011", "login_handle": "alicem", "phone": ""},
                    {"account_id": "ACC-E", "customer_id": "CUS-000020", "login_handle": "elenar", "phone": ""},
                ],
                onboarding=[
                    {"account_id": "ACC-J", "customer_id": "CUS-000010", "kyc_applicant_id": "APP-10"},
                    {"account_id": "ACC-J", "customer_id": "CUS-000011", "kyc_applicant_id": "APP-11"},
                ],
                kyc_checks=[
                    # Older approved check vs newer rejected + expired checks
                    {
                        "check_id": "KYC-10-APP",
                        "applicant_id": "APP-10",
                        "verified_name": "ROBERT JAMES MILLER",
                        "verified_dob": "05/04/1980",
                        "document_country": "United Kingdom",
                        "document_number": "UK-10",
                        "result": "approved",
                        "checked_at": "2023-05-01",
                    },
                    {
                        "check_id": "KYC-10-TIE",
                        "applicant_id": "APP-10",
                        "verified_name": "ROBERT JAMES MILLER",
                        "verified_dob": "1980-04-05",
                        "document_country": "Ireland",  # tied approved conflict on country!
                        "document_number": "IE-10",
                        "result": "approved",
                        "checked_at": "2023-05-01",
                    },
                    {
                        "check_id": "KYC-10-REJ",
                        "applicant_id": "APP-10",
                        "verified_name": "WRONG NAME",
                        "verified_dob": "1970-01-01",
                        "document_country": "Spain",
                        "document_number": "ES-10",
                        "result": "rejected",
                        "checked_at": "2025-01-01",
                    },
                    {
                        "check_id": "KYC-11-APP",
                        "applicant_id": "APP-11",
                        "verified_name": "ALICE MILLER",
                        "verified_dob": "1982-08-09",
                        "document_country": "Canada",
                        "document_number": "CA-11",
                        "result": "approved",
                        "checked_at": "2024-01-01",
                    },
                ],
                notes=[
                    {
                        "note_id": "NOTE-BOB",
                        "requester_email": "bob@example.com",
                        "created_at": "2024-02-01",
                        "channel": "phone",
                        "text": "Customer lived and worked in Germany between 2014 and 2016.",
                    },
                    {
                        "note_id": "NOTE-ELENA-KYC",
                        "requester_email": "elena@example.com",
                        "created_at": "2024-06-01",
                        "channel": "kyc",
                        "text": "Onboarding note requiring fallback parsing.",
                    },
                    {
                        "note_id": "NOTE-ELENA-RES",
                        "requester_email": "elena@example.com",
                        "created_at": "2024-06-02",
                        "channel": "email",
                        "text": "Customer spent 2017 in Brazil.",
                    },
                ],
                alerts=[
                    {
                        "alert_id": "A-JOINT",
                        "screened_at": "2025-06-01",
                        "customer": {"name": "Bob Miller", "entity_type": "Individual", "dob": "1980-04-05"},
                        "account_ref": "ACC-J",
                        "login_handle": None,
                        "email": None,
                    },
                    {
                        "alert_id": "A-ELENA",
                        "screened_at": "2025-06-01",
                        "customer": {"name": "Elena Rossi", "entity_type": "Individual", "dob": "1986-07-14", "country": "Italy"},
                        "account_ref": None,
                        "login_handle": None,
                        "email": None,
                    },
                ],
                hits=[],
            )
            ds = load_dataset(tmp)

            # 1. Joint account disambiguation + approved-before-latest + tied conflicts + note country
            res = CustomerResolver(ds)
            cands_joint = res.resolve_alert(ds.alerts_by_id["A-JOINT"])
            self.assertEqual(len(cands_joint), 1)
            bob = cands_joint[0]
            self.assertEqual(bob.record_ids, ("CUS-000010",))
            self.assertIsNotNone(bob.verified)
            self.assertEqual(bob.verified.name.value, "ROBERT JAMES MILLER")
            self.assertEqual(bob.verified.dob.value.iso_str, "1980-04-05")
            # Conflict retained on tied approved checks (United Kingdom vs Ireland)
            self.assertGreaterEqual(len(bob.verified.conflict_facts), 2)
            # Bob must NOT inherit Alice's Canada country; MUST include Germany note + UK/Ireland
            country_vals = {f.value for f in bob.countries}
            self.assertIn("United Kingdom", country_vals)
            self.assertIn("Germany", country_vals)
            self.assertNotIn("Canada", country_vals)

            # 2. LLMFallback hooks: pending_clues when fallback fails, VERIFIED when fallback succeeds
            res_pending = CustomerResolver(ds, llm_fallback=_StubCustomerLLM(pending=True))
            elena_p = res_pending.resolve_alert(ds.alerts_by_id["A-ELENA"])[0]
            self.assertEqual(len(elena_p.pending_clues), 2)

            stub_ok = _StubCustomerLLM(pending=False)
            res_ok = CustomerResolver(ds, llm_fallback=stub_ok)
            elena_ok = res_ok.resolve_alert(ds.alerts_by_id["A-ELENA"])[0]
            self.assertEqual(elena_ok.pending_clues, ())
            self.assertEqual(elena_ok.verified.name.value, "ELENA ROSSI")
            self.assertEqual(elena_ok.verified.dob.value.iso_str, "1986-07-14")
            self.assertIn("Brazil", {f.value for f in elena_ok.countries})


@unittest.skipUnless(os.path.isdir(DEV_DIR), "dev/ dataset not present")
class DevCustomerAcceptanceTest(unittest.TestCase):
    """Acceptance checks verified against the full dev/ dataset."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ds = load_dataset(DEV_DIR)
        t0 = time.perf_counter()
        cls.resolver = CustomerResolver(cls.ds)
        cls.init_seconds = time.perf_counter() - t0
        print(f"\n[dev customer] CustomerResolver init took {cls.init_seconds:.3f}s")

    def test_graph_stats_116_phone_dob_7_docs_and_68_families(self) -> None:
        stats = self.resolver.graph.stats()
        self.assertEqual(stats["total_records"], 72265)
        self.assertEqual(stats["phone_dob_clusters"], 116)
        self.assertEqual(stats["shared_doc_clusters"], 7)
        self.assertEqual(stats["family_shared_phones"], 68)

    def test_resolve_all_440_alerts_and_joint_accounts(self) -> None:
        t0 = time.perf_counter()
        resolved = {al["alert_id"]: self.resolver.resolve_alert(al) for al in self.ds.alerts}
        elapsed = time.perf_counter() - t0
        print(f"[dev customer] Resolved all 440 alerts in {elapsed:.3f}s")
        self.assertLess(elapsed, 10.0)

        # Every linked alert (170 account, 70 login, 54 email) resolves to >= 1 candidate
        joint_resolved = 0
        for al in self.ds.alerts:
            if al.get("account_ref") or al.get("login_handle") or al.get("email"):
                self.assertGreaterEqual(
                    len(resolved[al["alert_id"]]), 1, f"Unresolved linked {al['alert_id']}"
                )
                if al.get("account_ref") and len(self.ds.get_account_holders(al["account_ref"])) == 2:
                    joint_resolved += 1
        self.assertEqual(joint_resolved, 58)

        # Specific forensic cluster merge on joint account A-957E63 (H-0181BD)
        a_957 = resolved["A-957E63"]
        self.assertEqual(len(a_957), 1)
        self.assertIn("CUS-775419", a_957[0].record_ids)
        self.assertIn("CUS-138588", a_957[0].record_ids)

    def test_linkless_100_percent_recall_sample_and_approved_before_latest(self) -> None:
        # Stratified 5-alert sample across linkless shapes vs brute-force scan
        linkless = [
            al
            for al in self.ds.alerts
            if not (al.get("account_ref") or al.get("login_handle") or al.get("email"))
        ]
        self.assertEqual(len(linkless), 146)
        by_id = {al["alert_id"]: al for al in linkless}
        sample_ids = []
        # Pick 1 Company, 1 year-month, 1 year-only, 1 full DOB, and A-688B31 (multi-match)
        sample_ids.append("A-688B31")
        for pred in (
            lambda a: (a.get("customer") or {}).get("entity_type") == "Company",
            lambda a: re.fullmatch(r"\d{4}-\d{2}", str((a.get("customer") or {}).get("dob") or "")) is not None,
            lambda a: re.fullmatch(r"\d{4}", str((a.get("customer") or {}).get("dob") or "")) is not None,
            lambda a: re.fullmatch(r"\d{4}-\d{2}-\d{2}", str((a.get("customer") or {}).get("dob") or "")) is not None,
        ):
            for al in linkless:
                if pred(al) and al["alert_id"] not in sample_ids:
                    sample_ids.append(al["alert_id"])
                    break

        for aid in sample_ids:
            al = by_id[aid]
            idx_res = tuple(c.record_ids for c in self.resolver.resolve_alert(al))
            bf_res = tuple(c.record_ids for c in self.resolver.resolve_linkless_bruteforce(al))
            self.assertEqual(idx_res, bf_res, f"Recall mismatch on {aid}")

        # Verify 1,684 applicants have rejected/expired check newer than latest approved
        newer_bad_apps = []
        for app_id, checks in self.ds.kyc_by_applicant.items():
            app = [c for c in checks if c.get("result") == "approved"]
            bad = [c for c in checks if c.get("result") != "approved"]
            if app and bad and max(c["checked_at"] for c in bad) > max(c["checked_at"] for c in app):
                newer_bad_apps.append((app_id, max(app, key=lambda c: c["checked_at"])))
        self.assertEqual(len(newer_bad_apps), 1684)

        # Spot-check 25 applicants from the 1,684 via build_candidate_person
        for app_id, expected_chk in newer_bad_apps[:25]:
            cid = self.ds.applicant_to_customers[app_id][0]
            person = self.resolver.build_candidate_person(self.resolver.graph.cluster_of(cid))
            self.assertIsNotNone(person.verified)
            if person.verified.source_id.startswith("KYC-"):
                self.assertEqual(person.verified.checked_at, expected_chk["checked_at"])

    def test_diagnostic_practice_labels_customer_record_accuracy(self) -> None:
        labels_path = os.path.join(DEV_DIR, "practice_labels.jsonl")
        with open(labels_path, encoding="utf-8") as fh:
            labels = [json.loads(line) for line in fh if line.strip()]
        resolved = {al["alert_id"]: self.resolver.resolve_alert(al) for al in self.ds.alerts}
        matched = 0
        mismatches = []
        for row in labels:
            hit = self.ds.hits_by_id[row["hit_id"]]
            cands = resolved[hit["alert_id"]]
            if not cands:
                ok = None in row["customer_record_ids"]
                got = None
            elif len(cands) > 1:
                ok = "AMBIGUOUS" in row["customer_record_ids"]
                got = "AMBIGUOUS"
            else:
                ok = any(rid in row["customer_record_ids"] for rid in cands[0].record_ids)
                got = cands[0].record_ids
            if ok:
                matched += 1
            else:
                mismatches.append((row["hit_id"], hit["alert_id"], got, row["customer_record_ids"]))
        print(f"[dev customer] Practice labels customer_record_id accuracy: {matched}/{len(labels)}")
        if mismatches:
            print("  Mismatches:", mismatches)
        self.assertGreaterEqual(matched, 48)

    def test_hygiene_no_leakage_and_lf_endings(self) -> None:
        mod_path = os.path.join(SYSTEM_DIR, "customer.py")
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
