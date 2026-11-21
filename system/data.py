"""Stdlib data loader and deterministic O(1) / inverted screening indices."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import gc
import json
import os
import re
import sys
from typing import Any, Callable
import unicodedata

try:
    from .contracts import REQUIRED_DATA_FILES
except ImportError:
    from contracts import REQUIRED_DATA_FILES

_BRACKET_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NON_DIGIT_RE = re.compile(r"\D+")
_YEAR_RE = re.compile(r"\b(18\d{2}|19\d{2}|20\d{2})\b")
_CENTURY_PREFIXES = ("18", "19", "20")


def normalize_email(email: str | None) -> str:
    """Normalize an email address for case-insensitive lookup."""
    if not isinstance(email, str):
        return ""
    return email.strip().lower()


def normalize_handle(handle: str | None) -> str:
    """Normalize a login handle or username for case-insensitive lookup."""
    if not isinstance(handle, str):
        return ""
    return handle.strip().lower()


def normalize_phone_digits(phone: str | None) -> str:
    """Strip non-digit characters from a phone string."""
    if not isinstance(phone, str) or not phone:
        return ""
    return _NON_DIGIT_RE.sub("", phone)


def extract_year_hint(value: Any) -> int | None:
    """Extract a 4-digit year hint (1800-2099) from a date string."""
    if not value:
        return None
    text = value if isinstance(value, str) else str(value)
    length = len(text)
    if length >= 4:
        prefix = text[:4]
        if (
            prefix.isascii()
            and prefix.isdigit()
            and prefix[:2] in _CENTURY_PREFIXES
            and (length == 4 or not text[4].isalnum())
        ):
            return int(prefix)
        suffix = text[-4:]
        if (
            suffix.isascii()
            and suffix.isdigit()
            and suffix[:2] in _CENTURY_PREFIXES
            and (length == 4 or not text[-5].isalnum())
        ):
            return int(suffix)
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def _extract_unicode_words(text: str) -> list[str]:
    """Tokenize non-Latin text while keeping combining/spacing marks inside words."""
    tokens: list[str] = []
    current: list[str] = []
    for ch in f"{text} ":
        if unicodedata.category(ch)[0] in ("L", "M", "N"):
            current.append(ch)
        elif current:
            if len(current) > 1:
                tokens.append("".join(current))
            current.clear()
    return tokens


def _tokenize_folded_tokens(text: str | None) -> list[str]:
    """Fold accents, drop bracketed notes, drop 1-char tokens, preserve multiplicity."""
    if not isinstance(text, str) or not text:
        return []
    folded = text
    if not folded.isascii():
        folded = "".join(
            ch
            for ch in unicodedata.normalize("NFKD", folded)
            if not unicodedata.combining(ch)
        )
    if "(" in folded or "[" in folded:
        folded = _BRACKET_RE.sub(" ", folded)
    lowered = folded.lower()
    tokens = [tok for tok in _ASCII_TOKEN_RE.findall(lowered) if len(tok) > 1]
    if tokens:
        return tokens
    return _extract_unicode_words(lowered)


def provider_query_key(text: str | None) -> tuple[str, ...]:
    """Build sorted provider query key preserving repeated tokens (no set dedup)."""
    tokens = _tokenize_folded_tokens(text)
    if not tokens:
        return ()
    tokens.sort()
    return tuple(tokens)


def extract_index_tokens(text: str | None) -> tuple[str, ...]:
    """Build sorted unique normalized name tokens for inverted indexing."""
    tokens = _tokenize_folded_tokens(text)
    if not tokens:
        return ()
    return tuple(sorted(set(tokens)))


def _append_unique_str(
    mapping: dict[Any, list[str]],
    key: Any,
    value: str,
    dirty: list[list[str]],
) -> None:
    """Append a unique string value to a list bucket and track multi-item buckets."""
    bucket = mapping.get(key)
    if bucket is None:
        mapping[key] = [value]
    elif value not in bucket:
        if len(bucket) == 1:
            dirty.append(bucket)
        bucket.append(value)


def _append_bucket(
    mapping: dict[Any, list[dict[str, Any]]],
    key: Any,
    row: dict[str, Any],
    dirty: list[list[dict[str, Any]]],
) -> None:
    """Append a row dict to a list bucket and track multi-item buckets."""
    bucket = mapping.get(key)
    if bucket is None:
        mapping[key] = [row]
    else:
        if len(bucket) == 1:
            dirty.append(bucket)
        bucket.append(row)


def _add_customer_tokens(
    token_map: dict[str, list[str]],
    seen: set[str],
    raw_text: Any,
    cid: str,
) -> None:
    """Index all folded name tokens from raw_text for a single customer."""
    if not raw_text:
        return
    for tok in _tokenize_folded_tokens(raw_text):
        if tok not in seen:
            seen.add(tok)
            bucket = token_map.get(tok)
            if bucket is None:
                token_map[tok] = [cid]
            else:
                bucket.append(cid)


@dataclass
class ScreeningDataset:
    """Loaded screening tables with O(1) link indices and lazy linkless indices."""

    data_dir: str
    customers: tuple[dict[str, Any], ...]
    accounts: tuple[dict[str, Any], ...]
    onboarding: tuple[dict[str, Any], ...]
    kyc_checks: tuple[dict[str, Any], ...]
    notes: tuple[dict[str, Any], ...]
    alerts: tuple[dict[str, Any], ...]
    hits: tuple[dict[str, Any], ...]
    search_results: tuple[dict[str, Any], ...]
    name_variants_raw: dict[str, Any]
    name_variant_groups: tuple[frozenset[str], ...]
    company_legal_forms: frozenset[str]

    customers_by_id: dict[str, dict[str, Any]]
    accounts_by_id: dict[str, list[dict[str, Any]]]
    account_holders: dict[str, list[str]]
    accounts_by_customer: dict[str, list[dict[str, Any]]]
    login_to_customers: dict[str, list[str]]
    email_to_customers: dict[str, list[str]]
    onboarding_by_pair: dict[tuple[str, str], list[str]]
    onboarding_by_customer: dict[str, list[str]]
    applicant_to_customers: dict[str, list[str]]
    kyc_by_applicant: dict[str, list[dict[str, Any]]]
    notes_by_email: dict[str, list[dict[str, Any]]]
    alerts_by_id: dict[str, dict[str, Any]]
    hits_by_id: dict[str, dict[str, Any]]
    hits_by_alert: dict[str, list[dict[str, Any]]]
    sr_by_query_key: dict[tuple[str, ...], list[dict[str, Any]]]

    @cached_property
    def _birth_year_index(
        self,
    ) -> tuple[dict[int, tuple[str, ...]], tuple[str, ...]]:
        """Build birth-year inverted index across CRM DOBs and approved KYC DOBs."""
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            approved_years_by_applicant: dict[str, list[int]] = {}
            for applicant_id, checks in self.kyc_by_applicant.items():
                for check in checks:
                    res = check.get("result")
                    if res == "approved" or (
                        isinstance(res, str) and res.strip().lower() == "approved"
                    ):
                        year = extract_year_hint(check.get("verified_dob"))
                        if year is not None:
                            lst = approved_years_by_applicant.get(applicant_id)
                            if lst is None:
                                approved_years_by_applicant[applicant_id] = [year]
                            elif year not in lst:
                                lst.append(year)

            by_year_mut: dict[int, list[str]] = {}
            without_year: list[str] = []
            for customer in self.customers:
                cid = customer["record_id"]
                years: set[int] | None = None
                crm_year = extract_year_hint(customer.get("dob"))
                if crm_year is not None:
                    years = {crm_year}
                for applicant_id in self.onboarding_by_customer.get(cid, ()):
                    kyc_years = approved_years_by_applicant.get(applicant_id)
                    if kyc_years:
                        if years is None:
                            years = set(kyc_years)
                        else:
                            years.update(kyc_years)
                if years:
                    for year in years:
                        bucket = by_year_mut.get(year)
                        if bucket is None:
                            by_year_mut[year] = [cid]
                        else:
                            bucket.append(cid)
                else:
                    without_year.append(cid)

            by_year = {
                year: tuple(sorted(cids)) for year, cids in by_year_mut.items()
            }
            return by_year, tuple(sorted(without_year))
        finally:
            if gc_was_enabled:
                gc.enable()

    @cached_property
    def customers_by_birth_year(self) -> dict[int, tuple[str, ...]]:
        """Map birth year to sorted customer record IDs (CRM + approved KYC)."""
        return self._birth_year_index[0]

    @cached_property
    def customers_without_birth_year(self) -> tuple[str, ...]:
        """Sorted customer IDs lacking a birth year in both CRM and approved KYC."""
        return self._birth_year_index[1]

    @cached_property
    def customers_by_name_token(self) -> dict[str, tuple[str, ...]]:
        """Map normalized name/login token to sorted unique customer record IDs."""
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            token_map: dict[str, list[str]] = {}
            for customer in self.customers:
                cid = customer["record_id"]
                seen: set[str] = set()
                for raw_name in (
                    customer.get("full_name"),
                    customer.get("preferred_name"),
                    customer.get("login_name"),
                ):
                    _add_customer_tokens(token_map, seen, raw_name, cid)
                for account in self.accounts_by_customer.get(cid, ()):
                    _add_customer_tokens(
                        token_map, seen, account.get("login_handle"), cid
                    )
            return {tok: tuple(sorted(cids)) for tok, cids in token_map.items()}
        finally:
            if gc_was_enabled:
                gc.enable()

    def get_account_holders(self, account_id: str | None) -> tuple[str, ...]:
        """Return sorted customer IDs linked to an account (all joint holders)."""
        if not account_id:
            return ()
        return tuple(self.account_holders.get(account_id, ()))

    def get_customers_by_login(self, login_handle: str | None) -> tuple[str, ...]:
        """Return sorted customer IDs matching a normalized login handle."""
        key = normalize_handle(login_handle)
        if not key:
            return ()
        return tuple(self.login_to_customers.get(key, ()))

    def get_customers_by_email(self, email: str | None) -> tuple[str, ...]:
        """Return sorted customer IDs matching a normalized email address."""
        key = normalize_email(email)
        if not key:
            return ()
        return tuple(self.email_to_customers.get(key, ()))

    def get_kyc_applicants(self, account_id: str, customer_id: str) -> tuple[str, ...]:
        """Return sorted KYC applicant IDs for an (account_id, customer_id) pair."""
        if not account_id or not customer_id:
            return ()
        return tuple(self.onboarding_by_pair.get((account_id, customer_id), ()))

    def get_customer_kyc_applicants(self, customer_id: str) -> tuple[str, ...]:
        """Return sorted KYC applicant IDs associated with a customer ID."""
        if not customer_id:
            return ()
        return tuple(self.onboarding_by_customer.get(customer_id, ()))

    def get_kyc_checks_for_applicant(
        self, applicant_id: str
    ) -> tuple[dict[str, Any], ...]:
        """Return deterministically sorted KYC checks for an applicant ID."""
        if not applicant_id:
            return ()
        return tuple(self.kyc_by_applicant.get(applicant_id, ()))

    def get_kyc_checks_for_customer(
        self, customer_id: str
    ) -> tuple[dict[str, Any], ...]:
        """Return all KYC checks linked to a customer across onboarding records."""
        if not customer_id:
            return ()
        checks_by_id: dict[str, dict[str, Any]] = {}
        for applicant_id in self.onboarding_by_customer.get(customer_id, ()):
            for check in self.kyc_by_applicant.get(applicant_id, ()):
                checks_by_id[check["check_id"]] = check
        return tuple(
            sorted(
                checks_by_id.values(),
                key=lambda r: (r["check_id"], r.get("checked_at") or ""),
            )
        )

    def get_notes_for_email(self, email: str | None) -> tuple[dict[str, Any], ...]:
        """Return deterministically sorted notes for a normalized email."""
        key = normalize_email(email)
        if not key:
            return ()
        return tuple(self.notes_by_email.get(key, ()))

    def get_hits_for_alert(self, alert_id: str) -> tuple[dict[str, Any], ...]:
        """Return deterministically sorted hit records belonging to an alert."""
        if not alert_id:
            return ()
        return tuple(self.hits_by_alert.get(alert_id, ()))

    def get_search_results(
        self, name_or_query: str | None
    ) -> tuple[dict[str, Any], ...]:
        """Return search_results matching the provider query key for a name."""
        key = provider_query_key(name_or_query)
        if not key:
            return ()
        return tuple(self.sr_by_query_key.get(key, ()))

    def candidates_for_birth_year(self, year: int | None) -> tuple[str, ...]:
        """Return candidate customer IDs for a birth year plus unknown-DOB records."""
        if year is None:
            return tuple(sorted(self.customers_by_id))
        bucket = self.customers_by_birth_year.get(year, ())
        if not self.customers_without_birth_year:
            return bucket
        if not bucket:
            return self.customers_without_birth_year
        return tuple(sorted(set(bucket).union(self.customers_without_birth_year)))

    def candidates_for_name_tokens(self, text: str | None) -> tuple[str, ...]:
        """Return sorted union of customer IDs matching any normalized name token."""
        tokens = extract_index_tokens(text)
        if not tokens:
            return ()
        matched: set[str] = set()
        token_index = self.customers_by_name_token
        for tok in tokens:
            bucket = token_index.get(tok)
            if bucket:
                matched.update(bucket)
        return tuple(sorted(matched))


def _has_required_ids(row: Any, required_keys: tuple[str, ...]) -> bool:
    """Return True when row is a dict containing non-empty string values for keys."""
    if type(row) is not dict:
        return False
    for key in required_keys:
        val = row.get(key)
        if not isinstance(val, str) or not val.strip():
            return False
    return True


def _load_jsonl_lines(
    path: str, required_keys: tuple[str, ...], *, strict: bool
) -> list[dict[str, Any]]:
    """Fallback line-by-line JSONL loader preserving exact line numbers."""
    rows: list[dict[str, Any]] = []
    loads = json.loads
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = loads(stripped)
            except json.JSONDecodeError as exc:
                if strict:
                    raise ValueError(
                        f"Malformed JSON at {path}:{line_no}: {exc}"
                    ) from exc
                sys.stderr.write(
                    f"WARNING: skipping malformed JSON at {path}:{line_no}: {exc}\n"
                )
                continue
            if not isinstance(record, dict):
                if strict:
                    raise ValueError(
                        f"Expected JSON object at {path}:{line_no}, got {type(record).__name__}"
                    )
                sys.stderr.write(
                    f"WARNING: skipping non-object row at {path}:{line_no}\n"
                )
                continue
            if not _has_required_ids(record, required_keys):
                if strict:
                    raise ValueError(
                        f"Missing required ID field(s) {required_keys} at {path}:{line_no}"
                    )
                sys.stderr.write(
                    f"WARNING: skipping row missing required IDs at {path}:{line_no}\n"
                )
                continue
            rows.append(record)
    return rows


def _load_jsonl_table(
    path: str, required_keys: tuple[str, ...], *, strict: bool
) -> list[dict[str, Any]]:
    """Fast-path bulk JSONL parser with line-by-line fallback for edge cases."""
    with open(path, "rb") as handle:
        raw = handle.read().strip()
    if not raw:
        return []
    try:
        rows = json.loads(b"[" + raw.replace(b"\n", b",") + b"]")
    except json.JSONDecodeError:
        return _load_jsonl_lines(path, required_keys, strict=strict)

    if isinstance(rows, list) and all(_has_required_ids(r, required_keys) for r in rows):
        return rows
    return _load_jsonl_lines(path, required_keys, strict=strict)


def _load_name_variants(
    path: str,
) -> tuple[dict[str, Any], tuple[frozenset[str], ...], frozenset[str]]:
    """Load and validate name_variants.json."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    raw_groups = data.get("groups")
    raw_forms = data.get("company_legal_forms")
    if not isinstance(raw_groups, list) or not isinstance(raw_forms, list):
        raise ValueError(f"Invalid name_variants schema in {path}")

    groups = tuple(
        frozenset(
            str(item).strip().lower()
            for item in group
            if isinstance(item, str) and item.strip()
        )
        for group in raw_groups
        if isinstance(group, list)
    )
    legal_forms = frozenset(
        str(item).strip().lower()
        for item in raw_forms
        if isinstance(item, str) and item.strip()
    )
    return data, groups, legal_forms


def _validate_data_dir(data_dir: str) -> None:
    """Ensure data_dir and every required input file exist."""
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"Data directory does not exist: {data_dir}")
    for filename in REQUIRED_DATA_FILES:
        file_path = os.path.join(data_dir, filename)
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Required input file not found: {file_path}")


def _sort_dirty_row_buckets(
    groups: tuple[tuple[list[list[dict[str, Any]]], Callable[[dict[str, Any]], Any]], ...]
) -> None:
    """Deterministically sort only multi-element row buckets."""
    for bucket_list, key_fn in groups:
        for bucket in bucket_list:
            bucket.sort(key=key_fn)


def load_dataset(data_dir: str, *, strict: bool = False) -> ScreeningDataset:
    """Load all screening tables from data_dir and build O(1) indices."""
    _validate_data_dir(data_dir)
    join = os.path.join
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        customers = _load_jsonl_table(
            join(data_dir, "customers.jsonl"), ("record_id",), strict=strict
        )
        accounts = _load_jsonl_table(
            join(data_dir, "accounts.jsonl"), ("account_id", "customer_id"), strict=strict
        )
        onboarding = _load_jsonl_table(
            join(data_dir, "onboarding.jsonl"),
            ("account_id", "customer_id", "kyc_applicant_id"),
            strict=strict,
        )
        kyc_checks = _load_jsonl_table(
            join(data_dir, "kyc_checks.jsonl"),
            ("check_id", "applicant_id"),
            strict=strict,
        )
        notes = _load_jsonl_table(
            join(data_dir, "notes.jsonl"), ("note_id",), strict=strict
        )
        alerts = _load_jsonl_table(
            join(data_dir, "alerts.jsonl"), ("alert_id",), strict=strict
        )
        hits = _load_jsonl_table(
            join(data_dir, "hits.jsonl"), ("hit_id", "alert_id"), strict=strict
        )
        search_results = _load_jsonl_table(
            join(data_dir, "search_results.jsonl"), ("result_id",), strict=strict
        )
        name_variants_raw, name_variant_groups, company_legal_forms = _load_name_variants(
            join(data_dir, "name_variants.json")
        )

        dirty_str_lists: list[list[str]] = []

        customers_by_id: dict[str, dict[str, Any]] = {}
        email_to_customers: dict[str, list[str]] = {}
        login_to_customers: dict[str, list[str]] = {}
        for customer in customers:
            cid = customer["record_id"]
            customers_by_id[cid] = customer
            email_key = normalize_email(customer.get("email"))
            if email_key:
                _append_unique_str(email_to_customers, email_key, cid, dirty_str_lists)
            login_key = normalize_handle(customer.get("login_name"))
            if login_key:
                _append_unique_str(login_to_customers, login_key, cid, dirty_str_lists)

        accounts_by_id: dict[str, list[dict[str, Any]]] = {}
        account_holders: dict[str, list[str]] = {}
        accounts_by_customer: dict[str, list[dict[str, Any]]] = {}
        dirty_acc_id: list[list[dict[str, Any]]] = []
        dirty_acc_cust: list[list[dict[str, Any]]] = []
        for account in accounts:
            aid = account["account_id"]
            cid = account["customer_id"]
            _append_bucket(accounts_by_id, aid, account, dirty_acc_id)
            _append_unique_str(account_holders, aid, cid, dirty_str_lists)
            _append_bucket(accounts_by_customer, cid, account, dirty_acc_cust)
            handle_key = normalize_handle(account.get("login_handle"))
            if handle_key:
                _append_unique_str(login_to_customers, handle_key, cid, dirty_str_lists)

        onboarding_by_pair: dict[tuple[str, str], list[str]] = {}
        onboarding_by_customer: dict[str, list[str]] = {}
        applicant_to_customers: dict[str, list[str]] = {}
        for row in onboarding:
            aid = row["account_id"]
            cid = row["customer_id"]
            app_id = row["kyc_applicant_id"]
            _append_unique_str(onboarding_by_pair, (aid, cid), app_id, dirty_str_lists)
            _append_unique_str(onboarding_by_customer, cid, app_id, dirty_str_lists)
            _append_unique_str(applicant_to_customers, app_id, cid, dirty_str_lists)

        kyc_by_applicant: dict[str, list[dict[str, Any]]] = {}
        dirty_kyc: list[list[dict[str, Any]]] = []
        for check in kyc_checks:
            _append_bucket(kyc_by_applicant, check["applicant_id"], check, dirty_kyc)

        notes_by_email: dict[str, list[dict[str, Any]]] = {}
        dirty_notes: list[list[dict[str, Any]]] = []
        for note in notes:
            email_key = normalize_email(note.get("requester_email"))
            if email_key:
                _append_bucket(notes_by_email, email_key, note, dirty_notes)

        alerts_by_id = {alert["alert_id"]: alert for alert in alerts}
        hits_by_id = {hit["hit_id"]: hit for hit in hits}
        hits_by_alert: dict[str, list[dict[str, Any]]] = {}
        dirty_hits: list[list[dict[str, Any]]] = []
        for hit in hits:
            _append_bucket(hits_by_alert, hit["alert_id"], hit, dirty_hits)

        sr_by_query_key: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        dirty_sr: list[list[dict[str, Any]]] = []
        for sr in search_results:
            qk = provider_query_key(sr.get("query"))
            _append_bucket(sr_by_query_key, qk, sr, dirty_sr)

        for str_list in dirty_str_lists:
            str_list.sort()
        _sort_dirty_row_buckets(
            (
                (dirty_acc_id, lambda r: (r["customer_id"], r["account_id"], r.get("opened_at") or "")),
                (dirty_acc_cust, lambda r: (r["account_id"], r.get("opened_at") or "")),
                (dirty_kyc, lambda r: (r["check_id"], r.get("checked_at") or "")),
                (dirty_notes, lambda r: (r["note_id"], r.get("created_at") or "")),
                (dirty_hits, lambda r: r["hit_id"]),
                (dirty_sr, lambda r: r["result_id"]),
            )
        )

        alerts.sort(key=lambda r: r["alert_id"])
        hits.sort(key=lambda r: (r["hit_id"], r["alert_id"]))
        customers.sort(key=lambda r: r["record_id"])
        accounts.sort(key=lambda r: (r["account_id"], r["customer_id"], r.get("opened_at") or ""))
        onboarding.sort(
            key=lambda r: (r["account_id"], r["customer_id"], r["kyc_applicant_id"])
        )
        kyc_checks.sort(key=lambda r: (r["check_id"], r.get("checked_at") or ""))
        notes.sort(key=lambda r: (r["note_id"], r.get("created_at") or ""))
        search_results.sort(key=lambda r: r["result_id"])

        return ScreeningDataset(
            data_dir=os.path.abspath(data_dir),
            customers=tuple(customers),
            accounts=tuple(accounts),
            onboarding=tuple(onboarding),
            kyc_checks=tuple(kyc_checks),
            notes=tuple(notes),
            alerts=tuple(alerts),
            hits=tuple(hits),
            search_results=tuple(search_results),
            name_variants_raw=name_variants_raw,
            name_variant_groups=name_variant_groups,
            company_legal_forms=company_legal_forms,
            customers_by_id=customers_by_id,
            accounts_by_id=accounts_by_id,
            account_holders=account_holders,
            accounts_by_customer=accounts_by_customer,
            login_to_customers=login_to_customers,
            email_to_customers=email_to_customers,
            onboarding_by_pair=onboarding_by_pair,
            onboarding_by_customer=onboarding_by_customer,
            applicant_to_customers=applicant_to_customers,
            kyc_by_applicant=kyc_by_applicant,
            notes_by_email=notes_by_email,
            alerts_by_id=alerts_by_id,
            hits_by_id=hits_by_id,
            hits_by_alert=hits_by_alert,
            sr_by_query_key=sr_by_query_key,
        )
    finally:
        if gc_was_enabled:
            gc.enable()
