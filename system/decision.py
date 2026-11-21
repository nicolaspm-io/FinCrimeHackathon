"""SOP decision engine (POLICY principles 3, 6 and 7).

For every candidate customer person the hit is compared on type, death, time, date of birth,
location and verified name. EVERY applicable contradiction is collected (the reference
explanations state all of them and cite their union); the first in rule order gives the
reason code:

  ENTITY_TYPE_MISMATCH > DECEASED > TEMPORAL_IMPOSSIBILITY > DOB_MISMATCH >
  LOCATION_MISMATCH > NAME_MISMATCH_VERIFIED

Guards that keep a clearance from costing 60:
  - a pending clue on either side blocks only the rules it could overturn, including a
    location clearance when the missing name or birth fact could make name and date of
    birth agree, and any unread country fact;
  - the P1 key-value vs search-result case (decision #4) clears only if BOTH sources
    contradict the customer, and is REVIEW if they disagree about the customer;
  - a country difference never clears a hit whose name and date of birth agree (REVIEW);
  - declared birth dates that agree at different precision stay one date;
  - hit countries are canonicalised, and "International" / "Europe" are not countries;
  - an individual whose names have fewer than two real parts is neither confirmed nor
    cleared on name;
  - a customer with no entity type is not confirmed as the hit's company.
Without a contradiction: confirm a person on name + date of birth (+ country for the
_LOCATION_ variant), a company on name + country; otherwise REVIEW / INSUFFICIENT_DATA.

Several candidate persons: each is decided; unanimous -> that decision with
customer_record_id AMBIGUOUS; otherwise REVIEW / AMBIGUOUS_CUSTOMER_RECORD.
No candidate: customer_record_id null; only customer-independent contradictions (the
screened entity type, a death before screening) can clear.

Inputs are the frozen contracts (CandidatePerson, ResolvedHit). Fact values are read
leniently: a date may be a ParsedDate or a raw bank-format string, a country a canonical
name or any value countries.py understands.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from . import countries as _countries
    from . import dates as _dates
    from . import names as _names
    from .contracts import (AMBIGUOUS_CUSTOMER_ID, DECISION_DIFFERENT, DECISION_REVIEW, DECISION_SAME,
                            ENTITY_COMPANY, CandidatePerson, DateSpec, Fact, FactStatus, ParsedDate, ResolvedHit)
except ImportError:
    import countries as _countries
    import dates as _dates
    import names as _names
    from contracts import (AMBIGUOUS_CUSTOMER_ID, DECISION_DIFFERENT, DECISION_REVIEW, DECISION_SAME,
                           ENTITY_COMPANY, CandidatePerson, DateSpec, Fact, FactStatus, ParsedDate, ResolvedHit)

ENTITY_TYPE_MISMATCH = "ENTITY_TYPE_MISMATCH"
DECEASED = "DECEASED"
TEMPORAL_IMPOSSIBILITY = "TEMPORAL_IMPOSSIBILITY"
DOB_MISMATCH = "DOB_MISMATCH"
LOCATION_MISMATCH = "LOCATION_MISMATCH"
NAME_MISMATCH_VERIFIED = "NAME_MISMATCH_VERIFIED"
NAME_DOB_LOCATION_MATCH = "NAME_DOB_LOCATION_MATCH"
NAME_DOB_MATCH = "NAME_DOB_MATCH"
NAME_LOCATION_MATCH = "NAME_LOCATION_MATCH"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
AMBIGUOUS_CUSTOMER_RECORD = "AMBIGUOUS_CUSTOMER_RECORD"

CLEARANCE_ORDER = (ENTITY_TYPE_MISMATCH, DECEASED, TEMPORAL_IMPOSSIBILITY, DOB_MISMATCH,
                   LOCATION_MISMATCH, NAME_MISMATCH_VERIFIED)
CODE_PRIORITY = CLEARANCE_ORDER + (NAME_DOB_LOCATION_MATCH, NAME_DOB_MATCH, NAME_LOCATION_MATCH,
                                   INSUFFICIENT_DATA, AMBIGUOUS_CUSTOMER_RECORD)

# Which pending-clue fields can overturn which comparison.
_DOB_CLUES = {"dob", "age", "birth", "kyc", "incorporation"}
_NAME_CLUES = {"name", "kyc"}
_COUNTRY_CLUES = {"country", "countries", "residence", "doc_country", "kyc",
                  "nationality", "citizenship", "address", "place_of_birth", "location"}

# Why a hit is left for review (explain.py turns these into sentences).
WHY_NO_CUSTOMER = "no_customer"
WHY_P1_SR_CONFLICT = "p1_sr_conflict"
WHY_NAME_DOB_AGREE_COUNTRY_DIFFERS = "name_dob_agree_country_differs"
WHY_NOT_ENOUGH = "not_enough"
WHY_AMBIGUOUS = "ambiguous"


# ----------------------------------------------------------------------------- data

@dataclass(frozen=True)
class Finding:
    """One rule outcome with the facts and record IDs behind it."""

    code: str
    evidence: tuple[str, ...]
    facts: dict = field(default_factory=dict, hash=False, compare=True)


@dataclass(frozen=True)
class PersonView:
    """The customer facts the rules use, already normalised, with their sources."""

    record_ids: tuple[str, ...]
    entity_type: str
    dob: ParsedDate | None
    dob_source: str
    dob_verified: bool
    dob_unknown_reason: str
    verified_name: str | None
    verified_name_source: str
    names: tuple[str, ...]
    countries: dict  # canonical country -> tuple of source IDs
    pending_fields: frozenset


@dataclass(frozen=True)
class Comparison:
    """Name / date / country comparison of one person with the hit."""

    name_agrees: bool
    name_pair: tuple[str, str] | None  # (customer name, hit name) that agreed
    name_decidable: bool  # individual with >= 2 real parts (or a company with >= 1)
    dob_status: str  # dates.AGREE / CONTRADICT / UNKNOWN
    dob_specs_used: tuple[DateSpec, ...]
    p1_sr_split: bool  # decision #4: the two sources disagree about the customer
    country_overlap: tuple[str, ...]
    countries_comparable: bool
    hit_countries: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonOutcome:
    """Decision for one candidate person."""

    decision: str
    reason_code: str
    record_id: str | None
    findings: tuple[Finding, ...]
    person: PersonView | None
    comparison: Comparison | None
    why: str = ""

    @property
    def evidence(self) -> tuple[str, ...]:
        return _sorted_ids(i for f in self.findings for i in f.evidence)


@dataclass(frozen=True)
class Outcome:
    """Final decision for a hit (all candidates combined)."""

    hit_id: str
    alert_id: str
    decision: str
    reason_code: str
    customer_record_id: str | None
    evidence: tuple[str, ...]
    persons: tuple[PersonOutcome, ...]
    why: str = ""


# ----------------------------------------------------------------------------- helpers

def _sorted_ids(ids: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({i for i in ids if i}))


def _as_date(value: Any) -> ParsedDate | None:
    if isinstance(value, ParsedDate):
        return value
    if value is None:
        return None
    return _dates.parse_date(value, _dates.BANK)


def _as_country(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in _countries.ALL_COUNTRIES:
        return value
    return _countries.normalize_country(value) or _countries.country_from_location(value)


def _usable(fact: Fact | None) -> bool:
    return fact is not None and fact.status == FactStatus.VERIFIED and fact.value not in (None, "")


def _pending_fields(facts: Iterable[Fact]) -> frozenset:
    return frozenset(str(f.field).lower() for f in facts if f.status == FactStatus.PENDING_CLUE)


def person_view(person: CandidatePerson) -> PersonView:
    """Normalise a CandidatePerson: verified beats declared; conflicts become unknown."""
    verified = person.verified
    conflict_fields = {str(f.field).lower() for f in (verified.conflict_facts if verified else ())}

    dob, dob_source, dob_verified, unknown_reason = None, "", False, ""
    if verified and _usable(verified.dob) and "dob" not in conflict_fields:
        dob, dob_source, dob_verified = _as_date(verified.dob.value), verified.dob.source_id, True
    elif "dob" in conflict_fields:
        unknown_reason = "approved checks disagree on the date of birth"
    else:
        declared = {}
        for fact in person.dobs:
            if _usable(fact):
                parsed = _as_date(fact.value)
                if parsed is not None:
                    declared.setdefault(parsed.iso_str, (parsed, fact.source_id))
        if len(declared) == 1:
            dob, dob_source = next(iter(declared.values()))
        elif len(declared) > 1:
            dob, dob_source, unknown_reason = _merge_declared_dobs(tuple(declared.values()))

    verified_name, verified_name_source = None, ""
    if verified and _usable(verified.name) and "name" not in conflict_fields:
        verified_name, verified_name_source = str(verified.name.value), verified.name.source_id

    names = []
    if verified_name:
        names.append(verified_name)
    names.extend(str(f.value) for f in person.names if _usable(f))

    countries: dict[str, set] = {}
    country_facts = list(person.countries)
    if verified and _usable(verified.doc_country) and "doc_country" not in conflict_fields:
        country_facts.append(verified.doc_country)
    for fact in country_facts:
        if not _usable(fact):
            continue
        country = _as_country(fact.value)
        if country:
            countries.setdefault(country, set()).add(fact.source_id)

    pending = _pending_fields(person.pending_clues) | frozenset(
        conflict_fields & (_COUNTRY_CLUES | _NAME_CLUES | _DOB_CLUES))
    return PersonView(
        record_ids=tuple(person.record_ids),
        entity_type=person.entity_type or "",
        dob=dob,
        dob_source=dob_source,
        dob_verified=dob_verified,
        dob_unknown_reason=unknown_reason,
        verified_name=verified_name,
        verified_name_source=verified_name_source,
        names=tuple(dict.fromkeys(names)),
        countries={k: tuple(sorted(v)) for k, v in sorted(countries.items())},
        pending_fields=pending,
    )


def _merge_declared_dobs(dates: tuple[tuple[ParsedDate, str], ...]) -> tuple[ParsedDate | None, str, str]:
    """One declared birth date, or unknown when the records really disagree.

    A year (or year-month) that agrees with a fuller date of the same calendar value is
    one date: keep the most specific. Adjacent years and real contradictions stay unknown.
    """
    values = [item[0] for item in dates]
    if any(_dates.compare_dates(left, right) != _dates.AGREE
           for index, left in enumerate(values) for right in values[index + 1:]):
        return None, "", "the customer's records disagree on the date of birth"

    def rank(item: tuple[ParsedDate, str]) -> tuple[bool, bool]:
        parsed, _source = item
        return (parsed.day is not None, parsed.month is not None)

    parsed, source = max(dates, key=rank)
    return parsed, source, ""


def canonical_hit_countries(values: Iterable[Any]) -> tuple[str, ...]:
    """Canonical countries tied to a hit. Regions and unreadable tokens are dropped."""
    found = set()
    for raw in values:
        country = _countries.country_from_location(raw)
        if country:
            found.add(country)
    return tuple(sorted(found))


def _decidable_name(name: str, entity_type: str, variants: _names.NameVariants) -> bool:
    need = 1 if entity_type == ENTITY_COMPANY else 2
    return _names.real_part_count(name, entity_type, variants) >= need


def compare(person: PersonView, hit: ResolvedHit, variants: _names.NameVariants) -> Comparison:
    """Name, date-of-birth and country comparison of one person with one hit."""
    entity = person.entity_type or hit.entity_type
    hit_all_names = tuple(hit.names) + tuple(hit.aliases)
    decidable_names = [n for n in person.names if _decidable_name(n, entity, variants)]
    found = _names.best_agreement(decidable_names, hit_all_names, entity, variants)
    name_pair = (found[0], found[1]) if found else None

    hit_dob_pending = bool(_pending_fields(hit.pending_clues) & _DOB_CLUES)
    specs = tuple(hit.dob_specs)
    p1_sr_split = False
    if person.dob is None or not specs or hit_dob_pending or (person.pending_fields & _DOB_CLUES):
        status = _dates.UNKNOWN
    elif hit.p1_kv_sr_conflict:
        kv = tuple(s for s in specs if s.origin != "sr")
        sr = tuple(s for s in specs if s.origin == "sr")
        a, b = _dates.compare_dob(person.dob, kv), _dates.compare_dob(person.dob, sr)
        if a == b:
            status = a
        else:
            status, p1_sr_split = _dates.UNKNOWN, True
    else:
        status = _dates.compare_dob(person.dob, specs)

    hit_countries = canonical_hit_countries(hit.countries)
    overlap = tuple(sorted(set(person.countries) & set(hit_countries)))
    comparable = bool(person.countries) and bool(hit_countries)
    return Comparison(bool(found), name_pair, bool(decidable_names), status, specs, p1_sr_split,
                      overlap, comparable, hit_countries)


def _dob_evidence(person: PersonView, specs: Iterable[DateSpec], hit: ResolvedHit) -> tuple[str, ...]:
    # Search results are cited by their own id. A date read from the hit profile is the hit
    # itself, which the output already names via hit_id, so that id is not repeated.
    extra = [s.source_id for s in specs if s.origin == "sr" or (s.source_id and s.source_id != hit.hit_id)]
    return _sorted_ids([person.dob_source, *extra])


def _agreement_evidence(person: PersonView, cmp: Comparison, hit: ResolvedHit, *, countries: Iterable[str] | None) -> tuple[str, ...]:
    """Records that decided a match or a name-and-date review: identity, birth, and countries."""
    return _sorted_ids([*person.record_ids, *_dob_evidence(person, cmp.dob_specs_used, hit),
                        person.verified_name_source, *_country_evidence(person, countries)])


def _country_evidence(person: PersonView, countries: Iterable[str] | None = None) -> tuple[str, ...]:
    chosen = person.countries if countries is None else {c: person.countries.get(c, ()) for c in countries}
    return _sorted_ids(i for ids in chosen.values() for i in ids)


# ----------------------------------------------------------------------------- one person

def decide_person(person: CandidatePerson | PersonView, hit: ResolvedHit, screened_at: Any,
                  variants: _names.NameVariants = _names.EMPTY_VARIANTS) -> PersonOutcome:
    """Apply the SOP to one candidate person."""
    view = person if isinstance(person, PersonView) else person_view(person)
    record_id = view.record_ids[0] if view.record_ids else None
    cmp = compare(view, hit, variants)
    screened = _as_date(screened_at)
    findings: list[Finding] = []

    # 1. type
    if view.entity_type and hit.entity_type and view.entity_type != hit.entity_type:
        findings.append(Finding(ENTITY_TYPE_MISMATCH, (), {"customer_type": view.entity_type, "hit_type": hit.entity_type}))
    # 2. death before screening (does not depend on the customer)
    if hit.deceased_date is not None and _dates.certainly_before(hit.deceased_date, screened):
        findings.append(Finding(DECEASED, (), {"deceased": hit.deceased_date, "screened_at": screened}))
    # 3. time
    hit_pending = _pending_fields(hit.pending_clues)
    name_clue = bool(view.pending_fields & _NAME_CLUES) or bool(hit_pending & _NAME_CLUES)
    country_clue = bool(view.pending_fields & _COUNTRY_CLUES) or bool(hit_pending & _COUNTRY_CLUES)
    if view.dob is not None and hit.earliest_report_date is not None and not (view.pending_fields & _DOB_CLUES):
        report = hit.earliest_report_date
        if view.entity_type == ENTITY_COMPANY:
            if _dates.certainly_after(view.dob, report):
                findings.append(Finding(TEMPORAL_IMPOSSIBILITY, _sorted_ids([view.dob_source]),
                                        {"birth": view.dob, "report": report, "company": True}))
        elif _dates.certainly_younger_than(view.dob, report, 16):
            findings.append(Finding(TEMPORAL_IMPOSSIBILITY, _sorted_ids([view.dob_source]),
                                    {"birth": view.dob, "report": report, "company": False,
                                     "max_age": max(0, report.year - view.dob.year)}))
    # 4/5. date of birth
    if cmp.dob_status == _dates.CONTRADICT:
        findings.append(Finding(DOB_MISMATCH, _dob_evidence(view, cmp.dob_specs_used, hit),
                                {"customer_dob": view.dob, "source": view.dob_source, "specs": cmp.dob_specs_used}))
    # 6. location (never against a name + date-of-birth agreement, nor when a pending
    # name or birth clue could still produce that agreement)
    name_dob_agree = cmp.name_agrees and cmp.dob_status == _dates.AGREE
    identity_unresolved = ((cmp.name_agrees and bool(view.pending_fields & _DOB_CLUES or hit_pending & _DOB_CLUES))
                           or (cmp.dob_status == _dates.AGREE and name_clue))
    if (cmp.countries_comparable and not cmp.country_overlap and not name_dob_agree
            and not country_clue and not identity_unresolved):
        findings.append(Finding(LOCATION_MISMATCH, _sorted_ids(list(view.record_ids) + list(_country_evidence(view))),
                                {"customer_countries": tuple(view.countries), "hit_countries": cmp.hit_countries}))
    # 7. verified name
    entity = view.entity_type or hit.entity_type
    if (view.verified_name and not name_clue and _decidable_name(view.verified_name, entity, variants)
            and _names.best_agreement([view.verified_name], tuple(hit.names) + tuple(hit.aliases), entity, variants) is None):
        findings.append(Finding(NAME_MISMATCH_VERIFIED, _sorted_ids([view.verified_name_source]),
                                {"verified_name": view.verified_name, "source": view.verified_name_source,
                                 "hit_name": (hit.names or ("",))[0]}))

    if findings:
        findings.sort(key=lambda f: CLEARANCE_ORDER.index(f.code))
        return PersonOutcome(DECISION_DIFFERENT, findings[0].code, record_id, tuple(findings), view, cmp)

    if cmp.p1_sr_split:
        return PersonOutcome(DECISION_REVIEW, INSUFFICIENT_DATA, record_id,
                             (Finding(INSUFFICIENT_DATA, _dob_evidence(view, cmp.dob_specs_used, hit),
                                      {"customer_dob": view.dob, "source": view.dob_source, "specs": cmp.dob_specs_used}),),
                             view, cmp, WHY_P1_SR_CONFLICT)
    if name_dob_agree and cmp.countries_comparable and not cmp.country_overlap:
        return PersonOutcome(DECISION_REVIEW, INSUFFICIENT_DATA, record_id,
                             (Finding(INSUFFICIENT_DATA, _agreement_evidence(view, cmp, hit, countries=None),
                                      {"customer_countries": tuple(view.countries), "hit_countries": cmp.hit_countries}),),
                             view, cmp, WHY_NAME_DOB_AGREE_COUNTRY_DIFFERS)

    # 8. confirmation. A pending legal name can still overturn it. An unknown customer
    # type is not confirmed as the hit's company. A pending incorporation date can still
    # make a company too young for the earliest dated report.
    name_ready = not name_clue
    if view.entity_type == ENTITY_COMPANY:
        incorporation_open = bool(view.pending_fields & _DOB_CLUES) and hit.earliest_report_date is not None
        if cmp.name_agrees and cmp.country_overlap and not country_clue and name_ready and not incorporation_open:
            evidence = _agreement_evidence(view, cmp, hit, countries=cmp.country_overlap)
            return PersonOutcome(DECISION_SAME, NAME_LOCATION_MATCH, record_id,
                                 (Finding(NAME_LOCATION_MATCH, evidence, {"overlap": cmp.country_overlap}),), view, cmp)
    elif name_dob_agree and name_ready and not (not view.entity_type and hit.entity_type == ENTITY_COMPANY):
        evidence = _agreement_evidence(view, cmp, hit, countries=cmp.country_overlap)
        code = NAME_DOB_LOCATION_MATCH if cmp.country_overlap else NAME_DOB_MATCH
        return PersonOutcome(DECISION_SAME, code, record_id,
                             (Finding(code, evidence, {"overlap": cmp.country_overlap}),), view, cmp)

    # 9. nothing settles it: cite what was compared (the shared countries' sources)
    evidence = _country_evidence(view, cmp.country_overlap) or _sorted_ids(view.record_ids[:1])
    return PersonOutcome(DECISION_REVIEW, INSUFFICIENT_DATA, record_id,
                         (Finding(INSUFFICIENT_DATA, evidence, {"overlap": cmp.country_overlap}),), view, cmp, WHY_NOT_ENOUGH)


# ----------------------------------------------------------------------------- whole hit

def _no_customer(hit: ResolvedHit, screened_at: Any, screened_type: str | None) -> PersonOutcome:
    findings = []
    if screened_type and hit.entity_type and screened_type != hit.entity_type:
        findings.append(Finding(ENTITY_TYPE_MISMATCH, (), {"customer_type": screened_type, "hit_type": hit.entity_type}))
    screened = _as_date(screened_at)
    if hit.deceased_date is not None and _dates.certainly_before(hit.deceased_date, screened):
        findings.append(Finding(DECEASED, (), {"deceased": hit.deceased_date, "screened_at": screened}))
    if findings:
        return PersonOutcome(DECISION_DIFFERENT, findings[0].code, None, tuple(findings), None, None)
    return PersonOutcome(DECISION_REVIEW, INSUFFICIENT_DATA, None, (Finding(INSUFFICIENT_DATA, ()),), None, None, WHY_NO_CUSTOMER)


def decide(hit: ResolvedHit, candidates: Iterable[CandidatePerson], screened_at: Any,
           variants: _names.NameVariants = _names.EMPTY_VARIANTS, screened_entity_type: str | None = None) -> Outcome:
    """Decide one hit against every candidate person for its alert (POLICY 3)."""
    candidates = tuple(candidates)
    if not candidates:
        only = _no_customer(hit, screened_at, screened_entity_type)
        return Outcome(hit.hit_id, hit.alert_id, only.decision, only.reason_code, None, only.evidence, (only,), only.why)

    outcomes = tuple(decide_person(c, hit, screened_at, variants) for c in candidates)
    if len(outcomes) == 1:
        only = outcomes[0]
        return Outcome(hit.hit_id, hit.alert_id, only.decision, only.reason_code, only.record_id, only.evidence,
                       outcomes, only.why)

    decisions = {o.decision for o in outcomes}
    evidence = _sorted_ids(i for o in outcomes for i in o.evidence)
    if len(decisions) == 1:
        decision = outcomes[0].decision
        counts = Counter(o.reason_code for o in outcomes)
        code = sorted(counts, key=lambda c: (-counts[c], CODE_PRIORITY.index(c)))[0]
        why = outcomes[0].why if all(o.why == outcomes[0].why for o in outcomes) else ""
        return Outcome(hit.hit_id, hit.alert_id, decision, code, AMBIGUOUS_CUSTOMER_ID, evidence, outcomes, why)
    record_ids = _sorted_ids(r for o in outcomes for r in (o.person.record_ids if o.person else ()))
    return Outcome(hit.hit_id, hit.alert_id, DECISION_REVIEW, AMBIGUOUS_CUSTOMER_RECORD, AMBIGUOUS_CUSTOMER_ID,
                   _sorted_ids(list(record_ids) + list(evidence)), outcomes, WHY_AMBIGUOUS)


__all__ = [
    "AMBIGUOUS_CUSTOMER_RECORD", "CLEARANCE_ORDER", "Comparison", "Finding", "Outcome", "PersonOutcome",
    "PersonView", "compare", "decide", "decide_person", "person_view",
]
