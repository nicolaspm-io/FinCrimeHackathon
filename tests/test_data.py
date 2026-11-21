"""Unit and dev-dataset acceptance tests for system/data.py (Track B)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_DIR = os.path.join(REPO_ROOT, "system")
DEV_DIR = os.path.join(REPO_ROOT, "dev")

if SYSTEM_DIR not in sys.path:
    sys.path.insert(0, SYSTEM_DIR)

from data import (
    ScreeningDataset,
    extract_index_tokens,
    extract_year_hint,
    load_dataset,
    normalize_email,
    normalize_handle,
    normalize_phone_digits,
    provider_query_key,
)


def _write_synthetic_dataset(
    folder: str,
    *,
    tables: dict[str, list[dict[str, object]]],
    name_variants: dict[str, object] | None = None,
    shuffle_seed: int | None = None,
) -> None:
    """Write a complete synthetic dataset directory for testing."""
    rng = random.Random(shuffle_seed) if shuffle_seed is not None else None
    for table_name, rows in tables.items():
        copied = list(rows)
        if rng is not None:
            rng.shuffle(copied)
        with open(
            os.path.join(folder, f"{table_name}.jsonl"),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            for row in copied:
                handle.write(json.dumps(row, ensure_ascii=False))
                handle.write("\n")

    variants = name_variants or {
        "description": "synthetic variants",
        "groups": [["muhammad", "mohamed"], ["robert", "bob"]],
        "company_legal_forms": ["gmbh", "ltd", "sa"],
    }
    with open(
        os.path.join(folder, "name_variants.json"),
        "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        json.dump(variants, handle)


def _base_synthetic_tables() -> dict[str, list[dict[str, object]]]:
    """Build a compact synthetic dataset covering joint accounts, KYC, and linkless lookups."""
    return {
        "customers": [
            {
                "record_id": "CUS-000001",
                "entity_type": "Individual",
                "full_name": "Alice Marie Example",
                "preferred_name": "Ali Example",
                "login_name": "AliceLogin",
                "dob": "1985-04-12",
                "email": "Alice@Example.COM ",
                "phone": "+44 7700 900001",
            },
            {
                "record_id": "CUS-000002",
                "entity_type": "Individual",
                "full_name": "Bob Joint Holder",
                "preferred_name": None,
                "login_name": None,
                "dob": "1979-11-03",
                "email": "bob@example.com",
                "phone": "+44-7700-900002",
            },
            {
                "record_id": "CUS-000003",
                "entity_type": "Individual",
                "full_name": "Clara NoteOnly",
                "preferred_name": None,
                "login_name": None,
                "dob": None,
                "email": "clara@example.com",
                "phone": "",
            },
            {
                "record_id": "CUS-000004",
                "entity_type": "Individual",
                "full_name": "David ApprovedKyc",
                "preferred_name": None,
                "login_name": None,
                "dob": None,
                "email": "david@example.com",
                "phone": "",
            },
        ],
        "accounts": [
            {
                "account_id": "ACC-000001",
                "customer_id": "CUS-000002",
                "login_handle": "bobhandle99",
                "phone": "+447700900002",
                "opened_at": "2022-01-01",
                "status": "active",
            },
            {
                "account_id": "ACC-000001",
                "customer_id": "CUS-000001",
                "login_handle": " Alice_Handle ",
                "phone": "+447700900001",
                "opened_at": "2022-01-01",
                "status": "active",
            },
            {
                "account_id": "ACC-000002",
                "customer_id": "CUS-000003",
                "login_handle": "clara_h",
                "phone": "",
                "opened_at": "2023-05-01",
                "status": "active",
            },
            {
                "account_id": "ACC-000003",
                "customer_id": "CUS-000004",
                "login_handle": "david_h",
                "phone": "",
                "opened_at": "2023-06-01",
                "status": "active",
            },
        ],
        "onboarding": [
            {
                "account_id": "ACC-000001",
                "customer_id": "CUS-000001",
                "kyc_applicant_id": "APP-000001",
                "onboarded_at": "2022-01-01",
            },
            {
                "account_id": "ACC-000001",
                "customer_id": "CUS-000002",
                "kyc_applicant_id": "APP-000002",
                "onboarded_at": "2022-01-01",
            },
            {
                "account_id": "ACC-000002",
                "customer_id": "CUS-000003",
                "kyc_applicant_id": "APP-000003",
                "onboarded_at": "2023-05-01",
            },
            {
                "account_id": "ACC-000003",
                "customer_id": "CUS-000004",
                "kyc_applicant_id": "APP-000004",
                "onboarded_at": "2023-06-01",
            },
        ],
        "kyc_checks": [
            {
                "check_id": "KYC-000001",
                "applicant_id": "APP-000001",
                "verified_dob": "1985-04-12",
                "result": "approved",
                "checked_at": "2022-01-02",
            },
            {
                "check_id": "KYC-000002",
                "applicant_id": "APP-000002",
                "verified_dob": "1979-11-03",
                "result": "approved",
                "checked_at": "2022-01-02",
            },
            {
                "check_id": "KYC-000003",
                "applicant_id": "APP-000003",
                "verified_dob": "1991-02-10",
                "result": "rejected",
                "checked_at": "2023-05-02",
            },
            {
                "check_id": "KYC-000004",
                "applicant_id": "APP-000004",
                "verified_dob": "1992-07-19",
                "result": "approved",
                "checked_at": "2023-06-02",
            },
            {
                "check_id": "KYC-000005",
                "applicant_id": "APP-000004",
                "verified_dob": "1999-01-01",
                "result": "expired",
                "checked_at": "2024-01-01",
            },
        ],
        "notes": [
            {
                "note_id": "NOTE-000001",
                "requester_email": " ALICE@example.com",
                "created_at": "2023-01-01",
                "channel": "support",
                "text": "Customer lived in Spain in 2020.",
            }
        ],
        "alerts": [
            {
                "alert_id": "A-000001",
                "screened_at": "2025-01-01",
                "customer": {"name": "Alice Example"},
                "account_ref": "ACC-000001",
                "login_handle": None,
                "email": None,
                "hit_ids": ["H-000001"],
            }
        ],
        "hits": [
            {
                "hit_id": "H-000001",
                "alert_id": "A-000001",
                "entity_type": "Individual",
                "name": "Example, Alice A.",
            }
        ],
        "search_results": [
            {
                "result_id": "SR-000001",
                "query": "alice example",
                "title": "Result",
                "snippet": "Snippet",
                "source": "web",
            }
        ],
    }


class DataHelpersTest(unittest.TestCase):
    """Tests for key normalization and helper functions in system/data.py."""

    def test_provider_query_key_edge_cases(self) -> None:
        self.assertEqual(
            provider_query_key("Markus A. Müller-Kägi"),
            ("kagi", "markus", "muller"),
        )
        self.assertEqual(
            provider_query_key("John (AKA Johnny) [PEP] Smith"),
            ("john", "smith"),
        )
        self.assertEqual(provider_query_key("A. B. Mohamed"), ("mohamed",))
        self.assertEqual(provider_query_key("O’Connor, Michael"), ("connor", "michael"))
        self.assertEqual(
            provider_query_key("Drozd, Tomasz Paweł"),
            ("drozd", "pawe", "tomasz"),
        )
        # Repeated tokens MUST be preserved in provider_query_key, deduplicated in index tokens
        self.assertEqual(
            provider_query_key("Abubakar, Abubakar"),
            ("abubakar", "abubakar"),
        )
        self.assertEqual(extract_index_tokens("Abubakar, Abubakar"), ("abubakar",))
        # Purely non-Latin fallback keeps vowel marks attached to words
        self.assertEqual(len(provider_query_key("বাদশা মিয়া")), 2)
        self.assertEqual(provider_query_key(None), ())
        self.assertEqual(provider_query_key("   "), ())

    def test_scalar_normalizers_and_year_hint(self) -> None:
        self.assertEqual(normalize_email("  User@Example.COM "), "user@example.com")
        self.assertEqual(normalize_email(None), "")
        self.assertEqual(normalize_handle(" MyHandle "), "myhandle")
        self.assertEqual(normalize_handle(None), "")
        self.assertEqual(normalize_phone_digits("+44 (0) 7700-900123"), "4407700900123")
        self.assertEqual(normalize_phone_digits(None), "")
        self.assertEqual(extract_year_hint("1987-05-12"), 1987)
        self.assertEqual(extract_year_hint("7 October 1987"), 1987)
        self.assertEqual(extract_year_hint("20/06/1955"), 1955)
        self.assertEqual(extract_year_hint("Born 1974 in Madrid"), 1974)
        self.assertIsNone(extract_year_hint("1987abc"))
        self.assertIsNone(extract_year_hint("123456"))
        self.assertIsNone(extract_year_hint(None))


class SyntheticDatasetTest(unittest.TestCase):
    """Tests for ScreeningDataset indices, invariants, and error handling."""

    def test_indices_joint_accounts_and_kyc_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_synthetic_dataset(tmp, tables=_base_synthetic_tables())
            ds = load_dataset(tmp)

            self.assertIsInstance(ds, ScreeningDataset)
            # Joint account returns all holders sorted
            self.assertEqual(
                ds.get_account_holders("ACC-000001"),
                ("CUS-000001", "CUS-000002"),
            )
            # KYC applicants stay isolated per (account_id, customer_id)
            self.assertEqual(
                ds.get_kyc_applicants("ACC-000001", "CUS-000001"), ("APP-000001",)
            )
            self.assertEqual(
                ds.get_kyc_applicants("ACC-000001", "CUS-000002"), ("APP-000002",)
            )
            self.assertEqual(
                ds.get_kyc_checks_for_customer("CUS-000001")[0]["check_id"],
                "KYC-000001",
            )
            # Login & email case/whitespace normalization
            self.assertEqual(ds.get_customers_by_login("ALICELOGIN"), ("CUS-000001",))
            self.assertEqual(ds.get_customers_by_login("alice_handle"), ("CUS-000001",))
            self.assertEqual(ds.get_customers_by_email("alice@example.com"), ("CUS-000001",))
            self.assertEqual(len(ds.get_notes_for_email("alice@example.com")), 1)
            self.assertEqual(len(ds.get_search_results("Example, Alice A.")), 1)
            self.assertEqual(len(ds.get_hits_for_alert("A-000001")), 1)

            # Birth-year index: approved KYC included, rejected/expired excluded, unknown included
            self.assertEqual(ds.customers_without_birth_year, ("CUS-000003",))
            self.assertEqual(
                ds.candidates_for_birth_year(1992), ("CUS-000003", "CUS-000004")
            )
            self.assertNotIn(1991, ds.customers_by_birth_year)
            self.assertNotIn(1999, ds.customers_by_birth_year)
            self.assertEqual(
                ds.candidates_for_birth_year(None),
                ("CUS-000001", "CUS-000002", "CUS-000003", "CUS-000004"),
            )

            # Name token index covers full_name, preferred_name, login_name, login_handle
            self.assertEqual(ds.candidates_for_name_tokens("ali"), ("CUS-000001",))
            self.assertEqual(ds.candidates_for_name_tokens("alicelogin"), ("CUS-000001",))
            self.assertEqual(ds.candidates_for_name_tokens("alice_handle"), ("CUS-000001",))
            self.assertEqual(ds.candidates_for_name_tokens("bobhandle99"), ("CUS-000002",))

    def test_never_reads_practice_labels_even_if_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_synthetic_dataset(tmp, tables=_base_synthetic_tables())
            with open(
                os.path.join(tmp, "practice_labels.jsonl"), "w", encoding="utf-8"
            ) as fh:
                fh.write("{corrupt json that must never be opened\n")
            ds = load_dataset(tmp)
            self.assertEqual(len(ds.customers), 4)

    def test_row_shuffle_invariance_and_cross_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            tables = _base_synthetic_tables()
            _write_synthetic_dataset(tmp1, tables=tables)
            _write_synthetic_dataset(tmp2, tables=tables, shuffle_seed=99)

            old_cwd = os.getcwd()
            try:
                os.chdir(tempfile.gettempdir())
                ds1 = load_dataset(tmp1)
                ds2 = load_dataset(tmp2)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(ds1.customers, ds2.customers)
            self.assertEqual(ds1.accounts, ds2.accounts)
            self.assertEqual(ds1.onboarding, ds2.onboarding)
            self.assertEqual(ds1.kyc_checks, ds2.kyc_checks)
            self.assertEqual(ds1.notes, ds2.notes)
            self.assertEqual(ds1.alerts, ds2.alerts)
            self.assertEqual(ds1.hits, ds2.hits)
            self.assertEqual(ds1.search_results, ds2.search_results)
            self.assertEqual(ds1.account_holders, ds2.account_holders)
            self.assertEqual(ds1.login_to_customers, ds2.login_to_customers)
            self.assertEqual(ds1.email_to_customers, ds2.email_to_customers)
            self.assertEqual(ds1.customers_by_birth_year, ds2.customers_by_birth_year)
            self.assertEqual(ds1.customers_without_birth_year, ds2.customers_without_birth_year)
            self.assertEqual(ds1.customers_by_name_token, ds2.customers_by_name_token)

    def test_missing_files_and_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_dataset(os.path.join(tmp, "nonexistent"))

            _write_synthetic_dataset(tmp, tables=_base_synthetic_tables())
            os.remove(os.path.join(tmp, "notes.jsonl"))
            with self.assertRaises(FileNotFoundError):
                load_dataset(tmp)

            _write_synthetic_dataset(tmp, tables=_base_synthetic_tables())
            # Inject blank line, malformed JSON, non-object row, and missing ID row
            with open(os.path.join(tmp, "notes.jsonl"), "a", encoding="utf-8") as fh:
                fh.write("\n")
                fh.write("{bad json\n")
                fh.write('["not_an_object"]\n')
                fh.write('{"requester_email": "alice@example.com"}\n')

            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                ds_lenient = load_dataset(tmp, strict=False)
            self.assertEqual(len(ds_lenient.notes), 1)
            self.assertIn("WARNING:", stderr_buf.getvalue())

            with self.assertRaises(ValueError):
                load_dataset(tmp, strict=True)

    def test_hygiene_no_leakage_and_lf_endings(self) -> None:
        data_py = os.path.join(SYSTEM_DIR, "data.py")
        test_py = os.path.abspath(__file__)
        for path in (data_py, test_py):
            with open(path, "rb") as fh:
                self.assertNotIn(b"\r\n", fh.read(), f"CRLF found in {path}")

        with open(data_py, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("dev/", "practice_labels", "H-", "CUS-", "NOTE-", "A-"):
            self.assertNotIn(forbidden, source)


@unittest.skipUnless(os.path.isdir(DEV_DIR), "dev/ dataset not present")
class DevDatasetAcceptanceTest(unittest.TestCase):
    """Acceptance checks verified against the full dev/ dataset."""

    @classmethod
    def setUpClass(cls) -> None:
        t0 = time.perf_counter()
        cls.ds = load_dataset(DEV_DIR)
        cls.load_seconds = time.perf_counter() - t0
        print(f"\n[dev acceptance] load_dataset('dev') took {cls.load_seconds:.3f}s")

    def test_load_budget_and_exact_table_counts(self) -> None:
        self.assertLess(self.load_seconds, 10.0)
        self.assertEqual(len(self.ds.customers), 72265)
        self.assertEqual(len(self.ds.accounts), 86659)
        self.assertEqual(len(self.ds.onboarding), 72265)
        self.assertEqual(len(self.ds.kyc_checks), 54488)
        self.assertEqual(len(self.ds.notes), 31895)
        self.assertEqual(len(self.ds.alerts), 440)
        self.assertEqual(len(self.ds.hits), 819)
        self.assertEqual(len(self.ds.search_results), 16882)
        self.assertEqual(len(self.ds.name_variant_groups), 55)
        self.assertEqual(len(self.ds.company_legal_forms), 30)

    def test_joint_accounts_and_linked_alerts(self) -> None:
        joint_accounts = {
            aid: holders
            for aid, holders in self.ds.account_holders.items()
            if len(holders) > 1
        }
        self.assertEqual(len(joint_accounts), 52)
        self.assertTrue(all(len(h) == 2 for h in joint_accounts.values()))

        joint_alert_count = 0
        acc_alerts = login_alerts = email_alerts = linkless_alerts = 0
        for alert in self.ds.alerts:
            if alert.get("account_ref"):
                acc_alerts += 1
                holders = self.ds.get_account_holders(alert["account_ref"])
                self.assertGreaterEqual(len(holders), 1)
                if len(holders) == 2:
                    joint_alert_count += 1
            elif alert.get("login_handle"):
                login_alerts += 1
                self.assertGreaterEqual(
                    len(self.ds.get_customers_by_login(alert["login_handle"])), 1
                )
            elif alert.get("email"):
                email_alerts += 1
                self.assertGreaterEqual(
                    len(self.ds.get_customers_by_email(alert["email"])), 1
                )
            else:
                linkless_alerts += 1

        self.assertEqual(joint_alert_count, 58)
        self.assertEqual(acc_alerts, 170)
        self.assertEqual(login_alerts, 70)
        self.assertEqual(email_alerts, 54)
        self.assertEqual(linkless_alerts, 146)

    def test_sr_702_coverage(self) -> None:
        self.assertEqual(len(self.ds.sr_by_query_key), 1758)
        ind_hits = [
            hit for hit in self.ds.hits if hit.get("entity_type") == "Individual"
        ]
        self.assertEqual(len(ind_hits), 702)
        matched_keys = set()
        for hit in ind_hits:
            results = self.ds.get_search_results(hit.get("name"))
            self.assertGreaterEqual(len(results), 1, f"Missing SR for {hit['hit_id']}")
            matched_keys.add(provider_query_key(hit.get("name")))
        self.assertEqual(len(matched_keys), 358)

    def test_linkless_inverted_indices(self) -> None:
        by_year = self.ds.customers_by_birth_year
        without_year = self.ds.customers_without_birth_year
        self.assertEqual(len(without_year), 9)
        covered_ids = set(without_year)
        for cids in by_year.values():
            covered_ids.update(cids)
        self.assertEqual(len(covered_ids), 72265)

        for alert in self.ds.alerts:
            if alert.get("account_ref") or alert.get("login_handle") or alert.get("email"):
                continue
            screened = alert.get("customer") or {}
            year = extract_year_hint(screened.get("dob"))
            self.assertGreater(len(self.ds.candidates_for_birth_year(year)), 0)
            if extract_index_tokens(screened.get("name")):
                self.assertGreater(
                    len(self.ds.candidates_for_name_tokens(screened.get("name"))), 0
                )


if __name__ == "__main__":
    unittest.main()
