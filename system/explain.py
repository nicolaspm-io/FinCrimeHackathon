"""Turn a decision Outcome into the 7-field output line (POLICY principle 9).

The reason states the decisive facts with this hit's own values and the IDs they came from,
in the wording of the practice labels' reference reasons, e.g.
  "The customer's date of birth is 1979-11-08 (NOTE-8548C1) but the hit's profile notes put
   its birth year at 1976 or 1977."
  "The customer is linked only to Netherlands but the hit is linked only to United States."
Every applicable contradiction gets one sentence (at most four). A REVIEW says what agrees
and what is missing. Evidence is the decision's minimal record IDs, deduplicated and sorted.
"""
from __future__ import annotations

from typing import Iterable

try:
    from . import dates as _dates
    from . import decision as D
    from .contracts import (DECISION_DIFFERENT, DECISION_REVIEW, DECISION_SAME, ENTITY_COMPANY, DateSpec,
                            HitDecision, ParsedDate, ResolvedHit)
except ImportError:
    import dates as _dates
    import decision as D
    from contracts import (DECISION_DIFFERENT, DECISION_REVIEW, DECISION_SAME, ENTITY_COMPANY, DateSpec,
                           HitDecision, ParsedDate, ResolvedHit)

MAX_SENTENCES = 4


# ----------------------------------------------------------------------------- phrases

def join_list(items: Iterable[str]) -> str:
    """'A', 'A, B' (the reference reasons list countries with commas)."""
    return ", ".join(items)


def _iso(d: ParsedDate | None) -> str:
    return d.iso_str if d is not None else "unknown"


def _with_source(text: str, source: str) -> str:
    return f"{text} ({source})" if source else text


def hit_dates_phrase(specs: Iterable[DateSpec]) -> tuple[str, str]:
    """(where, value) for the hit's birth dates, e.g. ('listed date of birth is', '1962-09-10'),
    ('profile notes put its birth year at', '1976 or 1977'), ('search results put ...', ...)."""
    specs = tuple(specs)
    if not specs:
        return "", ""
    origins = {s.origin for s in specs}
    if origins == {"hit_dobs"}:
        values = list(dict.fromkeys(a.iso_str for s in specs for a in _dates.spec_alternatives(s)))
        verb = "is" if len(values) == 1 else "are"
        noun = "date of birth" if len(values) == 1 else "dates of birth"
        return f"listed {noun} {verb}", ", ".join(values)
    value = _dates.describe_years(specs)
    exact = all(s.exact_dates and all(d.month for d in s.exact_dates) for s in specs)
    if origins == {"sr"}:
        source = "search results"
    elif origins <= {"p1_prose", "p1_kv"}:
        source = "profile notes"
    else:
        source = "profile notes and search results"
    if exact:
        return f"{source} give its date of birth as", value
    return f"{source} put its birth year at", value


def hit_dates_short(specs: Iterable[DateSpec]) -> str:
    """Short form used in agreements: '1994, 1995' for listed dates, '1967 or 1968' otherwise."""
    specs = tuple(specs)
    if specs and {s.origin for s in specs} == {"hit_dobs"}:
        return ", ".join(dict.fromkeys(a.iso_str for s in specs for a in _dates.spec_alternatives(s)))
    return _dates.describe_years(specs)


def _hit_name(hit: ResolvedHit, outcome_person: D.PersonOutcome | None = None) -> str:
    if outcome_person and outcome_person.comparison and outcome_person.comparison.name_pair:
        return outcome_person.comparison.name_pair[1]
    return (hit.names or hit.aliases or ("the listed name",))[0]


def _article(entity_type: str) -> str:
    return "a company" if entity_type == ENTITY_COMPANY else "an individual"


# ----------------------------------------------------------------------------- sentences

def finding_sentence(finding: D.Finding, hit: ResolvedHit) -> str:
    f = finding.facts
    code = finding.code
    if code == D.ENTITY_TYPE_MISMATCH:
        return f"The customer is {_article(f['customer_type'])} but the hit is {_article(f['hit_type'])}."
    if code == D.DECEASED:
        return (f"The hit's profile gives a date of death of {_iso(f['deceased'])}, "
                f"before the customer was screened on {_iso(f['screened_at'])}.")
    if code == D.TEMPORAL_IMPOSSIBILITY:
        source = finding.evidence[0] if finding.evidence else ""
        if f.get("company"):
            return (f"The customer was incorporated on {_iso(f['birth'])}{' (' + source + ')' if source else ''}, "
                    f"after the hit's profile reports activity on {_iso(f['report'])}.")
        born = f"born in {f['birth'].year}"
        return (f"The customer was {born}{' (' + source + ')' if source else ''}, so they were at most "
                f"{f['max_age']} when the hit's profile reports activity on {_iso(f['report'])}.")
    if code == D.DOB_MISMATCH:
        where, value = hit_dates_phrase(f["specs"])
        return (f"The customer's date of birth is {_with_source(_iso(f['customer_dob']), f['source'])} "
                f"but the hit's {where} {value}.")
    if code == D.LOCATION_MISMATCH:
        return (f"The customer is linked only to {join_list(f['customer_countries'])} "
                f"but the hit is linked only to {join_list(f['hit_countries'])}.")
    if code == D.NAME_MISMATCH_VERIFIED:
        return (f"The customer's verified name is {_with_source(f['verified_name'], f['source'])}, "
                f"which does not match the hit's name {f['hit_name'] or _hit_name(hit)}.")
    return ""


def _agreement_sentences(person: D.PersonOutcome, hit: ResolvedHit, with_dob: bool = True) -> list[str]:
    view, cmp = person.person, person.comparison
    out = []
    if with_dob and cmp and cmp.dob_status == _dates.AGREE and view and view.dob is not None:
        out.append(f"The customer's date of birth {_with_source(view.dob.iso_str, view.dob_source)} "
                   f"agrees with the hit's {hit_dates_short(cmp.dob_specs_used)}.")
    if cmp and cmp.country_overlap:
        out.append(f"Both the customer and the hit are linked to {join_list(cmp.country_overlap)}.")
    if cmp and cmp.name_agrees:
        out.append(f"The customer's name agrees with the hit's name {_hit_name(hit, person)}.")
    return out


def _missing_clause(person: D.PersonOutcome, hit: ResolvedHit) -> str:
    view, cmp = person.person, person.comparison
    missing = []
    if cmp is None or view is None:
        return "nothing about the customer could be compared"
    if cmp.dob_status != _dates.AGREE and view.entity_type != ENTITY_COMPANY:
        if not hit.dob_specs:
            missing.append("the date of birth cannot be compared with it")
        elif view.dob is None:
            missing.append(view.dob_unknown_reason or "the customer's date of birth is not known")
        elif hit.pending_clues:
            missing.append("the hit's birth information could not be read reliably")
        else:
            missing.append("the dates of birth neither clearly agree nor clearly differ")
    if not cmp.name_agrees:
        if not cmp.name_decidable:
            missing.append("the customer's name has too few real parts to compare")
        else:
            missing.append("the names do not clearly agree")
    if not cmp.countries_comparable:
        missing.append("the countries cannot be compared")
    elif not cmp.country_overlap and view.entity_type == ENTITY_COMPANY:
        missing.append("no country is shared")
    if not missing:
        missing.append("the facts are not enough to confirm it")
    if len(missing) == 1:
        return missing[0]
    return ", ".join(missing[:-1]) + " and " + missing[-1]


def review_sentences(outcome: D.Outcome, hit: ResolvedHit) -> list[str]:
    person = outcome.persons[0] if outcome.persons else None
    why = outcome.why or (person.why if person else "")
    if why == D.WHY_NO_CUSTOMER or person is None or person.person is None:
        return ["No customer record could be found for this alert.",
                "Nothing contradicts the hit without the customer's records, so the hit can be neither cleared nor confirmed."]
    if outcome.reason_code == D.AMBIGUOUS_CUSTOMER_RECORD:
        parts = []
        for p in outcome.persons:
            ids = ", ".join(p.person.record_ids) if p.person else "unknown"
            parts.append(f"{ids} ({p.decision}/{p.reason_code})")
        return [f"{len(outcome.persons)} different customers fit the alert: {'; '.join(parts)}.",
                "They lead to different decisions, so the hit is left for review."]
    if why == D.WHY_P1_SR_CONFLICT:
        view, cmp = person.person, person.comparison
        kv = [s for s in cmp.dob_specs_used if s.origin != "sr"]
        sr = [s for s in cmp.dob_specs_used if s.origin == "sr"]
        return [f"The hit's profile notes give its date of birth as {_dates.describe_years(kv)} "
                f"while its search results put its birth year at {_dates.describe_years(sr)}.",
                f"Against the customer's date of birth {_with_source(_iso(view.dob), view.dob_source)} one source agrees "
                "and the other does not, so the hit can be neither cleared nor confirmed."]
    if why == D.WHY_NAME_DOB_AGREE_COUNTRY_DIFFERS:
        view, cmp = person.person, person.comparison
        return [f"The customer's name and date of birth {_with_source(_iso(view.dob), view.dob_source)} agree with the hit.",
                f"The customer is linked only to {join_list(view.countries)} but the hit is linked only to "
                f"{join_list(hit.countries)}.",
                "A country difference does not clear a hit whose name and date of birth agree, so it is left for review."]
    sentences = _agreement_sentences(person, hit)
    sentences.append(f"Nothing in the customer's records contradicts the hit, and {_missing_clause(person, hit)}, "
                     "so the hit can be neither cleared nor confirmed.")
    return sentences[-MAX_SENTENCES:]


def reason_text(outcome: D.Outcome, hit: ResolvedHit) -> str:
    """2-4 factual sentences (a single sentence for a single clear contradiction)."""
    if outcome.decision == DECISION_DIFFERENT:
        person = outcome.persons[0]
        sentences = [finding_sentence(f, hit) for f in person.findings]
        sentences = [s for s in dict.fromkeys(sentences) if s]
        return " ".join(sentences[:MAX_SENTENCES])
    if outcome.decision == DECISION_SAME:
        person = outcome.persons[0]
        sentences = _agreement_sentences(person, hit)
        if outcome.reason_code == D.NAME_DOB_MATCH:
            sentences.append("The customer's countries cannot be matched with the hit's, but nothing contradicts the match.")
        return " ".join(sentences[:MAX_SENTENCES])
    return " ".join(review_sentences(outcome, hit)[:MAX_SENTENCES])


def to_hit_decision(outcome: D.Outcome, hit: ResolvedHit) -> HitDecision:
    """The output-schema record for one hit."""
    reason = reason_text(outcome, hit).strip() or "The evidence does not settle this hit, so it is left for review."
    return HitDecision(
        hit_id=outcome.hit_id,
        alert_id=outcome.alert_id,
        decision=outcome.decision,
        reason_code=outcome.reason_code,
        customer_record_id=outcome.customer_record_id,
        reason=reason,
        evidence=tuple(sorted(set(outcome.evidence))),
    )


__all__ = ["DECISION_REVIEW", "hit_dates_phrase", "hit_dates_short", "reason_text", "to_hit_decision"]
