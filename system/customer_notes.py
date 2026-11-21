"""Parsers for KYC-channel and support residence notes with LLM fallback hooks."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

try:
    from . import countries, dates
    from .contracts import Fact, FactStatus, LLMFallback, ParsedDate
except ImportError:
    import countries
    import dates
    from contracts import Fact, FactStatus, LLMFallback, ParsedDate

_KYC_REJECTED_RE = re.compile(
    r"\b(?:rejected|failed|failure|declined|denied|unverified|invalid|"
    r"unsuccessful|not\s+approved|not\s+verified|not\s+valid|did\s+not\s+pass|"
    r"could\s+not\s+verify|unable\s+to\s+verify|failed\s+to\s+verify|"
    r"does\s+not\s+match|no\s+match|mismatch|discrepancy)\b",
    re.IGNORECASE,
)

_KYC_EXPIRED_RE = re.compile(
    r"\b(?:expired|no\s+longer\s+valid|validity\s+lapsed|past\s+expiry|out\s+of\s+date)\b",
    re.IGNORECASE,
)

_KYC_PENDING_RE = re.compile(
    r"\b(?:pending|awaiting|incomplete|in\s+progress|inconclusive|under\s+review)\b",
    re.IGNORECASE,
)

# Any remaining failure or negation cue means the note does not record an approved check
# (POLICY 4). Checked after the rejected/expired/pending lists and before the broad approval
# words, so "could not be validated" or "was unable to be completed" is never "approved".
# On dev it fires on 0 of 22,575 kyc notes.
_KYC_NEGATION_CUE_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|unable|refused|abandoned|revoked|forged|fake|"
    r"fraud\w*|tamper\w*|negative|flagged|suspended|withdrawn|cancel(?:l)?ed|error)\b",
    re.IGNORECASE,
)

_KYC_APPROVED_RE = re.compile(
    r"\b(?:approved|verified|confirmed|confirms|confirming|cleared|passed|pass|passing|"
    r"validated|validates|accepted|authenticated|finalized|finalised|completed|complete|"
    r"processed|matches|matched|successful|successfully|success|vetted|concluded|"
    r"onboarded|finished|achieved|valid|identified|"
    r"underwent\s+(?:[a-z]+\s+){0,2}verification)\b",
    re.IGNORECASE,
)

_DOB_BEFORE_RE = re.compile(
    r"(?:dob|d\.o\.b\.|date\s+of\s+birth|birth\s+date|birthdate|born(?:\s+on)?)"
    r"(?:\s+(?:recorded|listed|confirmed|verified|noted|stated))?"
    r"(?:\s+(?:as|is|of|:))*\s*$",
    re.IGNORECASE,
)

_DOB_AFTER_RE = re.compile(
    r"^\s*(?:as\s+(?:(?:his|her|their)\s+)?birth\s+date|birth\s+date|date\s+of\s+birth)",
    re.IGNORECASE,
)

# "no" must be a whole word (or "no."), and the captured value must contain a digit: without
# both, "licence noting a date of birth" was read as document number "ting", and two unrelated
# customers sharing that fake number were merged into one person (POLICY 3).
_DOC_NUM_RE = re.compile(
    r"\b(?:document|passport|id|licence|license)\s+(?:number|no\.|no\b|#)\s*[:\-#]?\s*"
    r"([A-Za-z0-9\-]*\d[A-Za-z0-9\-]*)\b",
    re.IGNORECASE,
)

_CONTEXT_CODE2_RE = re.compile(
    r"\b(?:from|issued\s+(?:in|by)|country\s*:)\s*(?:the\s+)?([A-Z]{2,3})\b"
)

_RESIDENCE_SIGNAL_RE = re.compile(
    r"\b(?:live[ds]?|living|resid(?:e|es|ed|ing|ence|ency|ential|ent|ents)|"
    r"work(?:s|ed|ing|force)?|employ(?:s|ed|ing|ment|ee|ees)|based|home|"
    r"household|dwell(?:s|ed|ing)?|career|job|position|professional(?:ly)?|"
    r"tenure|stay(?:s|ed|ing)?|relocat(?:e|es|ed|ing|ion)|moved\s+to|settled|"
    r"domicil(?:e|ed)|stationed|posted|branch|office|operat(?:e|es|ed|ing)|"
    r"established|situated|labor|labour|"
    r"life\s+and\s+(?:business|labor|labour))\b",
    re.IGNORECASE,
)

_RESIDENCE_CODE2_RE = re.compile(
    r"\b(?:lived|resided|worked|based)\s+in\s+([A-Z]{2})\b"
)

_TRANSFER_ONLY_RE = re.compile(
    r"\b(?:transfer|wire|remittance|payment|transaction|card\s+used|atm|"
    r"withdrawal|holiday|vacation|tourist|trip|visit(?:ed|ing)?|flight|hotel)\b",
    re.IGNORECASE,
)

_NEGATION_RE = re.compile(
    r"\b(?:never\s+(?:lived|resided|worked|stayed)|"
    r"did\s+not\s+(?:live|reside|work|stay)|"
    r"has\s+not\s+(?:lived|resided|worked)|"
    r"not\s+a\s+resident|denied\s+(?:living|residing|any\s+residence)|"
    r"no\s+residence|not\s+lived|"
    # broader clause negation: "were not living in", "denied ever living", "refuted claims of"
    r"not|never|neither|nor|denied|denies|deny|refuted|refutes|refute|disputed|disputes|"
    r"falsely|no\s+residency)\b",
    re.IGNORECASE,
)

_THIRD_PARTY_RE = re.compile(
    r"\b(?:brother|sister|spouse|wife|husband|mother|father|parent|"
    r"son|daughter|uncle|aunt|cousin|friend|colleague|relative|"
    r"neighbor|neighbour)\b",
    re.IGNORECASE,
)

_NON_NAME_WORDS = frozenset(
    {
        "sumsub",
        "veriff",
        "persona",
        "onfido",
        "id",
        "dob",
        "kyc",
        "pdf",
        "verification",
        "identity",
        "document",
        "passport",
        "national",
        "driving",
        "licence",
        "license",
        "customer",
        "client",
        "subject",
        "applicant",
        "holder",
        "record",
        "records",
        "checks",
        "check",
        "submission",
        "per",
        "on",
        "as",
        "of",
        "for",
        "the",
    }
)

_NAME_EXPLICIT_PATTERNS = (
    re.compile(r"\bName on document\s*:\s*([^.;\n]+)", re.IGNORECASE),
    re.compile(r"\bname\s+([^,.;\n]+?)(?:,\s*(?:DOB|born)|[.;]|$)", re.IGNORECASE),
    re.compile(
        r"\bidentifying\s+(?:the\s+)?(?:holder|subject|customer|applicant)\s+as\s+"
        r"([^,.;\n(]+)",
        re.IGNORECASE,
    ),
)

_NAME_SHOWS_FALLBACK_RE = re.compile(
    r"\b(?:document|record|ID|passport|licence|license)\s+(?:shows|lists)\s+"
    r"([^,.;\n(]+?)(?=\s+(?:validated|verified|approved|cleared|confirmed|on\s+\d)|,\s*(?:born|DOB|d\.o\.b\.)|\s*\(|$)",
    re.IGNORECASE,
)

_NAME_PRE_DOB_RE = re.compile(
    r"(?:^|[.:;]\s*|\b(?:for|of|matches)\s+)"
    r"([A-Z0-9][^\n,.;()]{1,65}?)"
    r"(?=(?:,\s*|\s*\(\s*)(?:born|dob|d\.o\.b\.|date\s+of\s+birth)\b)",
    re.IGNORECASE,
)

_NAME_POSSESSIVE_RE = re.compile(
    r"(?:^|[.:;]\s*|\b(?:for|of)\s+)"
    r"([^\n,.;()]{2,65}?)'s\s+(?:identity|driving|passport|national|credentials|id\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedKycNote:
    """Structured result parsed from one KYC-channel note."""

    note_id: str
    status: str
    verified_name: str | None
    verified_dob: ParsedDate | None
    verified_dob_raw: str
    doc_country: str | None
    doc_number: str | None
    checked_at: str
    raw_text: str
    pending_clues: tuple[Fact, ...] = ()


def extract_doc_number(text: str) -> str | None:
    """Document number written in a KYC note ("passport number X12345678"), or None."""
    match = _DOC_NUM_RE.search(text or "")
    if not match:
        return None
    value = match.group(1).strip("-")
    return value if 4 <= len(value) <= 25 else None


def detect_kyc_status(text: str) -> str:
    """Determine verification status conservatively (rejected/expired beat approved)."""
    if _KYC_REJECTED_RE.search(text):
        return "rejected"
    if _KYC_EXPIRED_RE.search(text):
        return "expired"
    if _KYC_PENDING_RE.search(text):
        return "unknown"
    if _KYC_NEGATION_CUE_RE.search(text):
        return "rejected"
    if _KYC_APPROVED_RE.search(text):
        return "approved"
    return "unknown"


def _extract_checked_at(note: dict[str, Any], text: str) -> str:
    """Return ISO check date from note metadata or explicit check-date phrase."""
    created = note.get("created_at")
    if isinstance(created, str) and created.strip():
        parsed = dates.parse_date(created, dates.BANK)
        return parsed.iso_str if parsed else created.strip()

    for match in re.finditer(r"\b(?:on|as\s+of|dated)\s+([^.;\n]{4,30})", text, re.I):
        snippet = match.group(1)
        found = dates.find_dates(snippet, dates.BANK)
        if found:
            return found[0][3][0].iso_str
    return ""


def _extract_kyc_dob(text: str, checked_at_iso: str) -> tuple[ParsedDate | None, str]:
    """Extract birth date from a KYC note without confusing it with checked_at."""
    found = dates.find_dates(text, dates.BANK)
    if not found:
        return None, ""

    for start, end, raw_span, alts in found:
        before = text[max(0, start - 40) : start]
        after = text[end : min(len(text), end + 30)]
        if _DOB_BEFORE_RE.search(before) or _DOB_AFTER_RE.match(after):
            return alts[0], raw_span

    if checked_at_iso and len(found) >= 2:
        checked_parsed = dates.parse_date(checked_at_iso, dates.BANK)
        checked_year = checked_parsed.year if checked_parsed else None
        non_check = [
            (raw_span, alts[0])
            for _, _, raw_span, alts in found
            if alts[0].iso_str != checked_at_iso
            and (checked_year is None or alts[0].year < checked_year)
        ]
        if len(non_check) == 1:
            return non_check[0][1], non_check[0][0]

    return None, ""


def _extract_kyc_country(text: str) -> str | None:
    """Extract canonical document issuing country from a KYC note."""
    mentions = countries.country_mentions(text)
    if len(mentions) == 1:
        return mentions[0]
    if len(mentions) > 1:
        doc_match = re.search(
            r"\b(?:passport|driving\s+licen[cs]e|national\s+id|document|issued)\b",
            text,
            re.IGNORECASE,
        )
        if doc_match:
            pos = doc_match.start()
            return min(
                mentions,
                key=lambda c: abs(text.lower().find(c.lower()) - pos)
                if c.lower() in text.lower()
                else 10_000,
            )
        return mentions[0]

    for match in _CONTEXT_CODE2_RE.finditer(text):
        norm = countries.normalize_country(match.group(1))
        if norm:
            return norm
    return None


def _clean_name_candidate(raw: str) -> str | None:
    """Trim leading/trailing descriptors and reject non-name boilerplate."""
    candidate = raw.strip(" \t\r\n.,;:-")
    candidate = re.sub(
        r"^(?:the\s+(?:customer|client|holder|subject|applicant)\s+)",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip(" \t\r\n.,;:-")
    if not candidate:
        return None
    words = [w.strip(".,;:'\"()").lower() for w in candidate.split()]
    meaningful = [w for w in words if w and w not in _NON_NAME_WORDS]
    if not meaningful:
        return None
    if len(words) == 1 and words[0] in _NON_NAME_WORDS:
        return None
    return candidate


def _strip_possessive(tok: str) -> str:
    """Strip trailing punctuation and possessive 's / ’s from a token."""
    cleaned = tok.strip(",.;:()")
    if cleaned.endswith("'s") or cleaned.endswith("’s"):
        cleaned = cleaned[:-2]
    elif cleaned.endswith("'S") or cleaned.endswith("’S"):
        cleaned = cleaned[:-2]
    return cleaned


def _is_uppercase_name_token(tok: str) -> bool:
    """True for uppercase name tokens or name placeholders (N/A, 0000, XXX)."""
    cleaned = _strip_possessive(tok)
    if not cleaned:
        return False
    upper = cleaned.upper()
    if upper in ("N/A", "NA", "XXX") or cleaned == "0000":
        return True
    if upper.lower() in _NON_NAME_WORDS or len(cleaned) < 2:
        return False
    if any(ch.isdigit() for ch in cleaned):
        return False
    letters = [ch for ch in cleaned if ch.isalpha()]
    return bool(letters) and all(ch.isupper() for ch in letters)


def _extract_uppercase_span(text: str) -> str | None:
    """Extract contiguous ALL-CAPS / placeholder customer name spans."""
    tokens = text.split()
    best: list[str] = []
    current: list[str] = []

    def _flush() -> None:
        nonlocal best
        has_real_alpha = any(
            any(ch.isalpha() for ch in t) and t.upper() not in ("N/A", "NA", "XXX")
            for t in current
        )
        if len(current) >= 2 and has_real_alpha and len(current) > len(best):
            best = list(current)
        current.clear()

    for raw_tok in tokens:
        if _is_uppercase_name_token(raw_tok):
            current.append(_strip_possessive(raw_tok))
            if raw_tok.endswith((",", ".", ";", ")", "'s", "’s", "'s,", "’s,")):
                _flush()
        else:
            _flush()
    _flush()

    if not best:
        return None
    return _clean_name_candidate(" ".join(best))


def _extract_kyc_name(text: str) -> str | None:
    """Extract verified legal name from a KYC note (explicit labels, caps, mixed-case)."""
    for pattern in _NAME_EXPLICIT_PATTERNS:
        match = pattern.search(text)
        if match:
            cleaned = _clean_name_candidate(match.group(1))
            if cleaned:
                return cleaned

    caps_span = _extract_uppercase_span(text)
    if caps_span:
        return caps_span

    for pattern in (_NAME_SHOWS_FALLBACK_RE, _NAME_PRE_DOB_RE, _NAME_POSSESSIVE_RE):
        match = pattern.search(text)
        if match:
            raw_cand = match.group(1)
            raw_cand = re.sub(
                r"^(?:.*\b(?:for|of|by|via|using|belonging\s+to|confirms|verified|approved|cleared|passed|validated|checked|shows|lists|that)\s+)",
                "",
                raw_cand,
                flags=re.IGNORECASE,
            )
            cleaned = _clean_name_candidate(raw_cand)
            if cleaned and not countries.normalize_country(cleaned):
                return cleaned

    return None


def parse_kyc_note(
    note: dict[str, Any], *, llm_fallback: LLMFallback | None = None
) -> ParsedKycNote:
    """Parse a KYC-channel note deterministically with optional LLM fallback."""
    note_id = str(note.get("note_id") or "")
    text = str(note.get("text") or "")
    checked_at = _extract_checked_at(note, text)
    status = detect_kyc_status(text)
    verified_dob, verified_dob_raw = _extract_kyc_dob(text, checked_at)
    doc_country = _extract_kyc_country(text)
    verified_name = _extract_kyc_name(text)

    doc_number = extract_doc_number(text)

    pending_clues: list[Fact] = []

    needs_fallback = status == "unknown" or (
        status == "approved"
        and (verified_name is None or verified_dob is None or doc_country is None)
    )
    if needs_fallback and text.strip():
        if llm_fallback is not None:
            res = llm_fallback.extract_kyc_note(
                text, note_id, created_at=checked_at
            )
            if res.status == FactStatus.VERIFIED and res.payload:
                llm_status = res.payload.get("status")
                # Never allow LLM to overturn a local rejected/expired detection
                if status == "unknown" and llm_status in (
                    "approved",
                    "rejected",
                    "expired",
                ):
                    status = llm_status
                if verified_name is None and res.payload.get("verified_name"):
                    verified_name = str(res.payload["verified_name"]).strip()
                if verified_dob is None and res.payload.get("verified_dob"):
                    parsed_llm_dob = dates.parse_date(
                        res.payload["verified_dob"], dates.BANK
                    )
                    if parsed_llm_dob is not None:
                        verified_dob = parsed_llm_dob
                        verified_dob_raw = str(
                            res.payload.get("verified_dob_raw")
                            or res.payload["verified_dob"]
                        )
                if doc_country is None and res.payload.get("document_country"):
                    doc_country = res.payload["document_country"]
                if doc_number is None and res.payload.get("document_number"):
                    doc_number = str(res.payload["document_number"]).strip()
                if not checked_at and res.payload.get("checked_at"):
                    checked_at = str(res.payload["checked_at"]).strip()
            elif res.status == FactStatus.PENDING_CLUE:
                if status == "unknown" or (
                    status == "approved"
                    and (verified_name is None or verified_dob is None)
                ):
                    pending_clues.append(
                        Fact(
                            field="kyc_note",
                            value=None,
                            raw=text,
                            status=FactStatus.PENDING_CLUE,
                            subject="customer",
                            source_type="NOTE",
                            source_id=note_id,
                            location="note_body",
                            date_str=checked_at,
                            verified_state=status,
                        )
                    )
        elif status == "unknown" or (
            status == "approved" and (verified_name is None or verified_dob is None)
        ):
            pending_clues.append(
                Fact(
                    field="kyc_note",
                    value=None,
                    raw=text,
                    status=FactStatus.PENDING_CLUE,
                    subject="customer",
                    source_type="NOTE",
                    source_id=note_id,
                    location="note_body",
                    date_str=checked_at,
                    verified_state=status,
                )
            )

    return ParsedKycNote(
        note_id=note_id,
        status=status,
        verified_name=verified_name,
        verified_dob=verified_dob,
        verified_dob_raw=verified_dob_raw,
        doc_country=doc_country,
        doc_number=doc_number,
        checked_at=checked_at,
        raw_text=text,
        pending_clues=tuple(pending_clues),
    )


def _residence_fact(country: str, raw_text: str, note_id: str, created_at: str) -> Fact:
    """Create a VERIFIED customer residence Fact from a support note."""
    return Fact(
        field="country",
        value=country,
        raw=raw_text,
        status=FactStatus.VERIFIED,
        subject="customer",
        source_type="NOTE",
        source_id=note_id,
        location="note_body",
        date_str=created_at,
        verified_state="declared",
    )


def _pending_residence_clue(raw_text: str, note_id: str, created_at: str) -> Fact:
    """Create a PENDING_CLUE residence Fact when note meaning cannot be settled."""
    return Fact(
        field="country",
        value=None,
        raw=raw_text,
        status=FactStatus.PENDING_CLUE,
        subject="unknown",
        source_type="NOTE",
        source_id=note_id,
        location="note_body",
        date_str=created_at,
        verified_state="declared",
    )


def parse_residence_note(
    note: dict[str, Any], *, llm_fallback: LLMFallback | None = None
) -> tuple[tuple[Fact, ...], tuple[Fact, ...]]:
    """Parse a non-KYC note for customer residence countries or pending clues."""
    note_id = str(note.get("note_id") or "")
    text = str(note.get("text") or "")
    created_at = str(note.get("created_at") or "")
    if not text.strip():
        return (), ()

    mentioned = list(countries.country_mentions(text))
    for match in _RESIDENCE_CODE2_RE.finditer(text):
        code_country = countries.normalize_country(match.group(1))
        if code_country and code_country not in mentioned:
            mentioned.append(code_country)

    if not mentioned:
        return (), ()

    has_residence = bool(_RESIDENCE_SIGNAL_RE.search(text))
    has_transfer_only = bool(_TRANSFER_ONLY_RE.search(text)) and not has_residence
    has_negation = bool(_NEGATION_RE.search(text))
    has_third_party = bool(_THIRD_PARTY_RE.search(text))

    # Pure transfer / travel mention without residence semantics
    if has_transfer_only:
        return (), ()

    # Clean deterministic customer residence statement
    if has_residence and not has_negation and not has_third_party:
        facts = tuple(
            _residence_fact(country, text, note_id, created_at) for country in mentioned
        )
        return facts, ()

    # Ambiguous or third-party / negated mention: delegate to LLM fallback if provided
    if llm_fallback is not None:
        res = llm_fallback.extract_residence_countries(
            text, note_id, created_at=created_at
        )
        if res.status == FactStatus.VERIFIED:
            facts = tuple(
                _residence_fact(entry["country"], text, note_id, created_at)
                for entry in (res.payload.get("lived_countries") or ())
                if isinstance(entry, dict)
                and entry.get("subject") == "customer"
                and entry.get("country")
            )
            return facts, ()
        if has_negation or has_third_party:
            return (), ()
        return (), (_pending_residence_clue(text, note_id, created_at),)

    # Without LLM: clear negation / third-party drops the country rather than fabricating
    if has_negation or has_third_party:
        return (), ()

    return (), (_pending_residence_clue(text, note_id, created_at),)


def parse_customer_notes(
    notes: Iterable[dict[str, Any]], *, llm_fallback: LLMFallback | None = None
) -> tuple[tuple[ParsedKycNote, ...], tuple[Fact, ...], tuple[Fact, ...]]:
    """Lazily parse KYC and non-KYC notes for a candidate cluster."""
    kyc_notes: list[ParsedKycNote] = []
    residence_facts: list[Fact] = []
    pending_clues: list[Fact] = []

    for note in sorted(
        notes, key=lambda n: (str(n.get("note_id") or ""), str(n.get("created_at") or ""))
    ):
        channel = str(note.get("channel") or "").strip().lower()
        if channel == "kyc":
            parsed_kyc = parse_kyc_note(note, llm_fallback=llm_fallback)
            kyc_notes.append(parsed_kyc)
            if parsed_kyc.pending_clues:
                pending_clues.extend(parsed_kyc.pending_clues)
        else:
            facts, clues = parse_residence_note(note, llm_fallback=llm_fallback)
            residence_facts.extend(facts)
            pending_clues.extend(clues)

    return tuple(kyc_notes), tuple(residence_facts), tuple(pending_clues)
