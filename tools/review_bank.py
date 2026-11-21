#!/usr/bin/env python3
"""Check and human-verify the synthetic paraphrase bank (Track GA input).

    python3 tools/review_bank.py check              # automatic checks, exit 0 only when verified
    python3 tools/review_bank.py review [--all]     # yes/no pass on the flagged rows

Files: tests/fixtures/paraphrase_bank.jsonl (open) and
tests/fixtures/paraphrase_bank_sealed.jsonl (sealed: two rows per group, read only
by a person proofreading and by the final Track GA run; whoever is fixing parsers
does not open it).

Automatic checks are polarity-aware:
  positive rows  every expected fact must be visible in the text (name parts, DOB
                 year/month/day, age and reference date, document number, listing
                 phrase); a country given only as a demonym or code is FLAGGED for
                 the proofread instead of failed.
  negative rows  the expected fact list must be empty and every tempting token must
                 appear in the text; tokens cannot confirm a negation or a third-party
                 subject, so every negative row is FLAGGED.
  all rows       other_tokens must appear; a year not explained by the facts,
                 other_tokens or tempting tokens is FLAGGED; a new numeric date whose
                 day and month could swap (both <= 12) FAILS unless it is copied from
                 the source text, so the proofread never has to pick a day.

The bank counts as verified when no row FAILS and every FLAGGED row carries a
"yes" for its current content (the answer stores a hash of the row; editing a row
makes the answer stale).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import textwrap
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = (
    ROOT / "tests" / "fixtures" / "paraphrase_bank.jsonl",
    ROOT / "tests" / "fixtures" / "paraphrase_bank_sealed.jsonl",
)
GROUPS = {
    "kyc_approved": ("kyc_note", "positive"),
    "kyc_rejected": ("kyc_note", "negative"),
    "residence_positive": ("residence_note", "positive"),
    "residence_traps": ("residence_note", "negative"),
    "profile_dob_positive": ("hit_p1_dob", "positive"),
    "profile_traps": (("hit_p1_dob", "dated_report"), "negative"),
    "sr_snippets": ("sr_facts", None),
}
SEALED_PER_GROUP = 2
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
# Demonyms and codes: a country written this way is flagged for the proofread.
COUNTRY_FORMS = {
    "bangladesh": ["bangladeshi"], "brazil": ["brazilian"], "bulgaria": ["bulgarian"],
    "canada": ["canadian"], "denmark": ["danish", "dane"], "france": ["french"],
    "germany": ["german"], "ghana": ["ghanaian"], "india": ["indian"], "italy": ["italian"],
    "kenya": ["kenyan"], "moldova": ["moldovan"], "morocco": ["moroccan"],
    "nigeria": ["nigerian"], "pakistan": ["pakistani"], "panama": ["panamanian"],
    "peru": ["peruvian"], "portugal": ["portuguese"], "romania": ["romanian"],
    "serbia": ["serbian"], "slovenia": ["slovenian", "slovene"], "spain": ["spanish"],
    "sweden": ["swedish"], "thailand": ["thai"],
    "united states": ["us", "u.s.", "usa", "american"],
}
NEGATIVE_QUESTION = {
    "kyc_note": "Is this check NOT approved, so that none of its facts count as verified?",
    "residence_note": "Does the text NOT say that the customer lived in any country?",
    "hit_p1_dob": "Does paragraph 1 NOT give the hit's own birth date or age?",
    "dated_report": "Is the report line NOT in the exact 'According to …; Month D, YYYY:' form?",
    "sr_facts": "Is the snippet NOT attributable to the hit (listing phrase or country differs)?",
}
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
NUM_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})([./-])(\d{1,2})\2(\d{4})(?!\d)")
ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def fold(text: str) -> str:
    """Casefold and strip accents."""
    norm = unicodedata.normalize("NFKD", text)
    return "".join(c for c in norm if not unicodedata.combining(c)).casefold()


def has_word(text: str, token: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(fold(token)) + r"(?!\w)", fold(text)) is not None


def row_sha(row: dict) -> str:
    body = {k: v for k, v in row.items() if k != "human_check"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_iso(value: str) -> tuple[int, int | None, int | None]:
    parts = [int(p) for p in str(value).split("-")]
    return parts[0], (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)


def date_visible(text: str, value: str) -> list[str]:
    """Return the parts of an ISO-precision date that the text does not show."""
    year, month, day = parse_iso(value)
    if not has_word(text, str(year)):
        return [f"year {year}"]
    if month is None:
        return []
    numeric = [(int(a), int(b), int(y)) for a, _, b, y in NUM_DATE_RE.findall(text)]
    numeric += [(int(d), int(m), int(y)) for y, m, d in ISO_RE.findall(text)]
    for a, b, y in numeric:
        if y == year and ((b == month and (day is None or a == day)) or
                          (a == month and (day is None or b == day))):
            return []
    missing = []
    name = MONTHS[month - 1]
    if not (has_word(text, name) or re.search(r"(?<!\w)" + name[:3] + r"\.?(?!\w)", fold(text))):
        missing.append(f"month {name}")
    if day is not None and not re.search(rf"(?<!\d)0?{day}(?:st|nd|rd|th)?(?!\d)", fold(text)):
        missing.append(f"day {day}")
    return missing


def country_status(text: str, country: str) -> str:
    """'name', 'form' (demonym or code) or 'missing'."""
    if has_word(text, country):
        return "name"
    if any(has_word(text, form) for form in COUNTRY_FORMS.get(fold(country), [])):
        return "form"
    return "missing"


def years_in(values) -> set[int]:
    found: set[int] = set()
    for value in values:
        found.update(int(y) for y in YEAR_RE.findall(str(value)))
    return found


def check_row(row: dict, known_ids: set[str]) -> tuple[list[str], list[str]]:
    """Return (fails, flags) for one row."""
    fails: list[str] = []
    flags: list[str] = []
    for key in ("case_id", "group", "task", "polarity", "text", "context", "expected",
                "tempting", "other_tokens", "source"):
        if key not in row:
            fails.append(f"missing key '{key}'")
    if fails:
        return fails, flags

    text, group, task, polarity = row["text"], row["group"], row["task"], row["polarity"]
    facts = row["expected"].get("facts")
    want_task, want_polarity = GROUPS.get(group, (None, None))
    if group not in GROUPS:
        fails.append(f"unknown group '{group}'")
    elif task not in (want_task if isinstance(want_task, tuple) else (want_task,)):
        fails.append(f"task '{task}' does not belong to group '{group}'")
    if polarity not in ("positive", "negative") or (want_polarity and polarity != want_polarity):
        fails.append(f"polarity '{polarity}' does not fit group '{group}'")
    if not isinstance(facts, dict):
        return fails + ["expected.facts must be an object"], flags

    for token in row["other_tokens"]:
        if fold(token) not in fold(text):
            fails.append(f"other token '{token}' not in text")

    if polarity == "negative":
        if facts:
            fails.append("negative row must have an empty fact list")
        if not row["tempting"]:
            fails.append("negative row needs at least one tempting token")
        for token in row["tempting"]:
            if fold(token) not in fold(text):
                fails.append(f"tempting token '{token}' not in text")
        flags.append(NEGATIVE_QUESTION.get(task, "Does the text NOT state the fact?"))
    else:
        if not facts:
            fails.append("positive row needs expected facts")
        name = facts.get("verified_name")
        if name:
            for part in re.split(r"\s+", name.strip()):
                if not has_word(text, part):
                    fails.append(f"name part '{part}' not in text")
        for key in ("verified_dob", "birth_date"):
            if facts.get(key):
                fails += [f"{key}: {m} not in text" for m in date_visible(text, facts[key])]
        if "age" in facts:
            if not has_word(text, str(facts["age"])):
                fails.append(f"age {facts['age']} not in text")
            fails += [f"reference: {m} not in text" for m in date_visible(text, facts.get("reference", ""))]
            ref_year = parse_iso(facts["reference"])[0]
            want = [ref_year - facts["age"] - 1, ref_year - facts["age"]]
            if facts.get("possible_birth_years") != want:
                fails.append(f"possible_birth_years should be {want} for age {facts['age']} at {ref_year}")
        if facts.get("birth_date") and facts.get("possible_birth_years") != [parse_iso(facts["birth_date"])[0]]:
            fails.append("possible_birth_years does not match birth_date")
        if facts.get("document_number") and facts["document_number"] not in text:
            fails.append(f"document number '{facts['document_number']}' not in text")
        countries = list(facts.get("lived_countries", []))
        if facts.get("document_country"):
            countries.append(facts["document_country"])
        for country in countries:
            status = country_status(text, country)
            if status == "missing":
                fails.append(f"country '{country}' not in text (no known demonym or code either)")
            elif status == "form":
                flags.append(f"'{country}' appears only as a demonym or code: does the text still mean {country}?")
        if facts.get("attributed"):
            ctx = row["context"]
            phrase = ctx.get("category_phrase", "")
            if not phrase or phrase not in text:
                fails.append(f"listing phrase '{phrase}' must appear verbatim")
            statuses = [country_status(text, c) for c in ctx.get("hit_countries", [])]
            if "name" not in statuses:
                if "form" in statuses:
                    flags.append("hit country appears only as a demonym or code: confirm it is the hit's country")
                else:
                    fails.append("no hit country in the snippet")

    explained = years_in(row["other_tokens"]) | years_in(row["tempting"])
    for key in ("verified_dob", "birth_date", "reference"):
        if facts.get(key):
            explained.add(parse_iso(facts[key])[0])
    explained.update(facts.get("possible_birth_years", []))
    stray = sorted(years_in([text]) - explained)
    if stray:
        flags.append(f"year(s) {stray} not explained by the facts or listed tokens: is this drift?")

    original = (row["source"] or {}).get("original") or ""
    for match in NUM_DATE_RE.finditer(text):
        a, b = int(match.group(1)), int(match.group(3))
        if a <= 12 and b <= 12 and a != b and match.group(0) not in original:
            fails.append(f"new date '{match.group(0)}' could be read day-first or month-first")

    block = row.get("e2e")
    if block:
        refs = [block["part_of"]] if "part_of" in block else [s["case_id"] for s in block.get("swaps", [])]
        for ref in refs:
            if ref not in known_ids:
                fails.append(f"e2e refers to unknown case '{ref}'")
    return fails, flags


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def save(path: Path, rows: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def human_state(row: dict) -> str:
    hc = row.get("human_check")
    if not hc:
        return "-"
    if hc.get("row_sha") != row_sha(row):
        return "stale"
    return hc.get("answer", "-")


def evaluate(files: list[Path]):
    data = {path: load(path) for path in files}
    all_ids = [r.get("case_id") for rows in data.values() for r in rows]
    known = set(all_ids)
    dupes = sorted({i for i in all_ids if all_ids.count(i) > 1})
    results = []
    for path, rows in data.items():
        for row in rows:
            fails, flags = check_row(row, known)
            if row.get("case_id") in dupes:
                fails.append("duplicate case_id")
            results.append((path, row, fails, flags))
    return data, results


def cmd_check(files: list[Path]) -> int:
    data, results = evaluate(files)
    counts = {"PASS": 0, "FLAG": 0, "FAIL": 0}
    unanswered = 0
    for path, row, fails, flags in results:
        status = "FAIL" if fails else ("FLAG" if flags else "PASS")
        counts[status] += 1
        state = human_state(row)
        if status == "FLAG" and state != "yes":
            unanswered += 1
        tag = "sealed" if "sealed" in path.name else "open"
        line = f"{status:4}  {row.get('case_id', '?'):10}  {tag:6}  human={state:5}"
        print(line + ("  " + "; ".join(fails or flags) if (fails or flags) else ""))
    for path, rows in data.items():
        if "sealed" in path.name:
            per_group: dict[str, int] = {}
            for row in rows:
                per_group[row.get("group")] = per_group.get(row.get("group"), 0) + 1
            off = {g: per_group.get(g, 0) for g in GROUPS if per_group.get(g, 0) != SEALED_PER_GROUP}
            if off:
                print(f"WARN  sealed file should hold {SEALED_PER_GROUP} rows per group: {off}")
    total = sum(counts.values())
    verified = counts["FAIL"] == 0 and unanswered == 0
    print(f"\n{total} rows: {counts['PASS']} pass, {counts['FLAG']} flagged, {counts['FAIL']} fail; "
          f"{unanswered} flagged row(s) still need a 'yes'. "
          f"{'VERIFIED' if verified else 'NOT VERIFIED'}")
    return 0 if verified else 1


def show(row: dict, flags: list[str], tag: str) -> None:
    wrap = lambda s, pre: textwrap.fill(s, width=92, initial_indent=pre, subsequent_indent=" " * len(pre))
    print("\n" + "=" * 92)
    print(f"{row['case_id']}  [{tag}]  {row['group']} / {row['task']} / {row['polarity']}")
    if row["context"]:
        print(f"context:  {json.dumps(row['context'], ensure_ascii=False)}")
    original = (row["source"] or {}).get("original")
    if original:
        print(wrap(original, "original: "))
    for i, para in enumerate(row["text"].split("\n\n")):
        print(wrap(para, "TEXT:     " if i == 0 else "          "))
    expected = dict(row["expected"])
    print(f"expected: {json.dumps(expected, ensure_ascii=False)}")
    if row["tempting"]:
        print(f"tempting: {row['tempting']}")
    if row["source"].get("why"):
        print(wrap(row["source"]["why"], "why:      "))
    print("-" * 92)
    questions = flags or ["Does the text state exactly these facts for the subject, and nothing that changes them?"]
    for q in questions:
        print(f"?  {q}")


def cmd_review(files: list[Path], review_all: bool, by: str) -> int:
    data, results = evaluate(files)
    todo = [(p, r, fa, fl) for p, r, fa, fl in results
            if not fa and (fl or review_all) and human_state(r) != "yes"]
    failing = [r["case_id"] for _, r, fa, _ in results if fa]
    if failing:
        print(f"Skipping {len(failing)} failing row(s); fix them first: {', '.join(failing)}")
    if not todo:
        print("Nothing to review.")
        return cmd_check(files)
    print(f"{len(todo)} row(s) to answer. y = yes, n = no, s = skip, q = quit (answers are saved as you go).")
    for n, (path, row, _, flags) in enumerate(todo, 1):
        show(row, flags, f"{'sealed' if 'sealed' in path.name else 'open'} {n}/{len(todo)}")
        while True:
            try:
                answer = input("answer [y/n/s/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "q"
            if answer in ("y", "n", "s", "q"):
                break
        if answer == "q":
            break
        if answer == "s":
            continue
        record = {"answer": "yes" if answer == "y" else "no", "row_sha": row_sha(row),
                  "at": dt.date.today().isoformat()}
        if by:
            record["by"] = by
        if answer == "n":
            try:
                comment = input("what is wrong (optional): ").strip()
            except (EOFError, KeyboardInterrupt):
                comment = ""
            if comment:
                record["comment"] = comment
        row["human_check"] = record
        save(path, data[path])
    print()
    return cmd_check(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "review"):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="*", type=Path, default=list(DEFAULT_FILES))
        if name == "review":
            p.add_argument("--all", action="store_true", help="also ask about rows that pass automatically")
            p.add_argument("--by", default="", help="initials stored with each answer")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args.files)
    return cmd_review(args.files, args.all, args.by)


if __name__ == "__main__":
    sys.exit(main())
