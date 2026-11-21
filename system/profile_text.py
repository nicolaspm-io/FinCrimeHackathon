"""Free-text parsers for the watchlist side (POLICY principle 5).

Three sources of hit facts live in free text:
  1. Paragraph 1 of `profile_notes`: a birth date ("was born on 10 April 1960"), an age at a
     date ("aged 30, in November 1998", "then 37", "a 38-year-old ... in January 2025"), or a
     key-value line ("DOB: 6/20/1955", "Date of Birth: January 12, 1968").
  2. Dated reports: exactly the English lines "According to ...; September 10, 2025: ...".
  3. search_results snippets: about the hit only when they contain the hit's listing phrase
     AND name a country tied to the hit; they usually give an age at a month/year.

Wording varies in the unseen set (except the dated-report form and listing phrases), so the
parsers work on cues (an age word next to a number, a date in the same sentence) rather than
on fixed templates. When a cue is present but cannot be read safely, the result carries a
PENDING_CLUE fact instead of a guess; the optional LLMFallback may resolve it, and its answer
is accepted only after deterministic post-validation against the text.

Dates inside hit text are parsed in the hit-provider mode (ambiguous slash dates keep both
readings). Nothing here reads files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from . import countries as _countries
    from . import dates as _dates
    from . import names as _names
    from .contracts import DateSpec, ExtractionResult, Fact, FactStatus, LLMFallback, ParsedDate
except ImportError:
    import countries as _countries
    import dates as _dates
    import names as _names
    from contracts import DateSpec, ExtractionResult, Fact, FactStatus, LLMFallback, ParsedDate

ORIGIN_DOBS = "hit_dobs"
ORIGIN_P1_PROSE = "p1_prose"
ORIGIN_P1_KV = "p1_kv"
ORIGIN_SR = "sr"

CATEGORY_PHRASES = {
    "sanctions": "a sanctioned individual",
    "pep": "a politically exposed person",
    "enforcement": "the subject of enforcement action",
    "adverse media": "named in adverse media",
    "soe": "a state-owned enterprise official",
}
DEFAULT_PHRASE = "named on a watchlist"

MIN_AGE, MAX_AGE = 1, 120

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATED_REPORT = re.compile(rf"^According to .+?; ((?:{_MONTHS}) \d{{1,2}}, \d{{4}}):")

# A key-value birth line, e.g. "DOB: 6/20/1955", "Date of Birth - January 12, 1968".
_KV_DOB = re.compile(r"^\s*(?:d\.?\s?o\.?\s?b\.?|date\s+of\s+birth|birth\s*date|born)\s*[:=\-–]\s*(?P<value>.+?)\s*$", re.I)
_KV_AGE = re.compile(r"^\s*age\s*[:=\-–]\s*(?P<age>\d{1,3})\s*$", re.I)

# Age cues. Each pattern captures the age as group "age".
_AGE_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\baged\s+(?:about\s+|around\s+)?(?P<age>\d{1,3})\b",
    r"\bage\s+(?:of\s+)?(?P<age>\d{1,3})\b",
    r"\bat\s+(?:the\s+)?age\s+(?:of\s+)?(?P<age>\d{1,3})\b",
    r"\b(?P<age>\d{1,3})[\s-]*years?[\s-]*old\b",
    r"\b(?P<age>\d{1,3})\s+years?\s+of\s+age\b",
    r"\b(?P<age>\d{1,3})[\s-]+year[\s-]+old\b",
    r"\bthen\s+(?:aged\s+)?(?P<age>\d{1,3})\b",
    r"\b(?:he|she|they|who)\s+(?:was|were|is|turned|had\s+turned)\s+(?:aged\s+)?(?P<age>\d{1,3})\b(?!\s*(?:%|percent|years?\s+(?:in|of)\s+prison|times|counts?|people|persons|members|kg|kilograms?|grams?))",
    r"\breached\s+(?:the\s+age\s+of\s+)?(?P<age>\d{1,3})\b",
    r"\bturned\s+(?P<age>\d{1,3})\b",
))
# "Name, 34," / "Name (34)" right after a capitalised word: the common snippet form.
_NAME_AGE = re.compile(r"(?:[A-Z][\w'’\-]+)\s*[,(]\s*(?P<age>\d{1,3})\s*[,)]")
_AGE_WORD = re.compile(r"\bage[ds]?\b|\byears?[\s-]+old\b|\byear-old\b|\byears of age\b", re.I)
_FREE_NUMBER = re.compile(r"(?<![\w.,/:$£€-])\d{1,3}(?![\w%/:]|[.,]\d)")
_BIRTH_WORD = re.compile(r"\bborn\b|\bbirth\b|\bd\.?o\.?b\b", re.I)
_CLUE_WORDS = re.compile(r"\bborn\b|\bbirth\b|\bd\.?o\.?b\b|\baged?\b|\byears?[\s-]+old\b|\byear-old\b|\byears of age\b", re.I)
_PRONOUN = re.compile(r"\b(?:he|she|his|her|him|they|their)\b", re.I)


@dataclass(frozen=True)
class DobFinding:
    """Birth information found in one piece of text, with provenance."""

    specs: tuple[DateSpec, ...] = ()
    pending: tuple[Fact, ...] = ()
    notes: tuple[str, ...] = ()  # short human-readable trace of what was read

    @property
    def found(self) -> bool:
        return bool(self.specs)


@dataclass(frozen=True)
class SRFacts:
    """One search result as it relates to one hit."""

    result_id: str
    attributed: bool
    phrase_ok: bool
    countries: tuple[str, ...]
    finding: DobFinding = field(default_factory=DobFinding)


# ----------------------------------------------------------------------------- generic helpers

def listing_phrase(hit: dict[str, Any]) -> str:
    """POLICY 5 listing phrase from primary_category, else categories[0]."""
    category = hit.get("primary_category") or ((hit.get("categories") or [None])[0])
    return CATEGORY_PHRASES.get(str(category or "").strip().lower(), DEFAULT_PHRASE)


def paragraphs(notes: Any) -> list[str]:
    """Paragraphs of profile_notes: blocks separated by blank lines."""
    text = str(notes or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    return [p.strip() for p in re.split(r"\n[ \t]*\n", text) if p.strip()]


def first_paragraph(notes: Any) -> str:
    parts = paragraphs(notes)
    return parts[0] if parts else ""


def sentences(text: str) -> list[str]:
    """Split on newlines and sentence ends; keeps 'Dr.', initials and decimals together."""
    out = []
    for line in str(text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        start = 0
        for match in re.finditer(r"[.!?](?=\s+[A-Z\"“(])", line):
            end = match.end()
            word = line[start:end].rsplit(" ", 1)[-1]
            if re.fullmatch(r"(?:[A-Z]\.|Dr\.|Mr\.|Mrs\.|Ms\.|St\.|Jr\.|Sr\.|Inc\.|Ltd\.|Co\.|No\.|U\.S\.|Govt\.)", word):
                continue
            out.append(line[start:end].strip())
            start = end
        if line[start:].strip():
            out.append(line[start:].strip())
    return out


def _hit_name_parts(hit_names: Iterable[str]) -> set[str]:
    parts = set()
    for name in hit_names:
        parts.update(p for p in _names.name_parts(name) if len(p) >= 3)
    return parts


def _mentions(sentence: str, parts: set[str]) -> bool:
    return bool(parts & set(_names.normalize_tokens(sentence)))


def _plausible_age(value: str) -> int | None:
    age = int(value)
    return age if MIN_AGE <= age <= MAX_AGE else None


def _date_spans(text: str) -> list[tuple[int, int, str, tuple[ParsedDate, ...]]]:
    return _dates.find_dates(text, _dates.HIT)


def _inside(span: tuple[int, int], spans: Iterable[tuple[int, int, Any, Any]]) -> bool:
    return any(s <= span[0] and span[1] <= e for s, e, _, _ in spans)


def _reference_year(spans, exclude=()) -> tuple[ParsedDate | None, str]:
    """The single reference date of a sentence (dates with a distinct year); else None."""
    years = {}
    for start, end, raw, alternatives in spans:
        if (start, end) in exclude:
            continue
        for alt in alternatives:
            years.setdefault(alt.year, (alt, raw))
    if len(years) == 1:
        return next(iter(years.values()))
    return None, ""


def _pending(field_name: str, raw: str, source_id: str, location: str) -> Fact:
    return Fact(field_name, None, raw[:500], FactStatus.PENDING_CLUE, "hit", "HIT" if location != "snippet" else "SR",
                source_id, location)


# ----------------------------------------------------------------------------- dated reports

def dated_reports(notes: Any) -> list[tuple[ParsedDate, str]]:
    """(date, line) for every exact 'According to ...; Month DD, YYYY:' line, in text order."""
    out = []
    for line in str(notes or "").split("\n"):
        match = DATED_REPORT.match(line.strip())
        if not match:
            continue
        parsed = _dates.parse_date(match.group(1), _dates.HIT)
        if parsed is not None:
            out.append((parsed, line.strip()))
    return out


def earliest_report_date(notes: Any) -> ParsedDate | None:
    reports = dated_reports(notes)
    return min((d for d, _ in reports), key=lambda d: (d.year, d.month or 0, d.day or 0)) if reports else None


# ----------------------------------------------------------------------------- paragraph 1

def _age_in(sentence: str, date_spans) -> tuple[int | None, tuple[int, int] | None]:
    """The age stated in a sentence (one distinct value), ignoring numbers inside dates."""
    found = {}
    for pattern in _AGE_PATTERNS + (_NAME_AGE,):
        for match in pattern.finditer(sentence):
            span = match.span("age")
            if _inside(span, date_spans):
                continue
            age = _plausible_age(match.group("age"))
            if age is not None:
                found.setdefault(age, span)
    if not found and _AGE_WORD.search(sentence):
        # Reworded cue ("The age of X was 38 when ..."): exactly one free-standing number.
        for match in _FREE_NUMBER.finditer(sentence):
            if not _inside(match.span(), date_spans):
                age = _plausible_age(match.group(0))
                if age is not None:
                    found.setdefault(age, match.span())
    if len(found) == 1:
        age, span = next(iter(found.items()))
        return age, span
    return None, None


def parse_p1(p1: str, hit_id: str, hit_names: Iterable[str]) -> DobFinding:
    """Birth information in paragraph 1 of a hit's profile notes.

    Key-value lines give ORIGIN_P1_KV specs; narrative sentences give ORIGIN_P1_PROSE specs.
    A sentence only counts when it names the hit (a name part of 3+ letters) or uses a
    pronoun in a paragraph that names the hit. Cues that cannot be read become pending clues.
    """
    if not p1:
        return DobFinding()
    parts = _hit_name_parts(hit_names)
    names_hit = _mentions(p1, parts)
    specs, pending, notes = [], [], []
    lines = p1.split("\n")
    report_dates = [d for line in lines for d in [_dates.find_dates(line, _dates.HIT)] if d]

    for line in lines:
        kv = _KV_DOB.match(line)
        if kv:
            spec = _dates.spec_from_raw(kv.group("value"), hit_id, ORIGIN_P1_KV, _dates.HIT)
            if spec is None:
                # "DOB: 6/20/1955 (approx.)" -> try the first date inside the value
                inner = _dates.find_dates(kv.group("value"), _dates.HIT)
                if len(inner) == 1:
                    spec = _dates.spec_from_dates(inner[0][3], hit_id, ORIGIN_P1_KV, inner[0][2], len(inner[0][3]) > 1)
            if spec is not None:
                specs.append(spec)
                notes.append(f"p1 key-value birth date {kv.group('value')!r}")
            else:
                pending.append(_pending("dob", line, hit_id, "profile_notes#p1"))
            continue
        kv_age = _KV_AGE.match(line)
        if kv_age:
            # "Age: 55" in a block "information as of May 15, 2019:" -> the block's own date
            others = [d for d in report_dates if d and d[0][2] not in line]
            ref, raw = _reference_year([s for group in others for s in group])
            age = _plausible_age(kv_age.group("age"))
            if ref is not None and age is not None:
                specs.append(_dates.spec_from_age(age, ref, hit_id, ORIGIN_P1_KV, f"Age: {age} ({raw})"))
                notes.append(f"p1 key-value age {age} at {ref.iso_str}")
            else:
                pending.append(_pending("age", line, hit_id, "profile_notes#p1"))

    for sentence in sentences(p1):
        if _KV_DOB.match(sentence) or _KV_AGE.match(sentence):
            continue
        if not _CLUE_WORDS.search(sentence) and not _NAME_AGE.search(sentence) and not re.search(r"\bthen\s+\d|\b(?:he|she) was \d", sentence, re.I):
            continue
        about_hit = _mentions(sentence, parts) or (names_hit and _PRONOUN.search(sentence))
        if not about_hit:
            continue
        spans = _date_spans(sentence)
        if _BIRTH_WORD.search(sentence) and spans:
            births = [s for s in spans if s[3]]
            if len(births) == 1:
                start, end, raw, alternatives = births[0]
                specs.append(_dates.spec_from_dates(alternatives, hit_id, ORIGIN_P1_PROSE, raw, len(alternatives) > 1))
                notes.append(f"p1 birth date {raw!r}")
                continue
        age, age_span = _age_in(sentence, spans)
        if age is not None:
            ref, raw = _reference_year(spans)
            if ref is not None:
                specs.append(_dates.spec_from_age(age, ref, hit_id, ORIGIN_P1_PROSE, f"aged {age} in {raw}"))
                notes.append(f"p1 age {age} at {raw!r}")
                continue
        if _CLUE_WORDS.search(sentence) or age is not None:
            if _BIRTH_WORD.search(sentence) and not spans:
                continue  # "was born in the United States": no date, nothing to read
            pending.append(_pending("dob", sentence, hit_id, "profile_notes#p1"))
    return DobFinding(tuple(specs), tuple(pending), tuple(notes))


# ----------------------------------------------------------------------------- search results

def phrase_in(snippet: str, phrase: str) -> bool:
    return phrase.lower() in " ".join(str(snippet or "").split()).lower()


def parse_snippet_dob(snippet: str, result_id: str) -> DobFinding:
    """Birth information in one snippet: a birth date, or one age plus one reference year."""
    text = " ".join(str(snippet or "").split())
    spans = _date_spans(text)
    if _BIRTH_WORD.search(text) and spans:
        births = [s for s in spans if s[3]]
        if len(births) == 1:
            _, _, raw, alternatives = births[0]
            return DobFinding((_dates.spec_from_dates(alternatives, result_id, ORIGIN_SR, raw, len(alternatives) > 1),),
                              (), (f"{result_id} birth date {raw!r}",))
    age, _ = _age_in(text, spans)
    # bare "..., 34, ..." numbers that are not inside a date
    numbers = {int(m.group(0)) for m in _FREE_NUMBER.finditer(text)
               if not _inside(m.span(), spans) and MIN_AGE <= int(m.group(0)) <= MAX_AGE}
    if age is None and len(numbers) == 1:
        age = next(iter(numbers))
    ref, raw = _reference_year(spans)
    if age is not None and ref is not None:
        return DobFinding((_dates.spec_from_age(age, ref, result_id, ORIGIN_SR, f"{age} in {raw}"),), (),
                          (f"{result_id} age {age} at {raw!r}",))
    if age is not None or numbers or _CLUE_WORDS.search(text):
        return DobFinding((), (_pending("dob", text, result_id, "snippet"),), ())
    return DobFinding()


def attribute_search_result(result: dict[str, Any], phrase: str, hit_countries: Iterable[str]) -> SRFacts:
    """POLICY 5: a result is about the hit only if it has the listing phrase and a hit country."""
    snippet = str(result.get("snippet") or "")
    text = f"{result.get('title') or ''} {snippet}"
    mentioned = _countries.country_mentions(text)
    shared = tuple(sorted(set(mentioned) & set(hit_countries)))
    phrase_ok = phrase_in(snippet, phrase)
    attributed = phrase_ok and bool(shared)
    finding = parse_snippet_dob(snippet, result.get("result_id", "")) if attributed else DobFinding()
    return SRFacts(result.get("result_id", ""), attributed, phrase_ok, shared, finding)


# ----------------------------------------------------------------------------- LLM fallback

def _validated_llm_spec(result: ExtractionResult, text: str, source_id: str, origin: str) -> DateSpec | None:
    """Accept an LLM extraction only if its numbers appear in the text and it parses.

    Expected payload keys (any subset): "birth_date" (str), "age" (int), "reference_date" (str),
    "subject_is_hit" (bool). The evidence quote, when given, must be a substring of the text.
    """
    if result is None or result.status != FactStatus.VERIFIED:
        return None
    payload = result.payload or {}
    folded = " ".join(str(text).split())
    if result.evidence_quote and " ".join(result.evidence_quote.split()) not in folded:
        return None
    if payload.get("subject_is_hit") is False:
        return None
    birth = payload.get("birth_date")
    if birth:
        alternatives = _dates.parse_date_alternatives(str(birth), _dates.HIT)
        if alternatives and str(alternatives[0].year) in folded:
            return _dates.spec_from_dates(alternatives, source_id, origin, str(birth), len(alternatives) > 1)
    age, ref = payload.get("age"), payload.get("reference_date")
    if age is not None and ref:
        try:
            age = int(age)
        except (TypeError, ValueError):
            return None
        ref_date = _dates.parse_date(str(ref), _dates.HIT)
        if ref_date and MIN_AGE <= age <= MAX_AGE and re.search(rf"\b{age}\b", folded) and str(ref_date.year) in folded:
            return _dates.spec_from_age(age, ref_date, source_id, origin, f"{age} in {ref}")
    return None


def resolve_pending_p1(finding: DobFinding, p1: str, hit_id: str, llm: LLMFallback | None,
                       reference_date: str = "") -> DobFinding:
    """Ask the LLM about paragraph 1 when local parsing left only pending clues."""
    if llm is None or finding.found or not finding.pending:
        return finding
    try:
        result = llm.extract_hit_p1_dob(p1, hit_id, reference_date=reference_date)
    except Exception:  # noqa: BLE001 - the fallback must never break a run
        return finding
    spec = _validated_llm_spec(result, p1, hit_id, ORIGIN_P1_PROSE)
    if spec is None:
        return finding
    return DobFinding((spec,), (), finding.notes + (f"p1 read by fallback {result.model_id}",))


def resolve_pending_sr(facts: SRFacts, snippet: str, phrase: str, hit_countries: tuple[str, ...],
                       llm: LLMFallback | None) -> SRFacts:
    """Ask the LLM about an attributed snippet whose birth information was not read locally."""
    if llm is None or not facts.attributed or facts.finding.found or not facts.finding.pending:
        return facts
    try:
        result = llm.extract_sr_facts(snippet, facts.result_id, category_phrase=phrase, hit_countries=hit_countries)
    except Exception:  # noqa: BLE001
        return facts
    spec = _validated_llm_spec(result, snippet, facts.result_id, ORIGIN_SR)
    if spec is None:
        return facts
    return SRFacts(facts.result_id, True, facts.phrase_ok, facts.countries,
                   DobFinding((spec,), (), (f"{facts.result_id} read by fallback {result.model_id}",)))
