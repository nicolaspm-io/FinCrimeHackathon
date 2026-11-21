#!/usr/bin/env python3
"""Risk audit of the hits that have no practice label: what a person should read by hand.

    python3 tools/audit_unlabeled.py --data dev --output output/dev.jsonl --out out/audit.md
    python3 tools/audit_unlabeled.py ... --sample 30 --seed 1     # a different, still reproducible sample

The practice labels cover 50 of the 819 dev hits, so most clearances are never scored locally.
A wrong clearance costs 60, so this tool lists, for the UNLABELLED hits:
  1. the decision / reason_code distribution next to the labels' (56% DIFF, 24% SAME, 20% REVIEW)
  2. every rare clearance: DECEASED, ENTITY_TYPE_MISMATCH, TEMPORAL_IMPOSSIBILITY
  3. every AMBIGUOUS answer (customer_record_id AMBIGUOUS or AMBIGUOUS_CUSTOMER_RECORD)
  4. every hit where no customer was found (customer_record_id null)
  5. every hit of an alert whose screened name contains a placeholder (NA, TEST, UNKNOWN, 0000 ...)
  6. every clearance of a hit with match_score 100 (the closest name matches)
  7. a seeded, stratified sample of the remaining DIFFERENT_ENTITY clearances (per reason_code)
  8. hits with no output line
Each entry shows the raw screened customer, the raw hit, and our answer, with an empty
"verdict" box to fill in. The sample order comes from sha256(seed:hit_id), so it is the same on
every machine and Python version. The tool never changes the output and always exits 0
unless its inputs are missing.
"""

import argparse
import collections
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_dev import ABBR, DECISIONS, REPO, by_hit, default_labels, link_type, load_output_rows, pct, read_jsonl  # noqa: E402

PLACEHOLDER = re.compile(r"^(na|n/a|n\.a\.|test|unknown|xxx+|none|null|nil|tbc|tbd|-+|\?+|\d+)$", re.I)
RARE_CODES = ("DECEASED", "ENTITY_TYPE_MISMATCH", "TEMPORAL_IMPOSSIBILITY")


def order_key(seed, hit_id):
    return hashlib.sha256(f"{seed}:{hit_id}".encode()).hexdigest()


def placeholder_tokens(name):
    return [t for t in re.split(r"[\s,]+", name or "") if t and PLACEHOLDER.match(t)]


def short(value, limit=160):
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hit_countries(hit):
    out = list(hit.get("countries") or []) + list(hit.get("nationality") or []) + list(hit.get("citizenship") or [])
    for addr in hit.get("addresses") or []:
        if isinstance(addr, dict):
            out.append(addr.get("country") or short(addr, 60))
        elif isinstance(addr, str):
            # Display only: the last comma part, without a trailing "[Business]" style tag.
            out.append(re.sub(r"\s*\[[^\]]*\]\s*$", "", addr).split(",")[-1].strip())
    if hit.get("place_of_birth"):
        out.append(f"pob {hit['place_of_birth']}")
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def card(hit, alert, row):
    cust = alert.get("customer") or {}
    link = link_type(alert)
    cat = hit.get("primary_category") or ((hit.get("categories") or [None])[0])
    lines = [
        f"#### {hit['hit_id']} · {alert.get('alert_id')} · **{row.get('decision', '—')} / {row.get('reason_code', '—')}** · record `{row.get('customer_record_id')}`",
        f"- screened: {cust.get('entity_type')} **{cust.get('name')}** · dob `{cust.get('dob')}` · {cust.get('country')} / {cust.get('city')} · "
        f"link {link}{(' ' + str(alert.get(link))) if link != 'none' else ''} · screened_at {alert.get('screened_at')}",
        f"- hit: {hit.get('entity_type')} **{hit.get('name')}** · score {hit.get('match_score')} · {cat} · dobs {hit.get('dobs') or '—'} · "
        f"countries {hit_countries(hit) or '—'}"
        + (f" · life_status {hit.get('life_status')} deceased_date {hit.get('deceased_date')}"
           if hit.get("deceased_date") or hit.get("life_status") else ""),
    ]
    if hit.get("aliases"):
        lines.append(f"- aliases: {short(', '.join(map(str, hit['aliases'][:6])))}")
    if hit.get("profile_notes"):
        lines.append(f"- profile_notes: {short(hit['profile_notes'], 240)}")
    lines.append(f"- reason: {row.get('reason', '—')}")
    lines.append(f"- evidence: {row.get('evidence', '—')}")
    lines.append("- verdict: [ ] ok  [ ] suspicious  — notes:")
    return "\n".join(lines)


def build(data, output, labels_path, sample, seed, limit):
    hits = {h["hit_id"]: h for h in read_jsonl(os.path.join(data, "hits.jsonl"))}
    alerts = {a["alert_id"]: a for a in read_jsonl(os.path.join(data, "alerts.jsonl"))}
    answers = by_hit(load_output_rows(output))
    labels = read_jsonl(labels_path) if labels_path else []
    labelled = {l["hit_id"] for l in labels}
    unlabelled = sorted(h for h in hits if h not in labelled)

    out = ["# Unlabelled-hit risk audit", "",
           f"output `{os.path.abspath(output)}` · data `{os.path.abspath(data)}` · "
           f"{len(unlabelled)} unlabelled hits ({len(labelled)} labelled left out) · sample {sample}, seed {seed}", ""]

    # 1. distribution
    dist = collections.Counter(answers.get(h, {}).get("decision") for h in unlabelled)
    gold = collections.Counter(l["decision"] for l in labels)
    codes = collections.Counter((answers.get(h, {}).get("decision"), answers.get(h, {}).get("reason_code")) for h in unlabelled)
    out += ["## 1. Distribution", "", "| | n | DIFF | SAME | REVIEW |", "|---|---:|---:|---:|---:|"]
    n = len(unlabelled)
    out.append(f"| ours, unlabelled | {n} | " + " | ".join(f"{dist.get(d, 0)} ({pct(dist.get(d, 0), n)})" for d in DECISIONS) + " |")
    if labels:
        g = len(labels)
        out.append(f"| practice labels (truth) | {g} | " + " | ".join(f"{gold.get(d, 0)} ({pct(gold.get(d, 0), g)})" for d in DECISIONS) + " |")
    out += ["", "| decision | reason_code | n |", "|---|---|---:|"]
    for (d, c), k in sorted(codes.items(), key=lambda kv: (str(kv[0][0]), -kv[1], str(kv[0][1]))):
        out.append(f"| {ABBR.get(d, d)} | {c} | {k} |")
    out.append("")

    listed = set()

    def section(title, ids, note=""):
        out.extend([f"## {title} ({len(ids)})", ""])
        if note:
            out.extend([note, ""])
        if not ids:
            out.extend(["_none_", ""])
        for h in ids[:limit]:
            listed.add(h)
            out.extend([card(hits[h], alerts.get(hits[h]["alert_id"], {}), answers.get(h, {})), ""])
        if len(ids) > limit:
            out.extend([f"_… {len(ids) - limit} more not shown (raise --limit)_", ""])

    present = [h for h in unlabelled if h in answers]
    ans = lambda h: answers[h]  # noqa: E731

    section("2. Rare clearances (DECEASED, ENTITY_TYPE_MISMATCH, TEMPORAL_IMPOSSIBILITY)",
            [h for h in present if ans(h).get("decision") == "DIFFERENT_ENTITY" and ans(h).get("reason_code") in RARE_CODES],
            "Each of these is a whole policy path with few or no practice labels. Check the date or type it rests on.")
    section("3. AMBIGUOUS answers",
            [h for h in present if ans(h).get("customer_record_id") == "AMBIGUOUS" or ans(h).get("reason_code") == "AMBIGUOUS_CUSTOMER_RECORD"])
    section("4. No customer found (customer_record_id null)",
            [h for h in present if ans(h).get("customer_record_id") in (None, "")],
            "A clearance here must rest on a contradiction that does not depend on the customer.")
    section("5. Alerts with a placeholder in the screened name",
            [h for h in present if placeholder_tokens((alerts.get(hits[h]["alert_id"], {}).get("customer") or {}).get("name"))],
            "Placeholder tokens are not name parts: the name alone can neither confirm nor clear.")
    section("6. Clearances of match_score 100 hits",
            [h for h in present if ans(h).get("decision") == "DIFFERENT_ENTITY" and hits[h].get("match_score") == 100],
            "match_score is not a decision input, but these are the closest name matches; every one should have a clear contradiction.")

    # 7. stratified sample of the other clearances
    rest = collections.defaultdict(list)
    for h in present:
        if ans(h).get("decision") == "DIFFERENT_ENTITY" and h not in listed:
            rest[ans(h).get("reason_code")].append(h)
    total = sum(len(v) for v in rest.values())
    picked = []
    if total and sample > 0:
        quotas = {c: max(1, round(sample * len(v) / total)) for c, v in rest.items()}
        for code in sorted(rest, key=str):
            ordered = sorted(rest[code], key=lambda h: order_key(seed, h))
            picked.extend(ordered[: quotas[code]])
    note = (f"{total} other unlabelled clearances; quota per reason_code proportional to its size (at least 1): "
            + ", ".join(f"{c} {len(v)}" for c, v in sorted(rest.items(), key=lambda kv: str(kv[0]))))
    section("7. Stratified sample of the other clearances", picked, note if total else "")

    missing = [h for h in unlabelled if h not in answers]
    section("8. Hits with no output line", missing, "These count as REVIEW when scored.")
    return "\n".join(out).rstrip() + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.path.join(REPO, "dev"), help="data folder (default: dev/)")
    ap.add_argument("--output", default=os.path.join(REPO, "output", "dev.jsonl"), help="output file to audit")
    ap.add_argument("--labels", help="practice labels to leave out (default: <data>/practice_labels.jsonl; 'none' = audit all)")
    ap.add_argument("--sample", type=int, default=20, help="size of the stratified clearance sample (default 20)")
    ap.add_argument("--seed", default="0", help="sample seed (default 0)")
    ap.add_argument("--limit", type=int, default=200, help="max cards per section (default 200)")
    ap.add_argument("--out", help="write the markdown report here instead of stdout")
    args = ap.parse_args(argv)

    if not os.path.exists(args.output):
        print(f"no output file at {args.output}", file=sys.stderr)
        return 1
    labels = None if args.labels == "none" else (args.labels or default_labels(args.data))
    report = build(args.data, args.output, labels, args.sample, args.seed, args.limit)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
