#!/usr/bin/env python3
"""Score a system output on the dev set, beyond what validate.py prints.

    python3 tools/eval_dev.py --data dev --output output/dev.jsonl
    python3 tools/eval_dev.py --data dev --output output/dev.jsonl --run            # run system/run first, timed
    python3 tools/eval_dev.py ... --json out/summary.json                           # save a summary
    python3 tools/eval_dev.py ... --compare out/main_summary.json                   # fail if the score drops

What it reports:
  - validate.py problems (the organisers' checks, imported from the repo root)
  - decision and reason_code distribution over every output line, labelled vs unlabelled
  - against the practice labels: the 3x3 cost matrix, decision score, every unsafe clearance,
    reason_code fit, customer_record_id accuracy, evidence precision/recall/F1
  - a per-reason_code breakdown of the labels (how many we got right, with the right code)
  - customer_record_id resolution by alert link type (account_ref / login_handle / email / none)
  - reason text checks: sentence counts (flag > 4), IDs named in the reason but missing from evidence,
    template leaks such as "None" or "{"
  - runtime of system/run when --run is given

Exit code is 1 when a gate fails: any validate.py problem, any unsafe clearance on the labels,
a run slower than 30 minutes, or (with --compare / --min-score) a lower decision score.

Only the Python 3 standard library is used. Nothing here is imported by system/.
The other tools in this folder (repro_check.py, audit_unlabeled.py) reuse these helpers.
"""

import argparse
import collections
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SYSTEM = os.path.join(REPO, "system", "run")
TIME_BUDGET_S = 30 * 60

DECISIONS = ["DIFFERENT_ENTITY", "SAME_ENTITY", "REVIEW"]
ABBR = {"DIFFERENT_ENTITY": "DIFF", "SAME_ENTITY": "SAME", "REVIEW": "REVIEW"}
COST = {
    "DIFFERENT_ENTITY": {"DIFFERENT_ENTITY": 0, "SAME_ENTITY": 3, "REVIEW": 3},
    "SAME_ENTITY": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 0, "REVIEW": 2},
    "REVIEW": {"DIFFERENT_ENTITY": 60, "SAME_ENTITY": 2, "REVIEW": 0},
}
LINK_FIELDS = ("account_ref", "login_handle", "email")

# Record IDs as they appear in the data files. H- and A- are left out on purpose: a reason may
# name its own hit or alert without citing it as evidence.
RECORD_ID = re.compile(r"\b(?:CUS-\d{6}|ACC-[0-9A-F]{6}|KYC-[0-9A-F]{6}|NOTE-[0-9A-F]{6}|SR-[0-9A-F]{6})\b")
TEMPLATE_LEAK = re.compile(r"\bNone\b|\bnull\b|\bnan\b|\{|\}|\[\]|\bTODO\b|\bXXX\b")
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "ltd", "inc", "co", "corp", "no", "vs",
                 "e.g", "i.e", "etc", "approx", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
                 "sep", "sept", "oct", "nov", "dec", "u.s", "u.k", "u.a.e"}
SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+(?=[A-Z0-9\"'(])")


# ----------------------------------------------------------------------------- loading

def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_validate():
    """Import the organisers' validate.py from the repo root, whatever the working directory."""
    path = os.path.join(REPO, "validate.py")
    spec = importlib.util.spec_from_file_location("organiser_validate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_output_rows(path):
    """Output lines in file order. Lines that are not JSON objects are skipped (validate.py reports them)."""
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def by_hit(rows):
    """{hit_id: row}; the first line wins, like the organisers' 'answered twice' check."""
    out = {}
    for row in rows:
        out.setdefault(row.get("hit_id"), row)
    return out


def default_labels(data):
    path = os.path.join(data, "practice_labels.jsonl")
    return path if os.path.exists(path) else None


def link_type(alert):
    for field in LINK_FIELDS:
        if alert.get(field):
            return field
    return "none"


# ----------------------------------------------------------------------------- running

def run_system(system, data, output, env=None, cwd=None, timeout=TIME_BUDGET_S + 60):
    """Run `system data output` as a black box. Returns (seconds, returncode, stderr_tail)."""
    cmd = [os.path.abspath(system), os.path.abspath(data), os.path.abspath(output)]
    out_dir = os.path.dirname(os.path.abspath(output))
    os.makedirs(out_dir, exist_ok=True)
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        code, err = proc.returncode, proc.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        code, err = -9, (exc.stderr or b"").decode("utf-8", "replace") + "\n[timed out]"
    except OSError as exc:
        code, err = -1, f"could not start {cmd[0]}: {exc}"
    seconds = time.monotonic() - start
    tail = "\n".join(err.strip().splitlines()[-15:])
    return seconds, code, tail


# ----------------------------------------------------------------------------- text checks

def sentences(text):
    """Heuristic sentence split that ignores 'Dr.', 'U.S.', initials like 'J.' and decimals."""
    text = " ".join((text or "").split())
    if not text:
        return []
    parts, start = [], 0
    for match in SENTENCE_END.finditer(text):
        end = match.start() + 1
        word = text[start:end].rsplit(" ", 1)[-1].rstrip(".!?\"')]").lower()
        if word in ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
            continue
        parts.append(text[start:end].strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def reason_issues(row):
    """List of short problems with a row's reason text (empty list if it looks fine)."""
    issues = []
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ["empty reason"]
    # README asks for 2-4 sentences, but 23 of the 50 reference reasons are one sentence
    # (most clearances), so only more than 4 is flagged; the per-count histogram shows the rest.
    n = len(sentences(reason))
    if n > 4:
        issues.append(f"{n} sentences (max 4)")
    evidence = set(row.get("evidence") or [])
    named = sorted(set(RECORD_ID.findall(reason)) - evidence)
    if named:
        issues.append("names IDs not in evidence: " + ", ".join(named))
    leak = TEMPLATE_LEAK.search(reason)
    if leak:
        issues.append(f"template leak {leak.group(0)!r}")
    return issues


# ----------------------------------------------------------------------------- metrics

def prf(pred, gold):
    pred, gold = set(pred), set(gold)
    tp = len(pred & gold)
    p = tp / len(pred) if pred else (1.0 if not gold else 0.0)
    r = tp / len(gold) if gold else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return tp, len(pred), len(gold), p, r, f


def record_ok(pred_row, label):
    """Same rule as validate.py's self_score."""
    got = pred_row.get("customer_record_id")
    want = label["customer_record_ids"]
    return got in want or (want == ["AMBIGUOUS"] and got in (None, "", "AMBIGUOUS"))


def evaluate(data, output, labels_path=None, validate_module=None):
    """Compute every metric. Returns a JSON-serialisable summary dict."""
    validate_module = validate_module or load_validate()
    _, problems = validate_module.validate(data, output)
    rows = load_output_rows(output)
    answers = by_hit(rows)
    alerts = {a["alert_id"]: a for a in read_jsonl(os.path.join(data, "alerts.jsonl"))}
    hits = {h["hit_id"]: h for h in read_jsonl(os.path.join(data, "hits.jsonl"))}
    labels = read_jsonl(labels_path) if labels_path else []
    labelled = {l["hit_id"] for l in labels}

    summary = {
        "output": os.path.abspath(output),
        "lines": len(rows),
        "hits": len(hits),
        "problems": len(problems),
        "problem_examples": problems[:20],
    }

    # Distribution over every line, split labelled / unlabelled.
    dist = {"all": collections.Counter(), "labelled": collections.Counter(), "unlabelled": collections.Counter()}
    codes = collections.Counter()
    for hit_id, row in answers.items():
        d = row.get("decision")
        dist["all"][d] += 1
        dist["labelled" if hit_id in labelled else "unlabelled"][d] += 1
        codes[(d, row.get("reason_code"))] += 1
    summary["distribution"] = {k: dict(v) for k, v in dist.items()}
    summary["reason_codes"] = {f"{d}/{c}": n for (d, c), n in sorted(codes.items(), key=lambda kv: (str(kv[0][0]), -kv[1]))}
    summary["gold_distribution"] = dict(collections.Counter(l["decision"] for l in labels))

    # Customer record resolution by link type, over every output line.
    by_link = collections.defaultdict(collections.Counter)
    for hit_id, hit in hits.items():
        alert = alerts.get(hit["alert_id"], {})
        got = answers.get(hit_id, {}).get("customer_record_id")
        kind = "AMBIGUOUS" if got == "AMBIGUOUS" else ("CUS" if got else "null")
        by_link[link_type(alert)][kind] += 1
    summary["record_by_link"] = {k: dict(v) for k, v in sorted(by_link.items())}

    # Reason text checks over every line.
    reason_problems = []
    sentence_counts = collections.Counter()
    for hit_id, row in sorted(answers.items(), key=lambda kv: str(kv[0])):
        sentence_counts[len(sentences(row.get("reason") or ""))] += 1
        issues = reason_issues(row)
        if issues:
            reason_problems.append({"hit_id": hit_id, "issues": issues})
    summary["sentence_counts"] = {str(k): v for k, v in sorted(sentence_counts.items())}
    summary["reason_problems"] = len(reason_problems)
    summary["reason_problem_examples"] = reason_problems[:25]

    if labels:
        summary["labels"] = score_labels(labels, answers, alerts)
    return summary


def score_labels(labels, answers, alerts):
    confusion = {g: {p: 0 for p in DECISIONS} for g in DECISIONS}
    cost = baseline = 0
    unsafe, wrong = [], []
    right = codes_ok = records_ok = 0
    ev_tp = ev_pred = ev_gold = 0
    ev_f1s, ev_exact = [], 0
    per_code = collections.defaultdict(lambda: {"n": 0, "decision_right": 0, "code_right": 0, "record_right": 0})
    link_records = collections.defaultdict(lambda: {"n": 0, "record_right": 0})
    misses = []

    for label in labels:
        hit_id, gold = label["hit_id"], label["decision"]
        row = answers.get(hit_id, {})
        pred = row.get("decision") if row.get("decision") in DECISIONS else "REVIEW"
        confusion[gold][pred] += 1
        cost += COST[gold][pred]
        baseline += COST[gold]["REVIEW"]
        bucket = per_code[f"{gold}/{label['reason_codes'][0]}"]
        bucket["n"] += 1
        kind = link_type(alerts.get(label["alert_id"], {}))
        link_records[kind]["n"] += 1
        rec_ok = record_ok(row, label)
        link_records[kind]["record_right"] += rec_ok

        entry = {
            "hit_id": hit_id, "gold": gold, "gold_codes": label["reason_codes"],
            "pred": row.get("decision"), "pred_code": row.get("reason_code"), "cost": COST[gold][pred],
            "gold_records": label["customer_record_ids"], "pred_record": row.get("customer_record_id"),
            "path": label.get("path", []),
        }
        if gold != "DIFFERENT_ENTITY" and pred == "DIFFERENT_ENTITY":
            unsafe.append(entry)
        if pred != gold:
            wrong.append(entry)
            continue

        right += 1
        bucket["decision_right"] += 1
        code_ok = row.get("reason_code") in label["reason_codes"]
        codes_ok += code_ok
        bucket["code_right"] += code_ok
        records_ok += rec_ok
        bucket["record_right"] += rec_ok
        tp, np_, ng, _, _, f = prf(row.get("evidence") or [], label.get("evidence") or [])
        ev_tp, ev_pred, ev_gold = ev_tp + tp, ev_pred + np_, ev_gold + ng
        ev_f1s.append(f)
        ev_exact += set(row.get("evidence") or []) == set(label.get("evidence") or [])
        if not code_ok or not rec_ok or f < 1.0:
            entry.update({
                "code_ok": code_ok, "record_ok": rec_ok, "evidence_f1": round(f, 3),
                "pred_evidence": sorted(row.get("evidence") or []), "gold_evidence": sorted(label.get("evidence") or []),
            })
            misses.append(entry)

    micro_p = ev_tp / ev_pred if ev_pred else 0.0
    micro_r = ev_tp / ev_gold if ev_gold else 0.0
    micro_f = 2 * micro_p * micro_r / (micro_p + micro_r) if micro_p + micro_r else 0.0
    return {
        "n": len(labels),
        "confusion": confusion,
        "cost": cost,
        "baseline": baseline,
        "decision_score": round(100 * max(0.0, 1 - cost / baseline), 2) if baseline else 0.0,
        "right": right,
        "unsafe": len(unsafe),
        "unsafe_hits": unsafe,
        "wrong_hits": wrong,
        "reason_code_rate": round(codes_ok / right, 4) if right else None,
        "record_rate": round(records_ok / right, 4) if right else None,
        "evidence": {
            "micro_precision": round(micro_p, 4), "micro_recall": round(micro_r, 4), "micro_f1": round(micro_f, 4),
            "macro_f1": round(sum(ev_f1s) / len(ev_f1s), 4) if ev_f1s else None,
            "exact_sets": ev_exact,
        },
        "per_gold_code": dict(sorted(per_code.items())),
        "record_by_link": dict(sorted(link_records.items())),
        "right_but_imperfect": misses,
    }


# ----------------------------------------------------------------------------- report

def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "-"


def print_report(s, verbose=False, show_paths=False, out=sys.stdout):
    w = lambda line="": print(line, file=out)  # noqa: E731
    w(f"== {s['output']}")
    if "run" in s:
        r = s["run"]
        w(f"run: {r['seconds']:.1f}s, exit {r['returncode']}" + ("  ** over 30 min **" if r["seconds"] > TIME_BUDGET_S else ""))
        if r["returncode"] != 0 and r["stderr_tail"]:
            w("  stderr tail:\n    " + r["stderr_tail"].replace("\n", "\n    "))
    w(f"validate.py: {s['lines']} lines for {s['hits']} hits, {s['problems']} problems")
    for p in s["problem_examples"][:10]:
        w(f"  {p}")

    w("\n-- decisions (all lines)")
    for part in ("all", "labelled", "unlabelled"):
        d = s["distribution"][part]
        total = sum(d.values())
        w(f"  {part:<10} n={total:<4} " + "  ".join(f"{ABBR.get(k, k)} {d.get(k, 0)} ({pct(d.get(k, 0), total)})" for k in DECISIONS))
    g = s.get("gold_distribution") or {}
    if g:
        total = sum(g.values())
        w(f"  {'labels':<10} n={total:<4} " + "  ".join(f"{ABBR[k]} {g.get(k, 0)} ({pct(g.get(k, 0), total)})" for k in DECISIONS))
    w("  reason codes: " + ", ".join(f"{k} {v}" for k, v in s["reason_codes"].items()))

    w("\n-- customer_record_id by alert link type (all hits)")
    for kind, c in s["record_by_link"].items():
        total = sum(c.values())
        w(f"  {kind:<13} n={total:<4} CUS {c.get('CUS', 0)}  AMBIGUOUS {c.get('AMBIGUOUS', 0)}  null {c.get('null', 0)}")

    w("\n-- reason text")
    w("  sentences per reason: " + ", ".join(f"{k}:{v}" for k, v in s["sentence_counts"].items()) + "   (reference reasons: 1:23, 2:9, 3:18)")
    w(f"  {s['reason_problems']} lines with reason issues")
    for e in s["reason_problem_examples"][: (25 if verbose else 8)]:
        w(f"    {e['hit_id']}: {'; '.join(e['issues'])}")

    L = s.get("labels")
    if not L:
        w("\n(no practice labels given)")
        return
    w(f"\n-- practice labels ({L['n']} hits)")
    w("  truth \\ said     DIFF   SAME  REVIEW")
    for gtruth in DECISIONS:
        row = L["confusion"][gtruth]
        w(f"  {ABBR[gtruth]:<14}" + "".join(f"{row[p]:>7}" for p in DECISIONS))
    w(f"  cost {L['cost']} vs all-REVIEW {L['baseline']}  ->  decision score {L['decision_score']:.1f}")
    w(f"  right decisions {L['right']}/{L['n']}, UNSAFE CLEARANCES {L['unsafe']}")
    if L["right"]:
        e = L["evidence"]
        w(f"  on right decisions: reason code {L['reason_code_rate']:.0%}, customer record {L['record_rate']:.0%}, "
          f"evidence F1 micro {e['micro_f1']:.2f} (P {e['micro_precision']:.2f} R {e['micro_recall']:.2f}) "
          f"macro {e['macro_f1']:.2f}, exact sets {e['exact_sets']}/{L['right']}")
    for u in L["unsafe_hits"]:
        w(f"  !! UNSAFE {u['hit_id']}: truth {u['gold']} {u['gold_codes']}, said DIFFERENT_ENTITY {u['pred_code']}")

    w("\n  " + "by gold reason code".ljust(42) + "  n  right  code  record")
    for code, b in L["per_gold_code"].items():
        w(f"  {code:<42}{b['n']:>3}{b['decision_right']:>7}{b['code_right']:>6}{b['record_right']:>8}")
    w("\n  customer record (all labels) by link type")
    for kind, b in L["record_by_link"].items():
        w(f"  {kind:<13} {b['record_right']}/{b['n']}")

    if L["wrong_hits"]:
        w(f"\n  wrong decisions ({len(L['wrong_hits'])}), costliest first")
        for x in sorted(L["wrong_hits"], key=lambda x: (-x["cost"], x["hit_id"])):
            w(f"   {x['hit_id']} cost {x['cost']:>2}: truth {ABBR[x['gold']]} {'/'.join(x['gold_codes'])} | said "
              f"{ABBR.get(x['pred'], x['pred'])} {x['pred_code']} | record want {x['gold_records']} got {x['pred_record']}")
            if show_paths:
                for step in x["path"]:
                    w(f"        path: {step}")
    if verbose and L["right_but_imperfect"]:
        w(f"\n  right decision, imperfect reason ({len(L['right_but_imperfect'])})")
        for x in L["right_but_imperfect"]:
            bits = []
            if not x["code_ok"]:
                bits.append(f"code {x['pred_code']} not in {x['gold_codes']}")
            if not x["record_ok"]:
                bits.append(f"record {x['pred_record']} not in {x['gold_records']}")
            if x["evidence_f1"] < 1:
                bits.append(f"evidence {x['pred_evidence']} vs {x['gold_evidence']}")
            w(f"   {x['hit_id']}: " + "; ".join(bits))
            if show_paths:
                for step in x["path"]:
                    w(f"        path: {step}")


def gates(s, min_score=None):
    """List of failed gate descriptions (empty when everything passes)."""
    failed = []
    if s["problems"]:
        failed.append(f"validate.py reports {s['problems']} problems")
    if "run" in s:
        if s["run"]["returncode"] != 0:
            failed.append(f"system/run exited {s['run']['returncode']}")
        if s["run"]["seconds"] > TIME_BUDGET_S:
            failed.append(f"run took {s['run']['seconds']:.0f}s (> 30 min)")
    L = s.get("labels")
    if L:
        if L["unsafe"]:
            failed.append(f"{L['unsafe']} unsafe clearances on the practice labels")
        if min_score is not None and L["decision_score"] < min_score - 1e-9:
            failed.append(f"decision score {L['decision_score']:.1f} < {min_score:.1f}")
    return failed


def print_compare(s, prev, out=sys.stdout):
    a, b = prev.get("labels") or {}, s.get("labels") or {}
    print("\n-- compared with " + prev.get("output", "previous summary"), file=out)
    for key, label in (("decision_score", "decision score"), ("unsafe", "unsafe clearances"), ("right", "right decisions"),
                       ("reason_code_rate", "reason code rate"), ("record_rate", "customer record rate")):
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            print(f"  {label:<22} {x} -> {y}", file=out)
        else:
            print(f"  {label:<22} {x} -> {y}  ({y - x:+.4g})", file=out)
    x, y = (a.get("evidence") or {}).get("micro_f1"), (b.get("evidence") or {}).get("micro_f1")
    print(f"  {'evidence micro F1':<22} {x} -> {y}", file=out)
    print(f"  {'problems':<22} {prev.get('problems')} -> {s.get('problems')}", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.path.join(REPO, "dev"), help="data folder (default: dev/)")
    ap.add_argument("--output", default=os.path.join(REPO, "output", "dev.jsonl"), help="output file to score")
    ap.add_argument("--labels", help="practice labels (default: <data>/practice_labels.jsonl if present; 'none' to skip)")
    ap.add_argument("--run", action="store_true", help="run system/run <data> <output> first and time it")
    ap.add_argument("--system", default=DEFAULT_SYSTEM, help="executable to run with --run (default: system/run)")
    ap.add_argument("--json", dest="json_out", help="write the summary as JSON here")
    ap.add_argument("--compare", help="a previous --json summary; fails if the decision score drops")
    ap.add_argument("--min-score", type=float, help="fail if the decision score is below this")
    ap.add_argument("--verbose", "-v", action="store_true", help="also list right decisions with an imperfect reason")
    ap.add_argument("--paths", action="store_true", help="print each label's reference path under its miss")
    args = ap.parse_args(argv)

    labels = None if args.labels == "none" else (args.labels or default_labels(args.data))
    run_info = None
    if args.run:
        seconds, code, tail = run_system(args.system, args.data, args.output)
        run_info = {"seconds": round(seconds, 2), "returncode": code, "stderr_tail": tail}
        if code != 0 or not os.path.exists(args.output):
            print(f"system/run failed (exit {code}) after {seconds:.1f}s\n{tail}")
            return 1
    if not os.path.exists(args.output):
        print(f"no output file at {args.output} (use --run to produce it)")
        return 1

    summary = evaluate(args.data, args.output, labels)
    if run_info:
        summary["run"] = run_info
    print_report(summary, verbose=args.verbose, show_paths=args.paths)

    min_score = args.min_score
    if args.compare:
        with open(args.compare, encoding="utf-8") as handle:
            prev = json.load(handle)
        print_compare(summary, prev)
        prev_score = (prev.get("labels") or {}).get("decision_score")
        if prev_score is not None:
            min_score = prev_score if min_score is None else max(min_score, prev_score)

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")

    failed = gates(summary, min_score)
    print("\nGATES: " + ("PASS" if not failed else "FAIL\n  - " + "\n  - ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
