"""Shared frozen domain contracts, constants, and fallback protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

DECISION_DIFFERENT = "DIFFERENT_ENTITY"
DECISION_SAME = "SAME_ENTITY"
DECISION_REVIEW = "REVIEW"

VALID_DECISIONS: tuple[str, ...] = (
    DECISION_DIFFERENT,
    DECISION_SAME,
    DECISION_REVIEW,
)

REASON_CODE_TO_DECISION: dict[str, str] = {
    "TEMPORAL_IMPOSSIBILITY": DECISION_DIFFERENT,
    "DOB_MISMATCH": DECISION_DIFFERENT,
    "LOCATION_MISMATCH": DECISION_DIFFERENT,
    "NAME_MISMATCH_VERIFIED": DECISION_DIFFERENT,
    "ENTITY_TYPE_MISMATCH": DECISION_DIFFERENT,
    "DECEASED": DECISION_DIFFERENT,
    "NAME_DOB_LOCATION_MATCH": DECISION_SAME,
    "NAME_DOB_MATCH": DECISION_SAME,
    "NAME_LOCATION_MATCH": DECISION_SAME,
    "INSUFFICIENT_DATA": DECISION_REVIEW,
    "AMBIGUOUS_CUSTOMER_RECORD": DECISION_REVIEW,
}

VALID_REASON_CODES: frozenset[str] = frozenset(REASON_CODE_TO_DECISION)

AMBIGUOUS_CUSTOMER_ID = "AMBIGUOUS"
ENTITY_INDIVIDUAL = "Individual"
ENTITY_COMPANY = "Company"

REQUIRED_DATA_FILES: tuple[str, ...] = (
    "customers.jsonl",
    "accounts.jsonl",
    "onboarding.jsonl",
    "kyc_checks.jsonl",
    "notes.jsonl",
    "alerts.jsonl",
    "hits.jsonl",
    "search_results.jsonl",
    "name_variants.json",
)

TASK_KYC_NOTE = "kyc_note"
TASK_RESIDENCE_NOTE = "residence_note"
TASK_HIT_P1_DOB = "hit_p1_dob"
TASK_SR_FACTS = "sr_facts"


class FactStatus(str, Enum):
    """Epistemic status of an extracted or parsed fact."""

    ABSENT = "ABSENT"
    PENDING_CLUE = "PENDING_CLUE"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True)
class Fact:
    """Provenance-tracked fact extracted from a customer or watchlist record."""

    field: str
    value: Any
    raw: str
    status: FactStatus
    subject: str
    source_type: str
    source_id: str
    location: str
    date_str: str = ""
    verified_state: str = ""


@dataclass(frozen=True)
class ParsedDate:
    """Calendar date with optional month and day precision."""

    year: int
    month: int | None = None
    day: int | None = None
    raw: str = ""

    @property
    def is_full(self) -> bool:
        """Return True when year, month, and day are all present."""
        return self.month is not None and self.day is not None

    @property
    def iso_str(self) -> str:
        """Return ISO-8601 representation matching the available precision."""
        if self.month is not None and self.day is not None:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.month is not None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}"


@dataclass(frozen=True)
class DateSpec:
    """Possible birth or incorporation years and exact dates from one source."""

    possible_years: tuple[int, ...]
    exact_dates: tuple[ParsedDate, ...]
    source_id: str
    origin: str
    raw: str = ""
    is_ambiguous_slash: bool = False


@dataclass(frozen=True)
class VerifiedIdentity:
    """Most recent approved identity attributes for a customer cluster."""

    name: Fact | None
    dob: Fact | None
    doc_country: Fact | None
    doc_number: str | None
    checked_at: str
    source_id: str
    conflict_facts: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class CandidatePerson:
    """Resolved customer person cluster and aggregated facts."""

    record_ids: tuple[str, ...]
    entity_type: str
    verified: VerifiedIdentity | None
    names: tuple[Fact, ...]
    dobs: tuple[Fact, ...]
    countries: tuple[Fact, ...]
    pending_clues: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class ResolvedHit:
    """Normalized watchlist hit attributes and temporal/geographic clues."""

    hit_id: str
    alert_id: str
    entity_type: str
    names: tuple[str, ...]
    aliases: tuple[str, ...]
    dob_specs: tuple[DateSpec, ...]
    countries: tuple[str, ...]
    earliest_report_date: ParsedDate | None
    deceased_date: ParsedDate | None
    sr_used_ids: tuple[str, ...]
    p1_kv_sr_conflict: bool = False
    pending_clues: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class HitDecision:
    """Final screening decision payload for a single hit."""

    hit_id: str
    alert_id: str
    decision: str
    reason_code: str
    customer_record_id: str | None
    reason: str
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the 7-field output schema dictionary."""
        return {
            "hit_id": self.hit_id,
            "alert_id": self.alert_id,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "customer_record_id": self.customer_record_id,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ExtractionRequest:
    """Structured extraction request passed to the LLM fallback boundary."""

    task: str
    text: str
    source_id: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    """Structured extraction result returned from the LLM fallback boundary."""

    task: str
    source_id: str
    status: FactStatus
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_quote: str | None = None
    model_id: str = ""
    prompt_version: str = ""
    error: str | None = None
    cached: bool = False


@runtime_checkable
class LLMFallback(Protocol):
    """Protocol for the 4 narrow free-text extraction tasks."""

    def extract_kyc_note(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        """Extract identity check fields from an unstructured KYC note."""

    def extract_residence_countries(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        """Extract lived/worked countries and subject attribution from a note."""

    def extract_hit_p1_dob(
        self, text: str, source_id: str, *, reference_date: str = ""
    ) -> ExtractionResult:
        """Extract birth date or age/reference date from hit profile paragraph 1."""

    def extract_sr_facts(
        self,
        text: str,
        source_id: str,
        *,
        category_phrase: str = "",
        hit_countries: tuple[str, ...] = (),
    ) -> ExtractionResult:
        """Extract age, birth date, reference date, and country from a search snippet."""


class NullLLMFallback:
    """No-op fallback implementation that marks unparsed clues as pending."""

    def extract_kyc_note(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        return ExtractionResult(
            task=TASK_KYC_NOTE,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            error="llm_disabled",
        )

    def extract_residence_countries(
        self, text: str, source_id: str, *, created_at: str = ""
    ) -> ExtractionResult:
        return ExtractionResult(
            task=TASK_RESIDENCE_NOTE,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            error="llm_disabled",
        )

    def extract_hit_p1_dob(
        self, text: str, source_id: str, *, reference_date: str = ""
    ) -> ExtractionResult:
        return ExtractionResult(
            task=TASK_HIT_P1_DOB,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            error="llm_disabled",
        )

    def extract_sr_facts(
        self,
        text: str,
        source_id: str,
        *,
        category_phrase: str = "",
        hit_countries: tuple[str, ...] = (),
    ) -> ExtractionResult:
        return ExtractionResult(
            task=TASK_SR_FACTS,
            source_id=source_id,
            status=FactStatus.PENDING_CLUE,
            error="llm_disabled",
        )
