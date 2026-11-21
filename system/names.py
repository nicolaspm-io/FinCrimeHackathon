"""Name normalisation and matching (POLICY principle 8) plus the search-provider query key.

Normalisation: case, accents and punctuation are ignored, and so is anything in (brackets) or
[brackets]. Apostrophes join ("O'Brien" -> "obrien"); other punctuation splits
("Müller-Kägi" -> "muller kagi"). Single letters are initials, not parts. Placeholders (NA,
N/A, TEST, UNKNOWN, XXX, NONE, pure digits ...) are not parts, but letters-plus-digits tokens
such as "N26" are. For companies the legal forms in name_variants.json (Ltd, GmbH, SA, N.A. ...)
are not parts. Each distinct part counts once.

Two parts match when they are identical, are listed together in one name_variants.json group
(pairwise only: "yusuf" and "giuseppe" both list "joseph" but do not match each other), are
written together or apart ("abdulsalam" ~ "abdul" + "salam", one match covering both parts),
or are within the edit budget of the SHORTER part: < 4 letters 0 edits, 4-6 letters 1,
7+ letters 2. Edits are plain Levenshtein (insert / delete / substitute), so a swap of two
letters costs 2.

Two names agree when at least two parts match, or every part of the shorter name is covered.
Each part is used at most once; the best assignment is found exhaustively (names are short).

The provider query key (search_results.query) is a different thing: lowercase words without
punctuation or 1-letter initials, sorted, repeats KEPT ("abubakar abubakar").
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

try:
    from .contracts import ENTITY_COMPANY
except ImportError:
    from contracts import ENTITY_COMPANY

PLACEHOLDER_WORDS = frozenset({
    "na", "nan", "n/a", "test", "tester", "testing", "unknown", "unk", "unkn", "none", "null",
    "nil", "tba", "tbc", "tbd", "dummy", "sample", "noname", "anonymous", "unnamed", "missing",
    "notprovided", "notknown", "xyz",
})
_PLACEHOLDER_SHAPE = re.compile(r"^(?:x{2,}|-+|\?+|\d+|0+)$")
_SPECIAL_LETTERS = str.maketrans({
    "ß": "ss", "ẞ": "ss", "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "ı": "i",
})
_APOSTROPHES = "'’‘`´ʼ"


# ----------------------------------------------------------------------------- variants

@dataclass(frozen=True, eq=False)  # compared and hashed by identity (it holds dicts)
class NameVariants:
    """name_variants.json: given-name groups (pairwise, non-transitive) and legal forms."""

    groups_of: dict  # part -> frozenset of group indexes
    legal_forms: frozenset

    def same_group(self, a: str, b: str) -> bool:
        ga, gb = self.groups_of.get(a), self.groups_of.get(b)
        return bool(ga and gb and ga & gb)


EMPTY_VARIANTS = NameVariants({}, frozenset())


def build_variants(raw: dict[str, Any]) -> NameVariants:
    """NameVariants from the parsed name_variants.json content."""
    groups_of: dict[str, set] = {}
    for index, group in enumerate(raw.get("groups") or []):
        for word in group:
            for part in normalize_tokens(word):
                groups_of.setdefault(part, set()).add(index)
    forms = set()
    for form in raw.get("company_legal_forms") or []:
        forms.update(_collapse_acronyms(_fold(form)).split())
    return NameVariants({k: frozenset(v) for k, v in groups_of.items()}, frozenset(forms))


def load_variants(path_or_dir: str) -> NameVariants:
    """Load name_variants.json from a file path or a data folder."""
    path = os.path.join(path_or_dir, "name_variants.json") if os.path.isdir(path_or_dir) else path_or_dir
    with open(path, encoding="utf-8") as handle:
        return build_variants(json.load(handle))


# ----------------------------------------------------------------------------- normalisation

def _fold(text: Any) -> str:
    """Lowercase, fold accents (NFKD) and special letters, drop bracketed text."""
    text = str(text or "")
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", text)
    text = unicodedata.normalize("NFKC", text).translate(_SPECIAL_LETTERS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def _collapse_acronyms(text: str) -> str:
    """'n.a.' -> 'na', 's.p.a.' -> 'spa' (dotted single letters written as one word)."""
    return re.sub(r"\b(?:[^\W\d_]\.){2,}(?:[^\W\d_]\b)?", lambda m: m.group(0).replace(".", ""), text)


def normalize_tokens(name: Any, collapse_acronyms: bool = False) -> list[str]:
    """Folded words of a name in order, before initials/placeholders/legal forms are dropped."""
    text = _fold(name)
    if collapse_acronyms:
        text = _collapse_acronyms(text)
    for ch in _APOSTROPHES:
        text = text.replace(ch, "")
    text = re.sub(r"[^\w]+|_", " ", text)
    return text.split()


def is_placeholder(token: str) -> bool:
    """True for placeholder tokens (NA, TEST, UNKNOWN, XXX, 0000 ...). 'N26' is not one."""
    return token in PLACEHOLDER_WORDS or bool(_PLACEHOLDER_SHAPE.match(token))


def name_parts(name: Any, entity_type: str | None = None, variants: NameVariants = EMPTY_VARIANTS) -> tuple[str, ...]:
    """Distinct real name parts, sorted. Drops initials, placeholders and (companies) legal forms."""
    company = entity_type == ENTITY_COMPANY
    parts = set()
    for token in normalize_tokens(name, collapse_acronyms=company):
        if len(token) < 2 or is_placeholder(token):
            continue
        if company and token in variants.legal_forms:
            continue
        parts.add(token)
    return tuple(sorted(parts))


def real_part_count(name: Any, entity_type: str | None = None, variants: NameVariants = EMPTY_VARIANTS) -> int:
    """How many real parts a name has (POLICY 8: an individual needs 2 to be decided on name)."""
    return len(name_parts(name, entity_type, variants))


def query_key(name: Any, drop_brackets: bool = True) -> str:
    """search_results.query key, reproducing the provider rather than POLICY 8: accents
    folded and any other non-ASCII letter deleted ("Paweł" -> "pawe"), every non-alphanumeric
    character splits ("O'Connor" -> "o connor"), 1-letter words dropped, words sorted and
    repeats KEPT ("abubakar abubakar")."""
    text = str(name or "")
    if drop_brackets:
        text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w]|_", " ", text)  # punctuation (any script) splits words first
    text = text.encode("ascii", "ignore").decode("ascii").lower()  # then other letters vanish
    tokens = [t for t in text.split() if len(t) > 1]
    return " ".join(sorted(tokens))


# ----------------------------------------------------------------------------- edit distance

def levenshtein(a: str, b: str, limit: int | None = None) -> int:
    """Plain Levenshtein distance (insert, delete, substitute; a swap costs 2).
    With `limit`, returns limit + 1 as soon as the distance is known to exceed it."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if limit is not None and len(a) - len(b) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if limit is not None and min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def edit_budget(a: str, b: str) -> int:
    """Allowed edits for two parts, decided by the shorter one: <4 -> 0, 4-6 -> 1, 7+ -> 2."""
    shorter = min(len(a), len(b))
    if shorter < 4:
        return 0
    return 1 if shorter <= 6 else 2


def parts_match(a: str, b: str, variants: NameVariants = EMPTY_VARIANTS) -> str | None:
    """How two single parts match: 'exact', 'variant', 'typo', or None."""
    if a == b:
        return "exact"
    if variants.same_group(a, b):
        return "variant"
    budget = edit_budget(a, b)
    if budget and levenshtein(a, b, budget) <= budget:
        return "typo"
    return None


# ----------------------------------------------------------------------------- matching

@dataclass(frozen=True)
class NameMatch:
    """Best assignment between two names' parts."""

    left: tuple[str, ...]
    right: tuple[str, ...]
    pairs: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]  # (left parts, right parts, kind)
    agrees: bool

    @property
    def matched(self) -> int:
        return len(self.pairs)


def _edges(left: tuple[str, ...], right: tuple[str, ...], variants: NameVariants):
    """All candidate matches: (left index set, right index set, kind)."""
    edges = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            kind = parts_match(a, b, variants)
            if kind:
                edges.append(((i,), (j,), kind))
    # written together or apart: one part on one side ~ two parts (in either order) on the other
    for side_one, side_two, flip in ((left, right, False), (right, left, True)):
        for i, joined in enumerate(side_one):
            for j, first in enumerate(side_two):
                for k, second in enumerate(side_two):
                    if j == k:
                        continue
                    glued = first + second
                    if not (glued == joined or (edit_budget(glued, joined)
                                                and levenshtein(glued, joined, edit_budget(glued, joined)) <= edit_budget(glued, joined))):
                        continue
                    pair = ((i,), tuple(sorted((j, k))), "joined")
                    if flip:
                        pair = (pair[1], pair[0], "joined")
                    if pair not in edges:
                        edges.append(pair)
    return edges


def _best(edges, used_left=frozenset(), used_right=frozenset(), start=0, memo=None):
    """Exhaustive search: maximise (matches, parts covered), memoised on the parts still free.
    Ties keep the earliest edges, so the result is deterministic."""
    memo = {} if memo is None else memo
    key = (used_left, used_right, start)
    if key in memo:
        return memo[key]
    best = ((0, 0), ())
    for index in range(start, len(edges)):
        li, ri, kind = edges[index]
        if used_left.intersection(li) or used_right.intersection(ri):
            continue
        (count, covered), chosen = _best(edges, used_left | frozenset(li), used_right | frozenset(ri), index + 1, memo)
        candidate = ((count + 1, covered + len(li) + len(ri)), ((li, ri, kind),) + chosen)
        if candidate[0] > best[0]:
            best = candidate
    memo[key] = best
    return best


def match_parts(left: Iterable[str], right: Iterable[str], variants: NameVariants = EMPTY_VARIANTS) -> NameMatch:
    """Match two collections of already-normalised parts (duplicates are ignored)."""
    left, right = tuple(sorted(set(left))), tuple(sorted(set(right)))
    if not left or not right:
        return NameMatch(left, right, (), False)
    (count, _), chosen = _best(_edges(left, right, variants))
    covered_left = {i for li, _, _ in chosen for i in li}
    covered_right = {j for _, rj, _ in chosen for j in rj}
    shorter_covered = (len(left) <= len(right) and len(covered_left) == len(left)) or \
                      (len(right) <= len(left) and len(covered_right) == len(right))
    pairs = tuple(sorted((tuple(left[i] for i in li), tuple(right[j] for j in rj), kind) for li, rj, kind in chosen))
    return NameMatch(left, right, pairs, count >= 2 or shorter_covered)


@lru_cache(maxsize=65536)
def _match_cached(left: tuple, right: tuple, variants: NameVariants) -> NameMatch:
    return match_parts(left, right, variants)


def match_names(a: Any, b: Any, entity_type: str | None = None, variants: NameVariants = EMPTY_VARIANTS) -> NameMatch:
    """Match two raw names under POLICY 8."""
    return _match_cached(name_parts(a, entity_type, variants), name_parts(b, entity_type, variants), variants)


def names_agree(a: Any, b: Any, entity_type: str | None = None, variants: NameVariants = EMPTY_VARIANTS) -> bool:
    """True when two raw names agree under POLICY 8."""
    return match_names(a, b, entity_type, variants).agrees


def best_agreement(customer_names: Iterable[Any], hit_names: Iterable[Any], entity_type: str | None = None,
                   variants: NameVariants = EMPTY_VARIANTS) -> tuple[Any, Any, NameMatch] | None:
    """First (customer name, hit name, match) that agrees, scanning in the given order,
    or None when no pair agrees. Aliases count as names: pass them in hit_names."""
    for c in customer_names:
        for h in hit_names:
            if c is None or h is None:
                continue
            result = match_names(c, h, entity_type, variants)
            if result.agrees:
                return c, h, result
    return None

