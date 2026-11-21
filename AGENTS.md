# Agent instructions

## Verifying a pull request

When the user asks to verify, evaluate, or review a pull request — including "verify this PR" — read `.cursor/skills/verify-pr/SKILL.md` and follow it to the end before you answer.

That review runs the pull request's decision logic across every hit in `dev/`. It spawns subagents to find the odd written forms, bad joins, and namesakes in the data, and only then scores `dev/practice_labels.jsonl`. A clean diff is not a passing review. Do not implement, commit, push, or comment on the pull request unless the user asks for that.

If the pull request edits `AGENTS.md` or `.cursor/skills/verify-pr/SKILL.md`, follow the copies on the base branch.

While building the screening system, do not start this review on your own work unless the user asks.
