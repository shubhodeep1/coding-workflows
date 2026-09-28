# Claude-fixer review: a failed reviewer slot is a missing vote, not a finding

Source issue: shubhodeep1/coding-workflows#4835 (https://github.com/shubhodeep1/coding-workflows/issues/4835)
Base branch: main
Security pass: run

## Summary

The Claude-fixer clean-ledger check treats a reviewer slot that failed (retry
limit reached, killed, timed out) as a finding, so one stalled slot turns an
otherwise clean review into a zero-finding hand-off that nobody can resolve.
This plan makes a verified failed slot a missing vote: the ledger is clean when
the consensus is empty, no completed reviewer reported anything, and at least
`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` (default 5) reviewers completed clean.

## Context

- Incident (2026-09-28): final PR #4695 of project #4687, run 36416865588. Five
  reviewers and the consensus reported `(No findings reported.)`; the sixth
  slot, `minimax/minimax-m3`, was killed on all three attempts. Its ledger block
  read `Reviewer minimax/minimax-m3 failed after reaching the slot
  retryable-failure limit (3).` The hand-off step posted a `kind=findings`
  hand-off with 0 entries, and without a verdict bot the project stopped
  (`Q3` on #4687; #4586 paused behind it). #4747 hit a similar zero-finding stall.
- `scripts/review_autofix_step_claude_fixer_handoff.sh` lines 117–128: the awk
  clean check requires every `CONSENSUS FINDINGS`, `CONSENSUS TASK GAPS` and
  `FINDINGS FROM <slug>` block to hold exactly its empty line.
- `scripts/review_run_reviewers.sh` lines 4440–4447, 4689–4700 and 4728–4731
  write the terminal failure line into the slot's output file
  (`${PREVIOUS_REVIEWS_DIR}/review_<safe_name>.txt`) and `failed` into
  `${PREVIOUS_REVIEWS_DIR}/status_review_<safe_name>.txt`, where
  `safe_name = model | tr '/.:' '___'` (line 4791). A successful slot's status
  file reads `success` (line 223). `scripts/summarize_reviewer_consensus.sh`
  passes the failure line to the summariser, which copies it into that slot's
  `FINDINGS FROM <safe_name>` block. `PREVIOUS_REVIEWS_DIR` is exported to
  `GITHUB_ENV` in the codex-agent job (`review_autofix.yml` "Initialize runtime
  workspace"), the same job as the hand-off step.
- The Claude-fixer convergence project (`docs/plans/claude-fixer-unattended-convergence-plan.md`,
  branch `claude/implement-plan-claude-fixer-unattended-convergence`) edits the
  same script (evidence, checks-pending, sticky rulings) but not the clean-check
  block this plan replaces, so the two merge independently.

## Goals

- A ledger whose only non-empty per-reviewer blocks are verified failed slots is
  clean when at least `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` reviewers completed
  clean, and takes the existing clean path (auto-merge only with a fresh,
  ready, same-head check snapshot).
- Fewer completed clean reviewers than the minimum still hands off, as today.
- A failed slot never counts as a finding and never as a clean vote. Only an
  exact `(No findings reported.)` from a slot whose status file reads
  `success` counts as a clean vote on this path.
- A block counts as a failed slot only when its single line is one of the
  reviewer runner's terminal failure lines for that block's own model and the
  slot's status file reads `failed`. Any other text stays a finding.
- The failed slots are recorded: log key `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS`
  on the clean path, and a line in the hand-off comment whenever failed slots
  were recognised but the round was handed off anyway.
- New repo variable `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS`, default `5`, documented
  in `README.md` and `agents.md` (§4).

## Non-goals

- Skipped slots (`skipped_budget`, `skipped_unmapped`, cached-open) and slots that
  failed on a non-retryable error: their blocks stay findings (AD-3).
- Changing the reviewer runner, the summariser, or the ledger format.
- The convergence project's checks-pending, judge and evidence work.

## Constraints

- §1: security first. The relaxed path is gated on runner-written status files,
  never on ledger text alone (the ledger is LLM output over reviewer output).
- §4: the new variable has a default (`5`) in the workflow and in the script.
- §6: no identifier is renamed; `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS` and
  `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` are new and collide with nothing.
- §9: the step script keeps its 2-space bash style; YAML stays 2-space.
- §15: no new GitHub API call; status files are local.
- §20: one `changelog.d/` fragment. §27: `review_autofix.yml` gains one env line
  (451,634 bytes today, far under 480,000).

## Approach

Replace the awk clean check with a classifier that walks the ledger once:

1. Consensus blocks must be exactly their empty line (unchanged).
2. Each `FINDINGS FROM <slug>` block is classified: `clean` (only line is
   `(No findings reported.)`), `failed` (only line matches
   `^Reviewer (\S+) failed after (reaching the slot retryable-failure limit \([0-9]+\)|retryable failure recovery was exhausted|[0-9]+ attempts)\.$`
   and the model, mapped with `tr '/.:' '___'`, equals the slug), or `finding`.
   A duplicated slug or a slug outside `[A-Za-z0-9_-]` is a finding.
3. No failed block → today's rule: clean when every block is clean.
4. At least one failed block → clean only when no block is a finding, every
   failed block's `status_review_<slug>.txt` reads `failed`, every clean
   block's status file reads `success`, and the clean count is at least the
   minimum. Otherwise the round is handed off with a line naming the failed
   slots and the clean count.

Alternatives: text-only matching (rejected, a reviewer could forge the line),
applying the minimum to every ledger (rejected, it would stall smaller panels
that pass today; AD-1).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
issue fixes the scope, and the change is one step script, its tests, one
workflow env line, and docs.

1. **Phase 1 — failed slots are missing votes.** Files: see Files & Modules.
   Done when the four issue test cases and the status-file and bound checks
   pass, existing Claude-fixer tests stay green, and docs name the variable.
   Rollback: revert the PR; the old all-blocks-clean rule returns.

## Implementation Steps

1. `scripts/review_autofix_step_claude_fixer_handoff.sh`: replace the clean
   check (lines 117–128) with the classifier above; read
   `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` (default 5; a non-integer or a value below
   1 warns and uses 5) and `PREVIOUS_REVIEWS_DIR`; log
   `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS pr=… head=… round=… failed_slots=… clean_reviewers=… min=…`
   when the ledger is clean with failed slots; add the failed-slot line to the
   findings hand-off body; document the new inputs in the header.
2. `.github/workflows/review_autofix.yml`: add
   `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS: ${{ vars.CLAUDE_FIXER_MIN_CLEAN_REVIEWERS || '5' }}`
   to the hand-off step's `env:`.
3. `tests/test_review_autofix_claude_fixer_mode.py`: extend `_run_handoff` with
   status files and the minimum, then add: 5 clean + 1 failed → clean; 4 clean
   + 2 failed → hand-off naming the failed slots; 5 clean + 1 finding →
   hand-off; a forged failure line inside a finding block → finding; a failure
   line whose status file reads `success` → finding; a failure line for another
   model → finding; an invalid minimum falls back to 5; the workflow wires the
   variable.
4. `README.md` (variable table and the Claude-fixer section) and `agents.md`
   (Claude-fixer mode paragraph): document the rule, the variable and the log key.
5. `changelog.d/4835-failed-reviewer-slot-missing-vote.md` (`fixed`).

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `.github/workflows/review_autofix.yml`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `changelog.d/4835-failed-reviewer-slot-missing-vote.md` [new]

## Testing

`python3 -m pytest tests/test_review_autofix_claude_fixer_mode.py tests/test_workflow_file_size_limit.py`
plus the review_autofix contract tests that expand step scripts
(`tests/test_review_autofix_*.py`), `bash -n` and `shellcheck` on the script.

## Risks

- The summariser rewrites the failure line (adds a bullet, paraphrases): the
  block stays a finding and the round hands off, which is today's behaviour.
- A same-head resume reuses cached status files, which are the ones the ledger
  was built from, so the cross-check still holds.
- Merge with the convergence project: both edit the same script in different
  hunks; the later merge resolves mechanically.

## Rollout

Ships to consumers with the next `@stable` sync. No consumer wrapper change:
`vars` resolves in the reusable workflow. Setting
`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` above the panel size restores the old
behaviour for failed-slot ledgers.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Does the minimum-clean-reviewers rule apply to every ledger or only when a slot failed? — Picked: A — only when at least one verified failed slot is present; a ledger without one keeps today's every-block-clean rule. Alternatives: B — every ledger. Why: B would stall panels with fewer than 5 reviewers that merge today (§1 backward compatibility); the issue scopes the minimum to failed-slot ledgers ("still fails closed, as today"). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] How is a failed slot proven? — Picked: A — the block's single line is the runner's terminal failure line for that block's own model, and `status_review_<slug>.txt` reads `failed`. Alternatives: B — the failure line text alone. Why: the ledger is LLM output over reviewer output, so text alone is forgeable; the status file is written by the runner only (§1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] Which slot outcomes count as failed? — Picked: A — the three retry-exhaustion lines (slot retryable-failure limit, retryable failure recovery exhausted, failed after N attempts). Alternatives: B — also non-retryable errors and skipped slots. Why: those three are the "retry limit reached, killed, or timed out" cases the issue names, with fixed text that parses strictly; the others keep failing closed (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] Must a clean vote on the failed-slot path be backed by a `success` status file? — Picked: A — yes. Alternatives: B — the `(No findings reported.)` text alone. Why: the issue requires an exact, reviewer-produced empty result; the status file proves the reviewer completed (§1). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] What does an invalid `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` (non-integer or below 1) do? — Picked: A — warn and use the default 5. Alternatives: B — treat every failed-slot ledger as a hand-off. Why: 5 is the documented default and keeps the check strict; 0 must never make a ledger with no clean vote clean. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] Coordinate with the Claude-fixer convergence project? — Picked: A — land on main independently, confined to the clean-check block and one hand-off line, so the convergence branch merges mechanically. Alternatives: B — wait for that project to finish. Why: the stall blocks projects now, and the hunks do not overlap. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` reported `{"skip": false, "reason": "no skip label"}`.
