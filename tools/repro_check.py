#!/usr/bin/env python3
"""Run system/run N times in fresh processes and check the outputs agree.

    python3 tools/repro_check.py --data dev --runs 5
    python3 tools/repro_check.py --data dev --runs 5 --no-llm          # deterministic-only (no OPENROUTER_API_KEY)
    python3 tools/repro_check.py --data dev --runs 3 --hide-labels     # practice_labels.jsonl not visible to the system
    python3 tools/repro_check.py --data dev --keep out/repro           # keep the N output files

Each run is a separate process with:
  - its own empty working directory (so no cache or relative path is shared between runs),
  - its own empty XDG_CACHE_HOME and TMPDIR,
  - a different PYTHONHASHSEED (1..N), so code that depends on set/dict-of-str ordering shows up,
  - PYTHONDONTWRITEBYTECODE=1.
It also reports any file the runs create or change inside the repository (the system should
only write its output file).

Gate (exit 1 on failure):
  - every run exits 0 and writes a file, each within 30 minutes,
  - run 1 passes validate.py with 0 problems,
  - every hit has the same (decision, reason_code) in all runs   <- what the organisers score.
Differences in customer_record_id, reason, evidence or line order are reported as warnings;
--strict-full turns them into failures too.
"""

import argparse
import collections
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_dev import (DEFAULT_SYSTEM, REPO, TIME_BUDGET_S, load_output_rows, load_validate,  # noqa: E402
                      run_system)

FIELDS = ("alert_id", "decision", "reason_code", "customer_record_id", "reason", "evidence")
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def snapshot(root, exclude):
    """{relative path: (size, mtime_ns)} for files under root, skipping .git and the excluded dirs."""
    exclude = [os.path.abspath(e) for e in exclude]
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and not any(os.path.abspath(os.path.join(dirpath, d)).startswith(e) for e in exclude))
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return out


def data_without_labels(data, into):
    """A folder of symlinks to every file in `data` except practice_labels.jsonl."""
    os.makedirs(into, exist_ok=True)
    for name in sorted(os.listdir(data)):
        if name == "practice_labels.jsonl":
            continue
        os.symlink(os.path.abspath(os.path.join(data, name)), os.path.join(into, name))
    return into


def run_env(index, scratch, no_llm):
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(index)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for var in ("XDG_CACHE_HOME", "TMPDIR"):
        path = os.path.join(scratch, var.lower())
        os.makedirs(path, exist_ok=True)
        env[var] = path
    if no_llm:
        env.pop("OPENROUTER_API_KEY", None)
    return env


def compare(runs):
    """runs: list of row lists. Returns (key_diffs, field_diffs, order_diffs, n_hits)."""
    maps = [{r.get("hit_id"): r for r in rows} for rows in runs]
    all_hits = sorted(set().union(*[set(m) for m in maps]), key=str)
    key_diffs, field_diffs = [], collections.defaultdict(list)
    for hit_id in all_hits:
        rows = [m.get(hit_id) for m in maps]
        keys = [(r.get("decision"), r.get("reason_code")) if r else ("<missing>", None) for r in rows]
        if len(set(keys)) > 1:
            key_diffs.append((hit_id, keys))
        for field in FIELDS:
            values = [repr(r.get(field)) if r else "<missing>" for r in rows]
            if len(set(values)) > 1:
                field_diffs[field].append(hit_id)
    first_order = [r.get("hit_id") for r in runs[0]]
    order_diffs = [i + 1 for i, rows in enumerate(runs) if [r.get("hit_id") for r in rows] != first_order]
    return key_diffs, field_diffs, order_diffs, len(all_hits)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.path.join(REPO, "dev"), help="data folder (default: dev/)")
    ap.add_argument("--runs", type=int, default=5, help="number of runs (default 5)")
    ap.add_argument("--system", default=DEFAULT_SYSTEM, help="executable (default: system/run)")
    ap.add_argument("--no-llm", action="store_true", help="unset OPENROUTER_API_KEY for the runs")
    ap.add_argument("--hide-labels", action="store_true", help="run against a copy of --data without practice_labels.jsonl")
    ap.add_argument("--keep", help="copy the run outputs into this folder (run1.jsonl ...)")
    ap.add_argument("--strict-full", action="store_true", help="also fail on differences outside (decision, reason_code)")
    ap.add_argument("--timeout", type=float, default=TIME_BUDGET_S + 60, help="seconds before a run is killed")
    args = ap.parse_args(argv)

    if args.runs < 2:
        ap.error("--runs must be at least 2")
    if not os.path.exists(args.system):
        print(f"no system at {args.system}")
        return 1

    scratch_root = tempfile.mkdtemp(prefix="repro_check_")
    failed = []
    try:
        data = args.data
        if args.hide_labels:
            data = data_without_labels(args.data, os.path.join(scratch_root, "data"))
        before = snapshot(REPO, [scratch_root] + ([args.keep] if args.keep else []))

        outputs, runs = [], []
        llm = "off (--no-llm)" if args.no_llm else ("on" if os.environ.get("OPENROUTER_API_KEY") else "off (no key set)")
        print(f"system {args.system}\ndata   {data}\nruns   {args.runs}, OpenRouter key {llm}")
        for i in range(1, args.runs + 1):
            scratch = os.path.join(scratch_root, f"run{i}")
            cwd = os.path.join(scratch, "cwd")
            os.makedirs(cwd)
            output = os.path.join(scratch, "out", f"run{i}.jsonl")
            seconds, code, tail = run_system(args.system, data, output, env=run_env(i, scratch, args.no_llm),
                                             cwd=cwd, timeout=args.timeout)
            exists = os.path.exists(output)
            rows = load_output_rows(output) if exists else []
            print(f"  run {i}: {seconds:7.1f}s  exit {code}  {len(rows)} lines")
            if code != 0 or not exists:
                failed.append(f"run {i} exited {code}" + ("" if exists else " and wrote no output"))
                if tail:
                    print("    stderr tail:\n      " + tail.replace("\n", "\n      "))
            if seconds > TIME_BUDGET_S:
                failed.append(f"run {i} took {seconds:.0f}s (> 30 min)")
            stray = [p for p in os.listdir(cwd)]
            if stray:
                print(f"    note: run {i} wrote into its working directory: {', '.join(sorted(stray)[:5])}")
            outputs.append(output)
            runs.append(rows)

        after = snapshot(REPO, [scratch_root] + ([args.keep] if args.keep else []))
        touched = sorted(p for p in after if before.get(p) != after[p])
        if touched:
            print(f"  warning: the runs created or changed {len(touched)} file(s) in the repo: " + ", ".join(touched[:10]))

        if os.path.exists(outputs[0]):
            _, problems = load_validate().validate(data, outputs[0])
            print(f"validate.py on run 1: {len(problems)} problems")
            for p in problems[:10]:
                print(f"  {p}")
            if problems:
                failed.append(f"run 1 has {len(problems)} validate.py problems")

        if all(runs):
            key_diffs, field_diffs, order_diffs, n = compare(runs)
            stable = n - len(key_diffs)
            print(f"(decision, reason_code) identical in all {args.runs} runs: {stable}/{n} hits")
            for hit_id, keys in key_diffs[:20]:
                print(f"  {hit_id}: " + " | ".join(f"{d}/{c}" for d, c in keys))
            if key_diffs:
                failed.append(f"{len(key_diffs)} hits change decision or reason_code between runs")
            for field in FIELDS:
                if field in ("decision", "reason_code"):
                    continue
                ids = field_diffs.get(field, [])
                if ids:
                    print(f"  warning: {field} differs on {len(ids)} hits, e.g. {', '.join(ids[:5])}")
                    if args.strict_full:
                        failed.append(f"{field} differs on {len(ids)} hits")
            if order_diffs:
                print(f"  warning: line order differs from run 1 in runs {order_diffs}")
                if args.strict_full:
                    failed.append("line order differs between runs")
            if not key_diffs and not any(field_diffs.values()) and not order_diffs:
                print("all runs byte-for-byte equivalent at the JSON level")

        if args.keep:
            os.makedirs(args.keep, exist_ok=True)
            for path in outputs:
                if os.path.exists(path):
                    shutil.copy2(path, os.path.join(args.keep, os.path.basename(path)))
            print(f"outputs kept in {args.keep}")
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    print("\nGATES: " + ("PASS" if not failed else "FAIL\n  - " + "\n  - ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
