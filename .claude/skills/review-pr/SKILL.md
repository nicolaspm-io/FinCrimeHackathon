---
name: review-pr
description: Review and validate a pull request (or branch) of this screening-alert system before it is approved. The review is data-first. It runs the pipeline and checks the code, but mostly it attacks the PR's logic, parsers and LLM prompts with the whole dev dataset, to find hits it decides wrongly, misses, or would miss on the unseen set. Use it whenever the user asks to review, verify, validate, evaluate, check or approve a PR or branch here, e.g. "review PR 12", "verifica este PR", "valida la rama X", "/review-pr 7".
argument-hint: <PR number | branch> [quick]
---

# Review a PR of the screening system

Target: `$ARGUMENTS`. The first word is a PR number or branch name. `quick` means a smaller blind sample. If there is no target, use the most recent open PR and say which one you picked.

You are the **independent reviewer**. Another AI wrote this PR, following a plan that may itself be wrong.

**Judge only against:**
- `POLICY.md`, the bank's identity SOP: the ground truth for decisions
- `README.md` §3–6: the data rules, deliverable, constraints and scoring
- the raw data in `dev/`

**Don't read the team's plan, and don't take the PR description's word for anything.** Read the description only to know what the PR *claims*, then check the claims. Your most valuable output is a **concrete hit the PR gets wrong**, proved from raw records, and a **flaw in the approach** the data exposes. Code style matters little.

Costs drive severity:

| Mistake | Cost per hit |
|---|---|
| Clearing a hit whose right answer is `SAME_ENTITY` or `REVIEW` (an unsafe clearance) | 60 |
| Any other wrong decision | 2–3 |

80% of the final score comes from an **unseen** set with new customers and reworded free text. A rule that only works on dev's wording is a defect even when dev's score goes up.

Bundled with this skill (paths are relative to the repo root):
- `.claude/skills/review-pr/checklist.md`: the traps in each POLICY principle and the direction each failure goes. Read it before step 3.
- `.claude/skills/review-pr/scripts/data_atlas.py <data> --out F`: the inventory of every format, wording and edge case in the data.
- `.claude/skills/review-pr/scripts/hit_dossier.py <data> <H-…|A-…>…`: every raw record relevant to a hit. It applies no decision logic.
- `.claude/skills/review-pr/scripts/decision_diff.py <base|none> <pr> --data <data> [--labels L]`: the per-hit diff between two outputs.

Keep everything you generate under `.review/` (it is git-ignored). Never commit to, push to or modify the PR branch: you review, you don't fix. Don't run anything that sends data outside the machine, except the PR's own OpenRouter calls.

## 0. Setup

```bash
ROOT=$(git rev-parse --show-toplevel); cd "$ROOT"
SK=$ROOT/.claude/skills/review-pr; DATA=$ROOT/dev; R=$ROOT/.review
mkdir -p $R/reports $R/wt
```

1. **Resolve the target.**
   - **PR number:** get the title, body, author, base and head branch, the file list and the diff. In a cloud session, load the GitHub MCP tools with ToolSearch (`mcp__github__pull_request_read`); owner and repo come from `git remote get-url origin`. On a local machine, use `gh pr view N --json title,body,author,baseRefName,headRefName,files` and `gh pr diff N`.
   - **Branch name:** the base is `main`.
2. **Fetch by name:** `git fetch origin <head> <base>`. If the head branch lives on a fork, use `git fetch origin pull/N/head:refs/review/pr-N`. If the branch exists only locally, use the local ref as it is.
3. **Create detached worktrees,** so the user's checkout is never touched. `<head-ref>` is `origin/<head>`, `refs/review/pr-N` or the local branch:
   ```bash
   git worktree add --detach $R/wt/pr-N <head-ref>
   git worktree add --detach $R/wt/base origin/<base>
   ```
   Remove stale worktrees first with `git worktree remove --force`. Record both commit SHAs for the report.
4. **Build the atlas once per data version:**
   ```bash
   python3 $SK/scripts/data_atlas.py $DATA --out $R/atlas.md
   ```
   Skim it. It is your map of what the code must handle.

## 1. Inventory what the PR actually does (from the diff, not the description)

Read every changed file under `system/`, plus anything it imports or calls. Write a table into your notes with one row per rule, parser, regex, threshold, mapping, prompt or fallback:

| What | file:line | POLICY principle | Inputs it reads | What happens on missing, unparseable or ambiguous input |
|---|---|---|---|---|

The last column is where the unsafe bugs live. Every path that ends in `DIFFERENT_ENTITY` must rest on a positively established contradiction. Note too what the PR *doesn't* implement yet, and how the code behaves when that piece is missing.

## 2. Run it

For the PR, and for the base if it has a `system/run`:

```bash
cd $R/wt/pr-N && ls -l system/run   # it must be executable; the graders call it directly
T0=$(date +%s); timeout 1800 system/run $DATA $R/pr-N.jsonl > $R/pr-N.stdout 2> $R/pr-N.stderr; echo "exit $? after $(( $(date +%s) - T0 ))s"
python3 $ROOT/validate.py --data $DATA --output $R/pr-N.jsonl --score $DATA/practice_labels.jsonl
```

- **Second run:** if the first run took under 10 minutes, run it again to `$R/pr-N.run2.jsonl`. Then run `decision_diff.py $R/pr-N.jsonl $R/pr-N.run2.jsonl --data $DATA`. Any difference is a reproducibility defect, and reproducibility is 10% of the score.
- **LLM calls:** if the PR calls an LLM and `OPENROUTER_API_KEY` is not set, say so. Run what you can offline and review the prompts statically. Never invent results.
- **Diff against base:**
  ```bash
  python3 $SK/scripts/decision_diff.py $R/base.jsonl $R/pr-N.jsonl --data $DATA --labels $DATA/practice_labels.jsonl --out $R/diff-N.md
  ```
  If the base has no system, use `none` instead of the base file.
- **Every newly cleared hit is a suspect** until you have justified it from raw data, in step 3 or 4.
- **Practice labels:** a perfect score on the 50 labels proves little. Use them to calibrate yourself. Take each label's `path` field as a worked example of how an answer is derived.

## 3. Adversarial data audit (the core; spend most of your effort here)

For each row of the step 1 inventory, start with the matching section of `checklist.md`, then do the following.

**a. Measure coverage on the full data**
- Write throwaway probes in `$R/probes/` (stdlib Python) that import or copy the PR's parser, rule or regex.
- Run them over **every** relevant value in `dev/`: all 72k customer DOBs, all 54k KYC checks, all 22k kyc notes, all profile-note first paragraphs, all search snippets.
- Count the values handled correctly, misparsed and silently dropped, and give examples.
- Compare against the atlas's list of shapes and templates. The long tail is where parsers break.

**b. Test the boundaries.** Build hits or customers at each edge and run the PR's functions on them:
- customer exactly 16 at the earliest dated report
- birth year exactly ±1 from the hit's, and ±2
- an age at a date, which gives two possible birth years
- name parts of length 3/4/6/7, one or two edits apart, and two letters swapped
- a year-only date against a full date, and a year-month date
- a death date that is only a month, against `screened_at`
- empty country sets on either side
- territories, demonyms and ISO codes for the same country
- placeholder names (`N/a`, `0000`, `Xxx`)

**c. Find concrete failures.** Pick the hits the PR decides through each rule. Prioritise:
- every newly cleared hit
- every `LOCATION_MISMATCH` and `NAME_MISMATCH_VERIFIED`
- every hit whose decisive fact came from free text or search results

Run `hit_dossier.py` on them, many IDs per call. Re-derive the answer yourself from POLICY. For each failure, write the record-by-record path, in the style of `practice_labels.jsonl` `path`:

```
account_ref ACC-… → joint holders CUS-a, CUS-b → screened DOB fits CUS-a → phone+DOB → CUS-c (same person) → NOTE-… says lived in Spain → hit tied to Spain → no LOCATION contradiction
```

**d. Generalization**
- Write 10–20 paraphrases of the free-text inputs the PR parses: KYC notes, residence notes, age sentences, search snippets. Change the word order, the date formats ("d.o.b.", "born on the 5th of May 1980", "05.05.1980") and the verbs.
- Run the PR's extraction on them.
- A parser that matches dev's templates but fails the paraphrases is a **Major** finding. It is a **Blocker** when the failure turns a fact into a mismatch rather than into unknown.
- What POLICY §5 keeps stable: the dated-report form and the listing phrases. Everything else may change.

**e. Approach-level flaws.** Step back from single bugs and ask whether the strategy can work on this data at all. Examples:
- resolving one customer before handling joint accounts or multi-record people
- merging records on a shared phone without the DOB check
- trusting the record name over the verified one
- an LLM deciding things POLICY defines exactly: the 16-year rule, ±1 year, edit thresholds
- search results used without the category and country filter
- country comparison without normalisation
- anything that only works because dev's wording is templated

Name what the approach can't handle, the hits that prove it, and what would have to change. **Report these even when the PR's own diff is correct:** the user wants to know if the plan is flawed.

## 4. Blind re-adjudication (independent second opinion)

**Build the sample.** In `quick` mode take about 15 hits; by default about 40. Choose:
- all newly cleared hits (up to 25)
- a random 3–5 of each decision the PR gave
- rare situations, whatever the PR decided:
  - company hits or company customers
  - deceased profiles
  - `AMBIGUOUS` or null customers
  - no-link alerts
  - hits whose DOB comes only from an age sentence or a search result
  - customers with residence-history notes
  - joint accounts
- **3–5 practice-label hits as calibration,** without telling the adjudicators which ones.

**Split the sample across 2–4 `general-purpose` subagents in parallel,** about 10 hits each. Give each one exactly this brief, with the values filled in:

> You are adjudicating watchlist hits for a bank. Decide each hit **only** from `ROOT/POLICY.md` (read it fully first) and the raw data in `DATA`.
>
> To see the records, run `python3 SK/scripts/hit_dossier.py DATA <hit ids>`. If you need more, open only these files, by name: `alerts`, `hits`, `customers`, `accounts`, `onboarding`, `kyc_checks`, `notes` and `search_results` (`.jsonl`), plus `name_variants.json`. Read them with small scripts. **Never glob, list or `head` `DATA/*`:** that folder also holds the answer key.
>
> **Do not** open or run anything under `system/`, `.review/` (except running the dossier script), `output/`, `DATA/practice_labels.jsonl`, any git history or diff, or any plan document. You must not see anyone else's answer. If you see an answer by accident, say which one.
>
> For each hit:
> 1. Resolve the customer (POLICY 2–4).
> 2. Gather the hit's facts (POLICY 5).
> 3. Apply POLICY 6–8 literally: unknown stays unknown, and clear only on a positive contradiction.
>
> Return one JSON line per hit with these fields: `hit_id`, `decision`, `reason_code`, `customer_record_id` (or `AMBIGUOUS` or null), `decisive_facts` (each with its record ID), `path` (the list of steps), `confidence` (high, medium or low), and `policy_doubt` (any point where POLICY was unclear for this hit).
>
> Hits: <ids>

**Compare** the adjudicators' answers with the PR's output. Investigate **every** disagreement yourself with the dossier, and classify it:
- **PR bug:** a finding.
- **Adjudicator error:** discard it, and note it if the same pattern repeats.
- **POLICY ambiguity:** report it as a risk, with both readings.

If the adjudicators miss calibration hits, discount their answers in that area.

## 5. Code and pipeline checks

Check each of these, and say pass or fail with evidence:

- [ ] `system/run <data> <out>` works from any directory, is executable, reads **only** `<data>` (grep for `dev/`, absolute paths and `practice_labels`), and writes one line per hit.
- [ ] Standard library only (Python 3 or Node), or shell. No `pip install` and no vendored heavy dependencies.
- [ ] **No leakage.** No hard-coded hit, customer, note or alert IDs, and no dev-specific name or value tables (grep the diff for `H-`, `CUS-`, `NOTE-`, `A-`). Nothing derived from practice labels.
- [ ] LLM calls go only to OpenRouter, with the key from `OPENROUTER_API_KEY` and never committed. Temperature 0, a seed or a response cache, and timeouts and retries. **Every failure path ends in REVIEW.** The model's output is validated before use. The model and prompt are fixed in code.
- [ ] Deterministic: no iteration over sets or unordered sources that affects the output, no wall-clock dates (use `screened_at`), and stable sorting.
- [ ] The run takes under 30 minutes (on macOS, use `gtimeout` or drop `timeout`). Extrapolate from dev's 819 hits to about 550 unseen hits plus a new customer base, and flag O(n²) scans.
- [ ] `validate.py` reports 0 problems. Evidence cites only the decisive records, and every ID exists. Each reason states this hit's facts in 2–4 sentences.
- [ ] No data, keys or large files are newly committed. The PR doesn't break existing files (for example `validate.py`) or other parts of the pipeline.
- [ ] `system/README.md` stays honest if the PR touches the behaviour it describes.

## 6. Report

Write `$R/reports/pr-N.md` (for a branch, `$R/reports/<branch>.md`), then give the user a short summary in chat: the verdict, the blockers, and the top 3 findings, with the report path.

```markdown
# Review of PR #N — <title>
**Verdict:** APPROVE | APPROVE WITH NOTES | REQUEST CHANGES — <one-sentence why>
Reviewed commit <sha> against <base sha>, data `dev/` (819 hits). Mode: default|quick.

## Metrics
| | base | PR |
|---|---|---|
| validate.py problems | | |
| practice decision score / right / unsafe | | |
| DIFF / SAME / REVIEW (review rate) | | |
| newly cleared / no longer cleared / other changes | – | |
| reproducible (run 1 vs run 2) | | |
| runtime, peak memory | | |
| blind sample agreement (excluding adjudicator errors) | – | |

## Blockers
### B1. <title>
- **Rule:** `system/…py:123`, <what it does>
- **Data evidence:** <counts over the full data>; e.g. H-…, H-… (path: …)
- **Impact:** <n hits in dev × 60>; why it carries over to the unseen set
- **Reproduce:** `python3 .review/probes/b1.py` (or an inline snippet)
- **Fix:** <concrete change>

## Major
## Minor
## Flaws in the approach
<strategy-level problems the data exposes, even if this PR's diff is correct — with hits as proof and what would need to change>

## Policy ambiguities found
## What I could not check
<e.g. no OPENROUTER_API_KEY; runtime not measured; …>
```

**Verdict rule:**
- `REQUEST CHANGES` if there is any blocker:
  - a plausible unsafe clearance, proved on at least one dev hit or by a boundary or paraphrase test that turns unknown into a contradiction
  - leakage or hard-coding
  - a broken or non-reproducible run
  - more than 30 minutes of runtime
- `APPROVE WITH NOTES` if there are only Major or Minor findings.
- `APPROVE` if there is nothing material.

Don't soften a blocker because the dev score went up.

## 7. Posting to GitHub (only after the user says yes)

Ask: "Post this review to PR #N as <APPROVE|REQUEST_CHANGES>?" Post only on a clear yes.

- **Cloud session:** use `mcp__github__pull_request_review_write`, method `create`, with event `APPROVE`, `REQUEST_CHANGES` or `COMMENT`. For findings tied to a line, create a pending review first, add the line comments with `mcp__github__add_comment_to_pending_review`, then submit it.
- **Locally:** run `gh pr review N --approve|--request-changes|--comment --body-file $R/reports/pr-N.md`.
- **Your own PR:** GitHub refuses `APPROVE` and `REQUEST_CHANGES` from the PR's author account. Then post as `COMMENT` and tell the user they must approve it themselves.
- End the posted body with:

  ```

  ---
  _Generated by [Claude Code](https://claude.ai/code)_
  ```

Afterwards, leave `.review/` in place for follow-up questions. Run `git worktree remove` only when the user asks or a new review needs the slot.
