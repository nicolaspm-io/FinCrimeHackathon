"""Customer identity graph (Union-Find), link/linkless resolution, and KYC priority."""
from __future__ import annotations

from collections import defaultdict
import datetime as _dt
from functools import lru_cache
from typing import Any, Iterable

try:
    from . import countries, dates, names
    from .contracts import (
        CandidatePerson,
        ENTITY_INDIVIDUAL,
        Fact,
        FactStatus,
        LLMFallback,
        ParsedDate,
        VerifiedIdentity,
    )
    from .customer_notes import ParsedKycNote, extract_doc_number, parse_customer_notes, parse_kyc_note
    from .data import (
        ScreeningDataset,
        extract_year_hint,
        normalize_email,
        normalize_phone_digits,
    )
except ImportError:
    import countries
    import dates
    import names
    from contracts import (
        CandidatePerson,
        ENTITY_INDIVIDUAL,
        Fact,
        FactStatus,
        LLMFallback,
        ParsedDate,
        VerifiedIdentity,
    )
    from customer_notes import ParsedKycNote, extract_doc_number, parse_customer_notes, parse_kyc_note
    from data import (
        ScreeningDataset,
        extract_year_hint,
        normalize_email,
        normalize_phone_digits,
    )


def _parse_bank_date_fast(raw: Any) -> ParsedDate | None:
    """Parse a bank-internal date using an ISO YYYY-MM-DD fast path."""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if (
            len(text) == 10
            and text[4] == "-"
            and text[7] == "-"
            and text[:4].isascii()
            and text[:4].isdigit()
        ):
            try:
                dt = _dt.date.fromisoformat(text)
                return ParsedDate(dt.year, dt.month, dt.day, text)
            except ValueError:
                pass
        return dates.parse_date(text, dates.BANK)
    return dates.parse_date(raw, dates.BANK)


@lru_cache(maxsize=8192)
def _fold_city_tokens_cached(text: str) -> tuple[str, ...]:
    """Cached folded city/address token tuple."""
    return tuple(names.normalize_tokens(text, collapse_acronyms=False))


def _fold_city_tokens(value: Any) -> tuple[str, ...]:
    """Normalize a city or address into folded alphanumeric word tokens."""
    if not value:
        return ()
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return ()
    return _fold_city_tokens_cached(text)


def _city_matches(
    screened_tokens: tuple[str, ...], rec_city: Any, rec_addr: Any
) -> bool:
    """Return True when screened city matches record city or appears in address."""
    if not screened_tokens:
        return True
    city_tokens = _fold_city_tokens(rec_city)
    addr_tokens = _fold_city_tokens(rec_addr)
    if not city_tokens and not addr_tokens:
        return True
    if city_tokens == screened_tokens:
        return True
    joined_city = " ".join(city_tokens)
    joined_screened = " ".join(screened_tokens)
    if joined_city and joined_screened == joined_city:
        return True
    if not addr_tokens:
        return False
    n = len(screened_tokens)
    for i in range(len(addr_tokens) - n + 1):
        if addr_tokens[i : i + n] == screened_tokens:
            return True
    return False


@lru_cache(maxsize=8192)
def _normalize_country_str(text: str) -> str | None:
    """Cached canonical country lookup for non-empty strings."""
    return countries.country_from_location(text) or countries.normalize_country(text)


def _normalize_country_field(value: Any) -> str | None:
    """Normalize a CRM/KYC country field cleanly."""
    if not value:
        return None
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return None
    return _normalize_country_str(text)


def _checked_at_sort_key(raw: str) -> tuple[int, int, int, str]:
    """Chronological descending-ready tuple for approved KYC check timestamps."""
    parsed = _parse_bank_date_fast(raw)
    if parsed is not None:
        return (parsed.year, parsed.month or 0, parsed.day or 0, raw)
    return (0, 0, 0, raw)


class CustomerGraph:
    """Global Union-Find clustering customer records of the same person."""

    def __init__(self, dataset: ScreeningDataset) -> None:
        self.dataset = dataset
        self._parent: dict[str, str] = {
            cid: cid for cid in dataset.customers_by_id
        }
        self._clusters_cache: dict[str, tuple[str, ...]] | None = None
        self._doc_to_records: dict[str, list[str]] = defaultdict(list)

        phone_dob_buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
        phone_distinct_dobs: dict[str, set[str]] = defaultdict(set)
        approved_doc_applicants: dict[str, set[str]] = defaultdict(set)
        approved_full_dobs_by_customer: dict[str, set[str]] = defaultdict(set)

        for check in dataset.kyc_checks:
            res = check.get("result")
            if isinstance(res, str) and res.strip().lower() == "approved":
                app_id = check["applicant_id"]
                linked_cids = dataset.applicant_to_customers.get(app_id, ())
                kd = _parse_bank_date_fast(check.get("verified_dob"))
                if kd is not None and kd.is_full:
                    iso = kd.iso_str
                    for cid in linked_cids:
                        approved_full_dobs_by_customer[cid].add(iso)
                doc_num = check.get("document_number")
                if isinstance(doc_num, str):
                    doc_key = doc_num.strip()
                    if doc_key:
                        approved_doc_applicants[doc_key].add(app_id)
                        for cid in linked_cids:
                            if cid not in self._doc_to_records[doc_key]:
                                self._doc_to_records[doc_key].append(cid)

        for cid, customer in dataset.customers_by_id.items():
            phones: set[str] = set()
            cust_phone = normalize_phone_digits(customer.get("phone"))
            if cust_phone:
                phones.add(cust_phone)
            for acc in dataset.accounts_by_customer.get(cid, ()):
                acc_phone = normalize_phone_digits(acc.get("phone"))
                if acc_phone:
                    phones.add(acc_phone)
            if not phones:
                continue

            full_dobs: set[str] = set(approved_full_dobs_by_customer.get(cid, ()))
            crm_dob = _parse_bank_date_fast(customer.get("dob"))
            if crm_dob is not None and crm_dob.is_full:
                full_dobs.add(crm_dob.iso_str)

            for phone in phones:
                for dob_iso in full_dobs:
                    phone_dob_buckets[(phone, dob_iso)].append(cid)
                    phone_distinct_dobs[phone].add(dob_iso)

        self._phone_dob_cluster_count = 0
        for cids in phone_dob_buckets.values():
            if len(cids) > 1:
                self._phone_dob_cluster_count += 1
                first = cids[0]
                for other in cids[1:]:
                    self.union(first, other)

        self._shared_doc_count = 0
        for doc_key, cids in self._doc_to_records.items():
            if len(approved_doc_applicants[doc_key]) > 1:
                self._shared_doc_count += 1
            if len(cids) > 1:
                first = cids[0]
                for other in cids[1:]:
                    self.union(first, other)

        self._family_shared_phone_count = sum(
            1 for dobs in phone_distinct_dobs.values() if len(dobs) > 1
        )

    def find(self, cid: str) -> str:
        """Find deterministic canonical root (lexicographically smallest ID)."""
        if cid not in self._parent:
            return cid
        root = cid
        while self._parent[root] != root:
            root = self._parent[root]
        curr = cid
        while self._parent[curr] != root:
            nxt = self._parent[curr]
            self._parent[curr] = root
            curr = nxt
        return root

    def union(self, a: str, b: str) -> str:
        """Merge two record clusters deterministically."""
        if a not in self._parent or b not in self._parent:
            return self.find(a)
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return ra
        root, child = (ra, rb) if ra < rb else (rb, ra)
        self._parent[child] = root
        self._clusters_cache = None
        return root

    def link_approved_document(self, cid: str, doc_number: str | None) -> None:
        """Register an approved document number extracted lazily from KYC notes."""
        if not doc_number or cid not in self._parent:
            return
        doc_key = doc_number.strip()
        if not doc_key:
            return
        existing = self._doc_to_records[doc_key]
        if cid not in existing:
            existing.append(cid)
        if len(existing) > 1:
            self.union(existing[0], cid)

    def _ensure_clusters_cache(self) -> dict[str, tuple[str, ...]]:
        """Build cached mapping from cluster root to sorted member IDs."""
        if self._clusters_cache is None:
            grouped: dict[str, list[str]] = defaultdict(list)
            for cid in self._parent:
                grouped[self.find(cid)].append(cid)
            self._clusters_cache = {
                root: tuple(sorted(members)) for root, members in grouped.items()
            }
        return self._clusters_cache

    def cluster_of(self, cid: str) -> tuple[str, ...]:
        """Return sorted tuple of all record_ids belonging to cid's person."""
        if cid not in self._parent:
            return (cid,)
        root = self.find(cid)
        return self._ensure_clusters_cache()[root]

    def clusters_for_records(
        self, cids: Iterable[str]
    ) -> tuple[tuple[str, ...], ...]:
        """Collapse an iterable of record_ids into deduplicated sorted clusters."""
        seen_roots: dict[str, tuple[str, ...]] = {}
        for cid in cids:
            root = self.find(cid)
            if root not in seen_roots:
                seen_roots[root] = self.cluster_of(cid)
        return tuple(seen_roots[r] for r in sorted(seen_roots))

    def stats(self) -> dict[str, int]:
        """Return summary cluster statistics for auditing and tests."""
        return {
            "total_records": len(self._parent),
            "phone_dob_clusters": self._phone_dob_cluster_count,
            "shared_doc_clusters": self._shared_doc_count,
            "family_shared_phones": self._family_shared_phone_count,
        }


class CustomerResolver:
    """Resolve alerts to deduplicated CandidatePerson profiles under POLICY 2-4."""

    def __init__(
        self,
        dataset: ScreeningDataset,
        *,
        llm_fallback: LLMFallback | None = None,
    ) -> None:
        self.dataset = dataset
        self.llm_fallback = llm_fallback
        self.variants = names.build_variants(dataset.name_variants_raw)
        self.graph = CustomerGraph(dataset)
        self._link_note_documents()
        self._email_notes_cache: dict[
            str, tuple[tuple[ParsedKycNote, ...], tuple[Fact, ...], tuple[Fact, ...]]
        ] = {}

    def _link_note_documents(self) -> None:
        """Union records whose approved KYC notes carry the same document number (POLICY 3).

        Done once, over every note, before any alert is resolved, so the identity graph is a
        pure function of the data folder and never depends on which alerts were resolved first.
        Only a cheap regex pre-filter runs over all notes; matching notes are fully parsed.
        """
        for email in sorted(self.dataset.notes_by_email):
            for note in self.dataset.get_notes_for_email(email):
                if str(note.get("channel") or "").strip().lower() != "kyc":
                    continue
                text = str(note.get("text") or "")
                if not extract_doc_number(text):
                    continue
                parsed = parse_kyc_note(note)
                if parsed.status == "approved" and parsed.doc_number:
                    for cid in self.dataset.get_customers_by_email(email):
                        self.graph.link_approved_document(cid, parsed.doc_number)

    def _get_parsed_notes_for_email(
        self, raw_email: str | None
    ) -> tuple[tuple[ParsedKycNote, ...], tuple[Fact, ...], tuple[Fact, ...]]:
        """Lazily parse and cache KYC + residence notes for one email address."""
        key = normalize_email(raw_email)
        if not key:
            return (), (), ()
        cached = self._email_notes_cache.get(key)
        if cached is not None:
            return cached
        raw_notes = self.dataset.get_notes_for_email(key)
        if not raw_notes:
            result: tuple[
                tuple[ParsedKycNote, ...], tuple[Fact, ...], tuple[Fact, ...]
            ] = ((), (), ())
        else:
            # Note-derived document links are built once in _link_note_documents, before any
            # resolution; linking here as well would make clusters depend on alert order.
            result = parse_customer_notes(raw_notes, llm_fallback=self.llm_fallback)
        self._email_notes_cache[key] = result
        return result

    def _approved_checks_for(self, cid: str) -> list[dict[str, Any]]:
        """Return approved kyc_checks for a single customer."""
        return [
            chk
            for chk in self.dataset.get_kyc_checks_for_customer(cid)
            if isinstance(chk.get("result"), str)
            and chk["result"].strip().lower() == "approved"
        ]

    def _record_fits_screened(
        self, cid: str, screened: dict[str, Any]
    ) -> bool:
        """Check whether a customer record fits the screened attributes (POLICY 2)."""
        rec = self.dataset.customers_by_id.get(cid)
        if rec is None:
            return False

        sc_et = screened.get("entity_type")
        rec_et = rec.get("entity_type")
        if sc_et and rec_et and str(sc_et).strip().lower() != str(rec_et).strip().lower():
            return False

        approved_checks: list[dict[str, Any]] | None = None

        sc_country = _normalize_country_field(screened.get("country"))
        if sc_country:
            rec_countries = {
                c
                for c in (
                    _normalize_country_field(rec.get("residence_country")),
                    _normalize_country_field(rec.get("nationality")),
                )
                if c
            }
            if not rec_countries:
                approved_checks = self._approved_checks_for(cid)
                for chk in approved_checks:
                    dc = _normalize_country_field(chk.get("document_country"))
                    if dc:
                        rec_countries.add(dc)
            if rec_countries and sc_country not in rec_countries:
                return False

        sc_city_tokens = _fold_city_tokens(screened.get("city"))
        if sc_city_tokens and not _city_matches(
            sc_city_tokens, rec.get("city"), rec.get("address")
        ):
            return False

        sc_dob = _parse_bank_date_fast(screened.get("dob"))
        lazy_kyc_notes: tuple[ParsedKycNote, ...] | None = None
        if sc_dob is not None:
            rec_dobs: list[ParsedDate] = []
            crm_dob = _parse_bank_date_fast(rec.get("dob"))
            if crm_dob is not None:
                rec_dobs.append(crm_dob)
            if not rec_dobs or dates.compare_dates(crm_dob, sc_dob) != dates.AGREE:
                if approved_checks is None:
                    approved_checks = self._approved_checks_for(cid)
                for chk in approved_checks:
                    kd = _parse_bank_date_fast(chk.get("verified_dob"))
                    if kd is not None:
                        rec_dobs.append(kd)
            if not rec_dobs:
                lazy_kyc_notes = self._get_parsed_notes_for_email(rec.get("email"))[0]
                for kn in lazy_kyc_notes:
                    if kn.status == "approved" and kn.verified_dob is not None:
                        rec_dobs.append(kn.verified_dob)
            if rec_dobs and not any(
                dates.compare_dates(d, sc_dob) == dates.AGREE for d in rec_dobs
            ):
                return False

        sc_name = screened.get("name")
        et_for_name = sc_et or rec_et or ENTITY_INDIVIDUAL
        if names.real_part_count(sc_name, et_for_name, self.variants) > 0:
            crm_names: list[str] = []
            for raw_n in (
                rec.get("full_name"),
                rec.get("preferred_name"),
                rec.get("login_name"),
            ):
                if isinstance(raw_n, str) and raw_n.strip():
                    crm_names.append(raw_n)
            for acc in self.dataset.accounts_by_customer.get(cid, ()):
                lh = acc.get("login_handle")
                if isinstance(lh, str) and lh.strip():
                    crm_names.append(lh)
            if any(
                names.names_agree(sc_name, cn, et_for_name, self.variants)
                for cn in crm_names
            ):
                return True

            if approved_checks is None:
                approved_checks = self._approved_checks_for(cid)
            kyc_names = [
                chk["verified_name"]
                for chk in approved_checks
                if isinstance(chk.get("verified_name"), str)
                and chk["verified_name"].strip()
            ]
            if any(
                names.names_agree(sc_name, kn, et_for_name, self.variants)
                for kn in kyc_names
            ):
                return True

            if not crm_names and not kyc_names:
                if lazy_kyc_notes is None:
                    lazy_kyc_notes = self._get_parsed_notes_for_email(rec.get("email"))[0]
                note_names = [
                    kn.verified_name
                    for kn in lazy_kyc_notes
                    if kn.status == "approved" and kn.verified_name
                ]
                if not note_names:
                    return True
                return any(
                    names.names_agree(sc_name, nn, et_for_name, self.variants)
                    for nn in note_names
                )
            return False

        return True

    def _cluster_fits_screened(
        self, cluster: tuple[str, ...], screened: dict[str, Any]
    ) -> bool:
        """Return True if any record in the cluster satisfies screened criteria."""
        return any(self._record_fits_screened(cid, screened) for cid in cluster)

    def _linkless_candidate_pool(self, screened: dict[str, Any]) -> tuple[str, ...]:
        """Fetch indexed candidate record_ids for a linkless alert."""
        sc_dob = _parse_bank_date_fast(screened.get("dob"))
        year = sc_dob.year if sc_dob is not None else extract_year_hint(screened.get("dob"))
        if year is not None:
            return self.dataset.candidates_for_birth_year(year)

        token_pool = self.dataset.candidates_for_name_tokens(screened.get("name"))
        if token_pool:
            return token_pool

        sc_et = screened.get("entity_type")
        if sc_et:
            et_low = str(sc_et).strip().lower()
            return tuple(
                cid
                for cid, rec in sorted(self.dataset.customers_by_id.items())
                if not rec.get("entity_type")
                or str(rec["entity_type"]).strip().lower() == et_low
            )
        return tuple(sorted(self.dataset.customers_by_id))

    def resolve_alert(self, alert: dict[str, Any]) -> tuple[CandidatePerson, ...]:
        """Resolve an alert to matching CandidatePerson clusters (POLICY 2-4)."""
        screened = alert.get("customer") or {}
        linked_cids: tuple[str, ...] = ()
        has_link = False

        if alert.get("account_ref"):
            has_link = True
            linked_cids = self.dataset.get_account_holders(alert["account_ref"])
        elif alert.get("login_handle"):
            has_link = True
            linked_cids = self.dataset.get_customers_by_login(alert["login_handle"])
        elif alert.get("email"):
            has_link = True
            linked_cids = self.dataset.get_customers_by_email(alert["email"])

        if has_link:
            if not linked_cids:
                return ()
            clusters = self.graph.clusters_for_records(linked_cids)
            matching_clusters = tuple(
                cl for cl in clusters if self._cluster_fits_screened(cl, screened)
            )
            return tuple(self.build_candidate_person(cl) for cl in matching_clusters)

        pool = self._linkless_candidate_pool(screened)
        matched_cids = [
            cid for cid in pool if self._record_fits_screened(cid, screened)
        ]
        clusters = self.graph.clusters_for_records(matched_cids)
        return tuple(self.build_candidate_person(cl) for cl in clusters)

    def resolve_linkless_bruteforce(
        self, alert: dict[str, Any]
    ) -> tuple[CandidatePerson, ...]:
        """Exhaustive full-table reference scan for verifying 100% linkless recall."""
        screened = alert.get("customer") or {}
        matched_cids = [
            cid
            for cid in sorted(self.dataset.customers_by_id)
            if self._record_fits_screened(cid, screened)
        ]
        clusters = self.graph.clusters_for_records(matched_cids)
        return tuple(self.build_candidate_person(cl) for cl in clusters)

    def build_candidate_person(
        self, cluster_record_ids: tuple[str, ...]
    ) -> CandidatePerson:
        """Aggregate declared + verified + note facts for a person cluster."""
        record_ids = tuple(sorted(set(cluster_record_ids)))
        entity_type = ENTITY_INDIVIDUAL
        name_facts: list[Fact] = []
        dob_facts: list[Fact] = []
        country_facts: list[Fact] = []
        pending_clues: list[Fact] = []
        emails: list[str] = []

        approved_sources: list[dict[str, Any]] = []

        for cid in record_ids:
            rec = self.dataset.customers_by_id.get(cid)
            if rec is None:
                continue
            if rec.get("entity_type") and entity_type == ENTITY_INDIVIDUAL:
                entity_type = str(rec["entity_type"])

            for field_name in ("full_name", "preferred_name", "login_name"):
                raw_name = rec.get(field_name)
                if isinstance(raw_name, str) and raw_name.strip():
                    name_facts.append(
                        Fact(
                            field="name",
                            value=raw_name.strip(),
                            raw=raw_name,
                            status=FactStatus.VERIFIED,
                            subject="customer",
                            source_type="CUS",
                            source_id=cid,
                            location=field_name,
                            verified_state="declared",
                        )
                    )
            for acc in self.dataset.accounts_by_customer.get(cid, ()):
                lh = acc.get("login_handle")
                if isinstance(lh, str) and lh.strip():
                    name_facts.append(
                        Fact(
                            field="name",
                            value=lh.strip(),
                            raw=lh,
                            status=FactStatus.VERIFIED,
                            subject="customer",
                            source_type="CUS",
                            source_id=cid,
                            location="login_handle",
                            verified_state="declared",
                        )
                    )

            raw_dob = rec.get("dob")
            parsed_dob = _parse_bank_date_fast(raw_dob)
            if parsed_dob is not None:
                dob_facts.append(
                    Fact(
                        field="dob",
                        value=parsed_dob,
                        raw=str(raw_dob),
                        status=FactStatus.VERIFIED,
                        subject="customer",
                        source_type="CUS",
                        source_id=cid,
                        location="dob",
                        verified_state="declared",
                    )
                )

            for loc_field in ("residence_country", "nationality"):
                raw_c = rec.get(loc_field)
                norm_c = _normalize_country_field(raw_c)
                if norm_c:
                    country_facts.append(
                        Fact(
                            field="country",
                            value=norm_c,
                            raw=str(raw_c),
                            status=FactStatus.VERIFIED,
                            subject="customer",
                            source_type="CUS",
                            source_id=cid,
                            location=loc_field,
                            verified_state="declared",
                        )
                    )

            em = normalize_email(rec.get("email"))
            if em and em not in emails:
                emails.append(em)

            for chk in self.dataset.get_kyc_checks_for_customer(cid):
                res = chk.get("result")
                if not (isinstance(res, str) and res.strip().lower() == "approved"):
                    continue
                chk_id = chk["check_id"]
                checked_at = str(chk.get("checked_at") or "").strip()
                v_name = chk.get("verified_name")
                v_dob_raw = chk.get("verified_dob")
                v_dob = _parse_bank_date_fast(v_dob_raw)
                v_country = _normalize_country_field(chk.get("document_country"))
                doc_num = (
                    str(chk["document_number"]).strip()
                    if chk.get("document_number")
                    else None
                )
                approved_sources.append(
                    {
                        "source_type": "KYC",
                        "source_id": chk_id,
                        "checked_at": checked_at,
                        "name_fact": (
                            Fact(
                                field="name",
                                value=str(v_name).strip(),
                                raw=str(v_name),
                                status=FactStatus.VERIFIED,
                                subject="customer",
                                source_type="KYC",
                                source_id=chk_id,
                                location="verified_name",
                                date_str=checked_at,
                                verified_state="approved",
                            )
                            if isinstance(v_name, str) and v_name.strip()
                            else None
                        ),
                        "dob_fact": (
                            Fact(
                                field="dob",
                                value=v_dob,
                                raw=str(v_dob_raw),
                                status=FactStatus.VERIFIED,
                                subject="customer",
                                source_type="KYC",
                                source_id=chk_id,
                                location="verified_dob",
                                date_str=checked_at,
                                verified_state="approved",
                            )
                            if v_dob is not None
                            else None
                        ),
                        "country_fact": (
                            Fact(
                                field="country",
                                value=v_country,
                                raw=str(chk.get("document_country")),
                                status=FactStatus.VERIFIED,
                                subject="customer",
                                source_type="KYC",
                                source_id=chk_id,
                                location="document_country",
                                date_str=checked_at,
                                verified_state="approved",
                            )
                            if v_country
                            else None
                        ),
                        "doc_number": doc_num,
                    }
                )

        for em in sorted(emails):
            kyc_notes, res_facts, note_clues = self._get_parsed_notes_for_email(em)
            country_facts.extend(res_facts)
            pending_clues.extend(note_clues)
            for kn in kyc_notes:
                if kn.status != "approved":
                    continue
                approved_sources.append(
                    {
                        "source_type": "NOTE",
                        "source_id": kn.note_id,
                        "checked_at": kn.checked_at,
                        "name_fact": (
                            Fact(
                                field="name",
                                value=kn.verified_name,
                                raw=kn.raw_text,
                                status=FactStatus.VERIFIED,
                                subject="customer",
                                source_type="NOTE",
                                source_id=kn.note_id,
                                location="note_body",
                                date_str=kn.checked_at,
                                verified_state="approved",
                            )
                            if kn.verified_name
                            else None
                        ),
                        "dob_fact": (
                            Fact(
                                field="dob",
                                value=kn.verified_dob,
                                raw=kn.verified_dob_raw,
                                status=FactStatus.VERIFIED,
                                subject="customer",
                                source_type="NOTE",
                                source_id=kn.note_id,
                                location="note_body",
                                date_str=kn.checked_at,
                                verified_state="approved",
                            )
                            if kn.verified_dob is not None
                            else None
                        ),
                        "country_fact": (
                            Fact(
                                field="country",
                                value=kn.doc_country,
                                raw=kn.raw_text,
                                status=FactStatus.VERIFIED,
                                subject="customer",
                                source_type="NOTE",
                                source_id=kn.note_id,
                                location="note_body",
                                date_str=kn.checked_at,
                                verified_state="approved",
                            )
                            if kn.doc_country
                            else None
                        ),
                        "doc_number": kn.doc_number,
                    }
                )

        verified = self._select_verified_identity(
            approved_sources, entity_type, country_facts
        )

        return CandidatePerson(
            record_ids=record_ids,
            entity_type=entity_type,
            verified=verified,
            names=tuple(name_facts),
            dobs=tuple(dob_facts),
            countries=tuple(country_facts),
            pending_clues=tuple(pending_clues),
        )

    def _select_verified_identity(
        self,
        approved_sources: list[dict[str, Any]],
        entity_type: str,
        country_facts: list[Fact],
    ) -> VerifiedIdentity | None:
        """Select latest approved check/note and retain tied incompatible conflicts."""
        if not approved_sources:
            return None

        approved_sources.sort(
            key=lambda s: (
                _checked_at_sort_key(s["checked_at"]),
                s["source_id"],
            ),
            reverse=True,
        )
        top = approved_sources[0]
        top_date_key = _checked_at_sort_key(top["checked_at"])[:3]
        tied = [
            s
            for s in approved_sources
            if _checked_at_sort_key(s["checked_at"])[:3] == top_date_key
        ]

        conflict_facts: list[Fact] = []
        top_name = top["name_fact"]
        top_dob = top["dob_fact"]
        top_country = top["country_fact"]

        if top_country is not None:
            country_facts.append(top_country)

        for other in tied[1:]:
            o_name = other["name_fact"]
            if (
                top_name is not None
                and o_name is not None
                and not names.names_agree(
                    top_name.value, o_name.value, entity_type, self.variants
                )
            ):
                if top_name not in conflict_facts:
                    conflict_facts.append(top_name)
                conflict_facts.append(o_name)

            o_dob = other["dob_fact"]
            if (
                top_dob is not None
                and o_dob is not None
                and dates.compare_dates(top_dob.value, o_dob.value) == dates.CONTRADICT
            ):
                if top_dob not in conflict_facts:
                    conflict_facts.append(top_dob)
                conflict_facts.append(o_dob)

            o_country = other["country_fact"]
            if (
                top_country is not None
                and o_country is not None
                and top_country.value != o_country.value
            ):
                if top_country not in conflict_facts:
                    conflict_facts.append(top_country)
                conflict_facts.append(o_country)
                country_facts.append(o_country)

        # Fallback: if latest approved check lacked a field (e.g. name or DOB),
        # inherit from an earlier/tied approved source only if non-contradictory.
        eff_name = top_name
        eff_dob = top_dob
        eff_country = top_country
        eff_doc_num = top["doc_number"]
        for other in approved_sources[1:]:
            if eff_name is None and other["name_fact"] is not None:
                eff_name = other["name_fact"]
            if eff_dob is None and other["dob_fact"] is not None:
                eff_dob = other["dob_fact"]
            if eff_country is None and other["country_fact"] is not None:
                eff_country = other["country_fact"]
                country_facts.append(eff_country)
            if eff_doc_num is None and other["doc_number"] is not None:
                eff_doc_num = other["doc_number"]

        return VerifiedIdentity(
            name=eff_name,
            dob=eff_dob,
            doc_country=eff_country,
            doc_number=eff_doc_num,
            checked_at=top["checked_at"],
            source_id=top["source_id"],
            conflict_facts=tuple(conflict_facts),
        )
