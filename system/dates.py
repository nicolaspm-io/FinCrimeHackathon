"""Date parsing, partial-date comparison, ages and temporal checks (POLICY principles 5-7).

Two parsing modes, because the two sides write slash dates differently:
  - BANK ("bank_internal"): customers, accounts, kyc_checks, notes, alerts. Every slash/dot
    date in dev is DD/MM/YYYY (9,909 have a first part > 12, none a second part > 12).
  - HIT ("hit_provider"): watchlist profiles "written as the provider wrote them". A part > 12
    decides the order; when both parts are <= 12 BOTH readings are kept (MM/DD and DD/MM).

Supported shapes (case-insensitive, accents and ordinals tolerated):
  2004-05-19  2004-05  2004  2004/05/19  2004.05.19
  19/05/2004  19.05.2004  19-05-2004  (2-digit years are NOT parsed: century unknown)
  19 May 2004  19th of May, 2004  May 19, 2004  May 19 2004  May 2004  Sept. 2004

Comparison semantics (POLICY 6/7), applied per alternative of a hit DateSpec:
  CONTRADICT  |year diff| > 1, or same year and both months known and different, or same year
              and month and both days known and different
  AGREE       same year and every part both sides give is equal
  UNKNOWN     anything else (adjacent year, or nothing comparable)
A hit's dates are contradicted only when EVERY alternative is; one agreeing alternative is
enough to agree. Missing parts are never a contradiction.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re
import unicodedata
from typing import Any, Iterable

try:
    from .contracts import DateSpec, ParsedDate
except ImportError:  # flat import when system/ is on sys.path
    from contracts import DateSpec, ParsedDate

BANK = "bank_internal"
HIT = "hit_provider"
MODES = (BANK, HIT)

AGREE = "agree"
CONTRADICT = "contradict"
UNKNOWN = "unknown"

MIN_YEAR, MAX_YEAR = 1800, 2100

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
MONTHS.update({name[:3]: num for name, num in list(MONTHS.items())})
MONTHS.update({"sept": 9})
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY_RE = r"(?:[12]\d|3[01]|0?[1-9])(?:st|nd|rd|th)?"
_YEAR_RE = r"(?:1[89]\d\d|20\d\d|2100)"

# Patterns used both for whole values (anchored) and for scanning free text.
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ymd", rf"(?P<y>{_YEAR_RE})[-/.](?P<m>0?[1-9]|1[0-2])[-/.](?P<d>0?[1-9]|[12]\d|3[01])"),
    ("ym", rf"(?P<y>{_YEAR_RE})-(?P<m>0[1-9]|1[0-2])(?![-/.]?\d)"),
    ("slash", rf"(?P<a>\d{{1,2}})(?P<sep>[/.\-])(?P<b>\d{{1,2}})(?P=sep)(?P<y>{_YEAR_RE})"),
    ("d_mon_y", rf"(?P<d>{_DAY_RE})\s+(?:of\s+)?(?P<mon>{_MONTH_RE})[\s,]+(?P<y>{_YEAR_RE})"),
    ("mon_d_y", rf"(?P<mon>{_MONTH_RE})\s+(?P<d>{_DAY_RE}),?\s+(?P<y>{_YEAR_RE})"),
    ("mon_y", rf"(?P<mon>{_MONTH_RE}),?\s+(?:of\s+)?(?P<y>{_YEAR_RE})"),
    ("y", rf"(?P<y>{_YEAR_RE})"),
)
_WHOLE = tuple((kind, re.compile(rf"^\s*{pat}\s*$", re.I)) for kind, pat in _PATTERNS)
def _prefixed(kind: str, pattern: str) -> str:
    """Rename a pattern's groups to `<kind>_<name>` so all shapes fit in one scanner."""
    pattern = re.sub(r"\(\?P<(\w+)>", lambda m: f"(?P<{kind}_{m.group(1)}>", pattern)
    return re.sub(r"\(\?P=(\w+)\)", lambda m: f"(?P={kind}_{m.group(1)})", pattern)


# One alternation, longest shapes first; each shape is wrapped in a group named after it.
_SCAN = re.compile(
    "|".join(rf"(?P<{kind}>(?<![\w/.\-]){_prefixed(kind, pat)}(?![\w/]|[.\-]\d))" for kind, pat in _PATTERNS),
    re.I,
)


# ----------------------------------------------------------------------------- parsing

def _clean(raw: Any) -> str:
    text = unicodedata.normalize("NFKC", str(raw))
    return re.sub(r"\s+", " ", text).strip()


def _valid(year: int, month: int | None, day: int | None) -> bool:
    if not MIN_YEAR <= year <= MAX_YEAR:
        return False
    if month is None:
        return day is None
    if not 1 <= month <= 12:
        return False
    if day is None:
        return True
    return 1 <= day <= calendar.monthrange(year, month)[1]


def _make(year: int, month: int | None, day: int | None, raw: str) -> ParsedDate | None:
    return ParsedDate(year, month, day, raw) if _valid(year, month, day) else None


def _month(text: str) -> int:
    return MONTHS[text.lower().rstrip(".")[:3]]


def _day(text: str) -> int:
    return int(re.match(r"\d+", text).group(0))


def _from_groups(kind: str, get, raw: str, mode: str) -> tuple[ParsedDate, ...]:
    """Build the alternatives for one matched shape. `get(name)` returns a group or None."""
    year = int(get("y"))
    if kind == "y":
        out = [_make(year, None, None, raw)]
    elif kind == "ym":
        out = [_make(year, int(get("m")), None, raw)]
    elif kind == "ymd":
        out = [_make(year, int(get("m")), int(get("d")), raw)]
    elif kind in ("d_mon_y", "mon_d_y"):
        out = [_make(year, _month(get("mon")), _day(get("d")), raw)]
    elif kind == "mon_y":
        out = [_make(year, _month(get("mon")), None, raw)]
    else:  # slash / dot / dash with the year last
        a, b = int(get("a")), int(get("b"))
        if mode == BANK:
            out = [_make(year, b, a, raw)]  # DD/MM/YYYY
        elif a > 12:
            out = [_make(year, b, a, raw)]
        elif b > 12:
            out = [_make(year, a, b, raw)]
        elif a == b:
            out = [_make(year, a, b, raw)]
        else:  # both <= 12: keep both readings, MM/DD first (most provider dates in dev)
            out = [_make(year, a, b, raw), _make(year, b, a, raw)]
    return tuple(d for d in out if d is not None)


def parse_date_alternatives(raw: Any, mode: str = BANK) -> tuple[ParsedDate, ...]:
    """Every admissible reading of one whole date value; () when it is not a date we read."""
    if mode not in MODES:
        raise ValueError(f"unknown date mode {mode!r}")
    if raw is None:
        return ()
    text = _clean(raw)
    if not text:
        return ()
    for kind, pattern in _WHOLE:
        match = pattern.match(text)
        if match:
            return _from_groups(kind, match.group, text, mode)
    return ()


def parse_date(raw: Any, mode: str = BANK) -> ParsedDate | None:
    """The single reading of a whole date value, or None if unreadable or ambiguous."""
    alternatives = parse_date_alternatives(raw, mode)
    return alternatives[0] if len(alternatives) == 1 else None


def is_ambiguous(raw: Any, mode: str = HIT) -> bool:
    """True when the value has more than one admissible reading (hit slash dates only)."""
    return len(parse_date_alternatives(raw, mode)) > 1


def find_dates(text: Any, mode: str = BANK) -> list[tuple[int, int, str, tuple[ParsedDate, ...]]]:
    """Scan free text for dates. Returns (start, end, matched text, alternatives) in order.

    Longer shapes win ("19 May 2004" is one date, not a bare year). Two-digit years and
    numbers glued to other digits or letters are ignored.
    """
    if mode not in MODES:
        raise ValueError(f"unknown date mode {mode!r}")
    if not text:
        return []
    text = unicodedata.normalize("NFKC", str(text))
    found = []
    for match in _SCAN.finditer(text):
        kind = match.lastgroup
        if kind is None:
            continue
        get = lambda name, _k=kind, _m=match: _m.group(f"{_k}_{name}")  # noqa: E731
        alternatives = _from_groups(kind, get, match.group(kind), mode)
        if alternatives:
            found.append((match.start(kind), match.end(kind), match.group(kind), alternatives))
    return found


# ----------------------------------------------------------------------------- specs

def spec_from_dates(dates: Iterable[ParsedDate], source_id: str, origin: str, raw: str = "",
                    is_ambiguous_slash: bool = False) -> DateSpec:
    """A DateSpec whose alternatives are the given dates."""
    dates = tuple(dict.fromkeys(d for d in dates if d is not None))
    years = tuple(sorted({d.year for d in dates}))
    return DateSpec(years, dates, source_id, origin, raw, is_ambiguous_slash)


def spec_from_raw(raw: Any, source_id: str, origin: str, mode: str = HIT) -> DateSpec | None:
    """A DateSpec for one raw date value (None if it cannot be read)."""
    alternatives = parse_date_alternatives(raw, mode)
    if not alternatives:
        return None
    return spec_from_dates(alternatives, source_id, origin, str(raw), is_ambiguous_slash=len(alternatives) > 1)


def birth_years_from_age(age: int, reference: Any) -> tuple[int, int]:
    """POLICY 5: someone aged `age` at a reference year Y was born in Y-age-1 or Y-age."""
    year = reference.year if isinstance(reference, (ParsedDate, _dt.date)) else int(reference)
    return (year - age - 1, year - age)


def spec_from_age(age: int, reference: Any, source_id: str, origin: str, raw: str = "") -> DateSpec:
    """A year-only DateSpec from an age at a reference date (two possible years)."""
    return DateSpec(birth_years_from_age(int(age), reference), (), source_id, origin, raw)


def spec_alternatives(spec: DateSpec) -> tuple[ParsedDate, ...]:
    """Alternatives of a spec: its exact dates plus a year-only date for any other year."""
    out = list(spec.exact_dates)
    covered = {d.year for d in spec.exact_dates}
    out.extend(ParsedDate(y, None, None, spec.raw) for y in spec.possible_years if y not in covered)
    return tuple(out)


# ----------------------------------------------------------------------------- comparison

def compare_dates(customer: ParsedDate | None, other: ParsedDate | None) -> str:
    """AGREE / CONTRADICT / UNKNOWN for two single dates (POLICY 6 and 7)."""
    if customer is None or other is None:
        return UNKNOWN
    if abs(customer.year - other.year) > 1:
        return CONTRADICT
    if customer.year != other.year:
        return UNKNOWN
    if customer.month is not None and other.month is not None:
        if customer.month != other.month:
            return CONTRADICT
        if customer.day is not None and other.day is not None and customer.day != other.day:
            return CONTRADICT
    return AGREE


def compare_dob(customer: ParsedDate | None, specs: DateSpec | Iterable[DateSpec]) -> str:
    """Compare a customer date with every alternative of one or more hit DateSpecs.

    CONTRADICT only if there is at least one alternative and every alternative contradicts;
    AGREE if any alternative agrees; otherwise UNKNOWN.
    """
    if isinstance(specs, DateSpec):
        specs = (specs,)
    results = [compare_dates(customer, alt) for spec in specs for alt in spec_alternatives(spec)]
    if not results or customer is None:
        return UNKNOWN
    if AGREE in results:
        return AGREE
    if all(r == CONTRADICT for r in results):
        return CONTRADICT
    return UNKNOWN


def agreeing_alternatives(customer: ParsedDate | None, specs: DateSpec | Iterable[DateSpec]) -> tuple[ParsedDate, ...]:
    """The hit alternatives that agree with the customer date (for explanations)."""
    if isinstance(specs, DateSpec):
        specs = (specs,)
    return tuple(alt for spec in specs for alt in spec_alternatives(spec) if compare_dates(customer, alt) == AGREE)


def describe_years(specs: DateSpec | Iterable[DateSpec]) -> str:
    """'1976 or 1977', '1955-06-20 or 1955-20-06'-free text of a spec's alternatives."""
    if isinstance(specs, DateSpec):
        specs = (specs,)
    labels = list(dict.fromkeys(alt.iso_str for spec in specs for alt in spec_alternatives(spec)))
    if not labels:
        return ""
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]


# ----------------------------------------------------------------------------- bounds & time

def earliest(d: ParsedDate) -> _dt.date:
    """Earliest calendar day a partial date can denote."""
    return _dt.date(d.year, d.month or 1, d.day or 1)


def latest(d: ParsedDate) -> _dt.date:
    """Latest calendar day a partial date can denote."""
    month = d.month or 12
    day = d.day or calendar.monthrange(d.year, month)[1]
    return _dt.date(d.year, month, day)


def _add_years(day: _dt.date, years: int) -> _dt.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February -> 1 March
        return day.replace(year=day.year + years, month=3, day=1)


def certainly_younger_than(birth: ParsedDate | None, reference: ParsedDate | None, years: int = 16) -> bool:
    """True only if the person was under `years` at `reference` for every admissible reading:
    even born as early as possible, they turned `years` after the latest possible reference day."""
    if birth is None or reference is None:
        return False
    return _add_years(earliest(birth), years) > latest(reference)


def certainly_after(first: ParsedDate | None, second: ParsedDate | None) -> bool:
    """True only if `first` is after `second` whatever their missing parts are
    (a company incorporated after the earliest dated report)."""
    if first is None or second is None:
        return False
    return earliest(first) > latest(second)


def certainly_before(first: ParsedDate | None, second: ParsedDate | None) -> bool:
    """True only if `first` is before `second` whatever their missing parts are
    (a date of death before the screening date)."""
    if first is None or second is None:
        return False
    return latest(first) < earliest(second)


def any_certainly_younger_than(births: Iterable[ParsedDate], reference: ParsedDate | None, years: int = 16) -> bool:
    """True only if every admissible birth date makes the person younger than `years`."""
    births = [b for b in births if b is not None]
    return bool(births) and all(certainly_younger_than(b, reference, years) for b in births)
