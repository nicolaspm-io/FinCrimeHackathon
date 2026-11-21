"""Shared helpers for the review-pr scripts: loading the data folder, indexing it, and
light, *descriptive* normalisation (dates, names, countries) used only to display and
group raw data. Nothing here decides a hit — the reviewer and the adjudicators do that
from the raw values, which every script always prints next to any parsed value."""

import collections
import json
import os
import re
import unicodedata

FILES = {
    "alerts": "alert_id",
    "hits": "hit_id",
    "customers": "record_id",
    "accounts": "account_id",
    "onboarding": "kyc_applicant_id",
    "kyc_checks": "check_id",
    "notes": "note_id",
    "search_results": "result_id",
}

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load(data_dir, names=None):
    """Return {file: [rows]} for the requested files (all by default)."""
    out = {}
    for name in names or FILES:
        path = os.path.join(data_dir, f"{name}.jsonl")
        out[name] = read_jsonl(path) if os.path.exists(path) else []
    variants = os.path.join(data_dir, "name_variants.json")
    if names is None or "name_variants" in names:
        out["name_variants"] = json.load(open(variants, encoding="utf-8")) if os.path.exists(variants) else {"groups": [], "company_legal_forms": []}
    return out


def index(rows, key):
    """key -> list of rows (keys can repeat, e.g. joint accounts)."""
    idx = collections.defaultdict(list)
    for row in rows:
        value = row.get(key)
        if value not in (None, ""):
            idx[value].append(row)
    return idx


# ---------------------------------------------------------------- dates

def date_shape(value):
    """Mask a raw date string: digits -> 9, month words -> Mon. '7 March 1990' -> '9 Mon 9999'."""
    if value is None:
        return "<null>"
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    shape = re.sub(r"[A-Za-z]+", lambda m: "Mon" if m.group(0).lower() in MONTHS else "Word", value)
    return re.sub(r"\d", "9", shape)


def parse_date(value):
    """Best-effort parse to (year, month, day) with None for unknown parts, or None.
    Recognises ISO (YYYY, YYYY-MM, YYYY-MM-DD), DD/MM/YYYY (the bank's slash format is
    day-first in dev), 'Month D, YYYY', 'D Month YYYY', 'Month YYYY'. Returns None when
    unsure — callers must show the raw value too."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", v)
    if m:
        return tuple(int(x) if x else None for x in m.groups())
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        d, mo, y = map(int, m.groups())
        return (y, mo, d)
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", v)
    if m and m.group(1).lower() in MONTHS:
        return (int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})", v)
    if m and m.group(2).lower() in MONTHS:
        return (int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", v)
    if m and m.group(1).lower() in MONTHS:
        return (int(m.group(2)), MONTHS[m.group(1).lower()], None)
    return None


def fmt_date(value):
    p = parse_date(value)
    if value is None:
        return "null"
    if p is None:
        return f"{value!r} (UNPARSED by helper)"
    return f"{value!r} -> {'-'.join('??' if x is None else str(x) for x in p)}"


# ---------------------------------------------------------------- names

def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def name_parts(name):
    """Lowercase, accent-free parts with brackets and punctuation removed (POLICY 8 style).
    Single letters are kept out (initials). Used for search/grouping only."""
    if not name:
        return []
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", strip_accents(name).lower())
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return [p for p in text.split() if len(p) > 1]


def edit_distance(a, b, limit=3):
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def hit_names(hit):
    names = [hit.get("name"), hit.get("best_matching_name")]
    first, middle, last = hit.get("first_name"), hit.get("middle_name"), hit.get("last_name")
    if first or last:
        names.append(" ".join(x for x in (first, middle, last) if x))
    for alias in hit.get("aliases") or []:
        names.append(alias if isinstance(alias, str) else (alias.get("name") if isinstance(alias, dict) else str(alias)))
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def short(text, n=240):
    if text is None:
        return "null"
    text = str(text).replace("\n", " ⏎ ")
    return text if len(text) <= n else text[: n - 1] + "…"
