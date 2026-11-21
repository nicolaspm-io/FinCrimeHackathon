#!/usr/bin/env python3
"""Compare two output files hit by hit (main vs PR, or run 1 vs run 2 of the same PR).

    python3 decision_diff.py <base.jsonl | none> <pr.jsonl> --data <dir> [--labels practice_labels.jsonl] [--out diff.md]

Prints:
  - decision counts and review rate for each file, and the practice-label cost if --labels
  - the transition matrix base -> PR
  - every hit NEWLY cleared (-> DIFFERENT_ENTITY from anything else) with its key facts:
    these are the candidate unsafe clearances (cost 60 each if wrong) and every one must be
    justified from raw data
  - every other changed hit (decision, reason code or customer record)
With base `none` (first PR that adds a system) it lists all DIFFERENT_ENTITY hits grouped
by reason code instead, so the reviewer can sample each clearance rule.
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import read_jsonl, short  # noqa: E402

DECISIONS = ["DIFFERENT_ENTITY", "SAME_ENTITY", "REVIEW"]
ABBR = {"DIFFERENT_ENTITY": "DIFF", "SAME_ENTITY": "SAME", "REVIEW": "REVIEW", None: "—"}
COST = {
    "DIFFERENT_ENTITY": {"DIFFERENT_ENTITY": 0, "SAME_ENTITY": 3, "REVIEW": 3},
    "SAME_ENTITY": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 0, "REVIEW": 2},
    "REVIEW": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 2, "REVIEW": 0},
}


def load_out(path):
    if path in (None, "none", "-"):
        return None
    rows = {}
    for r in read_jsonl(path):
        rows[r.get("hit_id")] = r
    return rows


def facts(hit, alert):
    s = alert.get("customer", {}) if alert else {}
    countries = sorted(set((hit.get("countries") or []) + (hit.get("nationality") or []) + (hit.get("citizenship") or [])))
    return (f"hit {hit['entity_type'][:4]} {hit['name']!r} dobs={hit.get('dobs') or '-'} ctry={countries or '-'} "
            f"cat={hit.get('primary_category') or (hit.get('categories') or ['-'])[0]} notes={'Y' if hit.get('profile_notes') else '-'} | "
            f"screened {s.get('name')!r} dob={s.get('dob') or '-'} ctry={s.get('country') or '-'} "
            f"link={'acc' if alert.get('account_ref') else 'login' if alert.get('login_handle') else 'email' if alert.get('email') else 'NONE'}")


def summary(name, rows, labels, hits):
    n = len(rows)
    c = collections.Counter(r.get("decision") for r in rows.values())
    missing = [h for h in hits if h not in rows]
    lines = [f"**{name}**: {n} lines; " + ", ".join(f"{ABBR[d]} {c.get(d, 0)}" for d in DECISIONS)
             + f"; review rate {100 * c.get('REVIEW', 0) / max(n, 1):.1f}%" + (f"; {len(missing)} hits missing (count as REVIEW)" if missing else "")]
    if labels:
        cost = sum(COST[l["decision"]][rows.get(l["hit_id"], {}).get("decision", "REVIEW")] for l in labels)
        base = sum(COST[l["decision"]]["REVIEW"] for l in labels)
        unsafe = [l["hit_id"] for l in labels if l["decision"] != "DIFFERENT_ENTITY" and rows.get(l["hit_id"], {}).get("decision") == "DIFFERENT_ENTITY"]
        right = sum(1 for l in labels if rows.get(l["hit_id"], {}).get("decision") == l["decision"])
        lines.append(f"  practice: {right}/{len(labels)} right, decision score {100 * max(0.0, 1 - cost / base):.1f}, unsafe clearances {len(unsafe)} {unsafe or ''}")
    codes = collections.Counter((r.get("decision"), r.get("reason_code")) for r in rows.values())
    lines.append("  codes: " + ", ".join(f"{ABBR[d]}/{code} {k}" for (d, code), k in sorted(codes.items(), key=lambda x: (DECISIONS.index(x[0][0]) if x[0][0] in DECISIONS else 9, -x[1]))))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("pr")
    ap.add_argument("--data", required=True)
    ap.add_argument("--labels")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int, default=400, help="max hits listed per section")
    args = ap.parse_args()

    hits = {h["hit_id"]: h for h in read_jsonl(os.path.join(args.data, "hits.jsonl"))}
    alerts = {a["alert_id"]: a for a in read_jsonl(os.path.join(args.data, "alerts.jsonl"))}
    labels = read_jsonl(args.labels) if args.labels else None
    label_by = {l["hit_id"]: l for l in labels or []}
    base, pr = load_out(args.base), load_out(args.pr)

    md = ["# Decision diff", ""]
    if base is not None:
        md += summary(f"base ({args.base})", base, labels, hits)
    md += summary(f"PR ({args.pr})", pr, labels, hits)
    md.append("")

    def lab(hid):
        l = label_by.get(hid)
        return f" **[label: {ABBR[l['decision']]} {l['reason_codes']}]**" if l else ""

    if base is None:
        md += ["## No base output: every clearance by reason code", "",
               "Sample each group and re-derive a few by hand from `hit_dossier.py` — a clearance rule that is wrong once is usually wrong many times.", ""]
        groups = collections.defaultdict(list)
        for hid, r in pr.items():
            if r.get("decision") == "DIFFERENT_ENTITY":
                groups[r.get("reason_code")].append(hid)
        for code, hids in sorted(groups.items(), key=lambda x: -len(x[1])):
            md.append(f"### {code} ({len(hids)})")
            for hid in sorted(hids)[: args.limit]:
                h = hits.get(hid, {"entity_type": "?", "name": "?"})
                md.append(f"- {hid}{lab(hid)} — {facts(h, alerts.get(h.get('alert_id'), {}))}")
                md.append(f"  - PR reason: {short(pr[hid].get('reason'), 300)} | evidence {pr[hid].get('evidence')}")
            md.append("")
    else:
        matrix = collections.Counter()
        for hid in hits:
            matrix[(base.get(hid, {}).get("decision"), pr.get(hid, {}).get("decision"))] += 1
        md += ["## Transitions (rows: base, columns: PR)", "", "| base \\ PR | " + " | ".join(ABBR[d] for d in DECISIONS + [None]) + " |",
               "|---|" + "---:|" * (len(DECISIONS) + 1)]
        for b in DECISIONS + [None]:
            md.append(f"| {ABBR[b]} | " + " | ".join(str(matrix.get((b, p), 0)) for p in DECISIONS + [None]) + " |")
        md.append("")
        newly_cleared = [hid for hid in hits if pr.get(hid, {}).get("decision") == "DIFFERENT_ENTITY" and base.get(hid, {}).get("decision") != "DIFFERENT_ENTITY"]
        md += [f"## Newly cleared — {len(newly_cleared)} hits (each is a potential 60-point unsafe clearance; justify every one from raw data)", ""]
        for hid in sorted(newly_cleared, key=lambda x: (pr[x].get("reason_code"), x))[: args.limit]:
            h = hits[hid]
            md.append(f"- {hid}{lab(hid)} {ABBR[base.get(hid, {}).get('decision')]}→DIFF/{pr[hid].get('reason_code')} — {facts(h, alerts.get(h['alert_id'], {}))}")
            md.append(f"  - PR reason: {short(pr[hid].get('reason'), 300)} | evidence {pr[hid].get('evidence')}")
        md.append("")
        uncleared = [hid for hid in hits if base.get(hid, {}).get("decision") == "DIFFERENT_ENTITY" and pr.get(hid, {}).get("decision") != "DIFFERENT_ENTITY"]
        md += [f"## No longer cleared — {len(uncleared)} hits (safer, but each costs 3 if it really is a different entity)", ""]
        for hid in sorted(uncleared)[: args.limit]:
            h = hits[hid]
            md.append(f"- {hid}{lab(hid)} DIFF/{base[hid].get('reason_code')}→{ABBR[pr.get(hid, {}).get('decision')]}/{pr.get(hid, {}).get('reason_code')} — {facts(h, alerts.get(h['alert_id'], {}))}")
        md.append("")
        other = []
        for hid in hits:
            b, p = base.get(hid, {}), pr.get(hid, {})
            if hid in newly_cleared or hid in uncleared:
                continue
            changed = [k for k in ("decision", "reason_code", "customer_record_id") if b.get(k) != p.get(k)]
            if changed:
                other.append((hid, changed, b, p))
        md += [f"## Other changes — {len(other)} hits", ""]
        for hid, changed, b, p in other[: args.limit]:
            md.append(f"- {hid}{lab(hid)} " + "; ".join(f"{k}: {b.get(k)} → {p.get(k)}" for k in changed))
        md.append("")
        if not (newly_cleared or uncleared or other):
            md.append("**No differences in decision, reason code or customer record.**")
    text = "\n".join(md) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
