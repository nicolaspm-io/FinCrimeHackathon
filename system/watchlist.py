"""Watchlist hit resolution: everything the decision rules need to know about one hit.

POLICY 5 cascade for an individual's birth date:
  1. the `dobs` field, if it has any value;
  2. otherwise paragraph 1 of `profile_notes` (birth date, age at a date, or a key-value line);
  3. otherwise search results for the hit's name that carry the hit's listing phrase and name
     a country tied to the hit.
For a company, only `dobs` is an incorporation date.

Decision #4 (FINAL_PLAN): when paragraph 1 gives a key-value "DOB:" line AND an attributed
search result also gives a birth year (H-2C6B2E / H-FE02A6), both are kept and
`p1_kv_sr_conflict` is set. The decision engine then compares the customer with each source
separately: both contradict -> DOB_MISMATCH, both agree -> agreement, otherwise REVIEW.

Also: names and aliases, countries from all five sources, the earliest dated report, the
date of death (only from a non-null `deceased_date`), the search results used, and pending
clues (text that looks like birth information but could not be read safely).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from . import countries as _countries
    from . import dates as _dates
    from . import profile_text as _text
    from .contracts import ENTITY_COMPANY, DateSpec, Fact, LLMFallback, ParsedDate, ResolvedHit
except ImportError:
    import countries as _countries
    import dates as _dates
    import profile_text as _text
    from contracts import ENTITY_COMPANY, DateSpec, Fact, LLMFallback, ParsedDate, ResolvedHit


@dataclass(frozen=True)
class HitDetail:
    """Extra, explanation-only facts about a resolved hit (not part of the frozen contract)."""

    listing_phrase: str
    country_sources: tuple[tuple[str, str, str], ...]
    dated_reports: tuple[tuple[ParsedDate, str], ...]
    dob_origin: str  # "hit_dobs" | "p1_prose" | "p1_kv" | "sr" | "p1_kv+sr" | ""
    sr_considered: tuple[str, ...] = ()
    sr_attributed: tuple[str, ...] = ()
    trace: tuple[str, ...] = field(default_factory=tuple)


def hit_names(hit: dict[str, Any]) -> tuple[str, ...]:
    """The hit's own names: `name`, `best_matching_name` and first/middle/last joined."""
    out = []
    for value in (hit.get("name"), hit.get("best_matching_name")):
        if value and str(value).strip():
            out.append(str(value).strip())
    joined = " ".join(str(hit.get(k)).strip() for k in ("first_name", "middle_name", "last_name") if hit.get(k))
    if joined:
        out.append(joined)
    return tuple(dict.fromkeys(out))


def hit_aliases(hit: dict[str, Any]) -> tuple[str, ...]:
    out = []
    for alias in hit.get("aliases") or []:
        if isinstance(alias, dict):
            alias = alias.get("name") or alias.get("alias")
        if alias and str(alias).strip():
            out.append(str(alias).strip())
    return tuple(dict.fromkeys(out))


def _dobs_specs(hit: dict[str, Any]) -> tuple[DateSpec, ...]:
    specs = []
    for raw in hit.get("dobs") or []:
        spec = _dates.spec_from_raw(raw, hit["hit_id"], _text.ORIGIN_DOBS, _dates.HIT)
        if spec is not None:
            specs.append(spec)
    return tuple(specs)


def _deceased(hit: dict[str, Any]) -> ParsedDate | None:
    """Date of death from `deceased_date` only; an ambiguous value keeps its LATEST reading,
    so 'died before screening' is only concluded when every reading says so."""
    alternatives = _dates.parse_date_alternatives(hit.get("deceased_date"), _dates.HIT)
    if not alternatives:
        return None
    return max(alternatives, key=lambda d: _dates.latest(d))


def _search_results_for(hit: dict[str, Any], dataset: Any) -> tuple[dict[str, Any], ...]:
    if dataset is None:
        return ()
    getter = getattr(dataset, "get_search_results", None)
    results = getter(hit.get("name")) if getter else ()
    return tuple(sorted(results or (), key=lambda r: r.get("result_id", "")))


def resolve_hit(hit: dict[str, Any], dataset: Any = None, llm: LLMFallback | None = None,
                search_results: Iterable[dict[str, Any]] | None = None) -> tuple[ResolvedHit, HitDetail]:
    """Resolve one hit. `dataset` (system/data.py ScreeningDataset) supplies search results;
    `search_results` overrides it (tests). `llm` is consulted only for pending clues."""
    hit_id, alert_id = hit["hit_id"], hit.get("alert_id", "")
    entity_type = hit.get("entity_type") or ""
    names = hit_names(hit)
    aliases = hit_aliases(hit)
    sources = _countries.hit_country_sources(hit)
    countries = tuple(sorted({c for c, _, _ in sources}))
    phrase = _text.listing_phrase(hit)
    reports = tuple(_text.dated_reports(hit.get("profile_notes")))
    earliest = min((d for d, _ in reports), key=lambda d: _dates.earliest(d)) if reports else None
    deceased = _deceased(hit)

    trace: list[str] = []
    pending: list[Fact] = []
    sr_used: list[str] = []
    considered: list[str] = []
    attributed: list[str] = []
    conflict = False
    origin = ""

    specs = _dobs_specs(hit)
    if specs:
        origin = _text.ORIGIN_DOBS
        trace.append(f"dobs field: {', '.join(map(str, hit.get('dobs') or []))}")
    elif entity_type != ENTITY_COMPANY:
        p1 = _text.first_paragraph(hit.get("profile_notes"))
        p1_finding = _text.parse_p1(p1, hit_id, names + aliases)
        p1_finding = _text.resolve_pending_p1(p1_finding, p1, hit_id, llm)
        trace.extend(p1_finding.notes)

        sr_specs: list[DateSpec] = []
        sr_pending: list[Fact] = []
        results = (tuple(sorted(search_results, key=lambda r: r.get("result_id", "")))
                   if search_results is not None else _search_results_for(hit, dataset))
        for result in results:
            considered.append(result.get("result_id", ""))
            facts = _text.attribute_search_result(result, phrase, countries)
            if not facts.attributed:
                continue
            facts = _text.resolve_pending_sr(facts, str(result.get("snippet") or ""), phrase, countries, llm)
            attributed.append(facts.result_id)
            if facts.finding.found:
                sr_specs.extend(facts.finding.specs)
                sr_used.append(facts.result_id)
                trace.extend(facts.finding.notes)
            sr_pending.extend(facts.finding.pending)

        kv_specs = tuple(s for s in p1_finding.specs if s.origin == _text.ORIGIN_P1_KV)
        if p1_finding.found:
            specs = p1_finding.specs
            origin = specs[0].origin
            pending.extend(p1_finding.pending)
            if kv_specs and sr_specs:
                # Decision #4: keep both sources; the decision engine compares them separately.
                specs = tuple(specs) + tuple(sr_specs)
                conflict = True
                origin = "p1_kv+sr"
                trace.append("p1 key-value DOB and attributed search results both give birth information")
            else:
                sr_used = []
        else:
            pending.extend(p1_finding.pending)
            if sr_specs:
                specs = tuple(sr_specs)
                origin = _text.ORIGIN_SR
            pending.extend(sr_pending)
    resolved = ResolvedHit(
        hit_id=hit_id,
        alert_id=alert_id,
        entity_type=entity_type,
        names=names,
        aliases=aliases,
        dob_specs=tuple(specs),
        countries=countries,
        earliest_report_date=earliest,
        deceased_date=deceased,
        sr_used_ids=tuple(dict.fromkeys(sr_used)),
        p1_kv_sr_conflict=conflict,
        pending_clues=tuple(pending),
    )
    detail = HitDetail(phrase, sources, reports, origin, tuple(considered), tuple(attributed), tuple(trace))
    return resolved, detail
