# Rejection votes never demote a finding without an automated disproof

Source issue: shubhodeep1/coding-workflows#5582 (https://github.com/shubhodeep1/coding-workflows/issues/5582)
Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
Security pass: skip (ai:security: automation-produced issue)

## Summary

Reviewer `REJECTED_FINDING` votes, even a verified majority with quotes, no longer move a single-reviewer finding out of the Claude-fixer hand-off. Demotion now also requires an independent automated check that proves the specific finding false. No such check exists in production, so every single-reviewer finding stays blocking and reaches the Claude fixer. This closes security finding #5582 (A08, high) at `scripts/review_claude_fixer_nonblocking.py:617`.

## Context

- Issue #4586's project (the base branch above, final PR #4593, still a draft) lets the Claude-fixer hand-off gate move a CONSENSUS FINDINGS entry raised by one reviewer into a `NON-BLOCKING FINDINGS` block. That happens when a strict majority of the other successful pass-2 reviewers (at least two) reject it. When that entry was the last finding, the round takes the zero-findings path and `review_autofix.yml` auto-merges the PR (`scripts/review_autofix_step_claude_fixer_handoff.sh:195-207`).
- Earlier follow-ups hardened the vote. #4688 binds a vote to a run-issued `RF-<16 hex>` ID. #4687 binds the ID to a pass-1 `consensus_id`. #4976 requires `evidence: <file>:<range> | quote: <text>` that the gate finds at those lines of the reviewed commit (`_verify_evidence`, `_votes_in`, `scripts/review_claude_fixer_nonblocking.py:579-620`). #4975 (a sibling follow-up in flight on its own project branch) binds the flagger's citation to its own structured finding.
- #5582 (this issue) reports that a matching quote proves only that the reviewer copied text from the file, not that the finding is false. A PR author can prompt-inject reviewers into copying the run-issued ID and nearby source text into rejection votes, even text that *demonstrates* the defect. A majority of such votes demotes the last finding and permits auto-merge. The recommendation: "Treat reviewer votes and PR source as untrusted data. Require independently verifiable automated proof that the specific finding is false; otherwise keep it blocking for the unattended fixer."
- Every vote field (ID, reason, evidence, quote) is written by a model whose input includes PR-controlled text. No verification of those fields can turn a model's opinion into proof, so this plan stops using votes as the deciding signal.
- The Claude fixer already judges every handed-off finding against the code (`.claude/commands/implement-plan-claude.md` step 7a). When `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is configured, it answers invalid ones through that verdict bot and a fresh reviewer panel; with it empty (the default), it leaves the PR blocked for a human to merge by hand or rework. A single-reviewer false positive therefore still has a path to a merge without the demotion, automatic only when the verdict bot is configured.

## Goals

- G1: `demote_with_diagnostics` / `demote` demote an entry only when every existing condition holds **and** a caller-supplied `disproof_check` returns exactly `True` for that entry. Without a `disproof_check` (the CLI and the hand-off step pass none), nothing is demoted. The output ledger is then byte-for-byte the input.
- G2: A single-reviewer entry that passes every existing check (a strict majority of verified votes) but has no automated disproof stays blocking. It is logged as `CLAUDE_FIXER_NONBLOCKING_KEPT file=<file:span> flagged_by=<slug> reason=no_automated_proof`. All earlier keep reasons (`too_few_rejecters`, `flagger_did_not_cite`, …) are still reported first, unchanged.
- G3: The exact #5582 exploit is covered by a regression test. A majority of reviewers cast votes with a valid run ID, verified evidence, and a quote of the defective line itself. The finding stays blocking both in the module and end-to-end through the hand-off step (`nonblocking=0`, `kind=findings`, no `action=auto_merge`).
- G4: Docs and prompts no longer promise that rejected singletons skip the fixer. That covers the pass-2 cross-pollination header sentence (`scripts/review_run_reviewers.sh:5073`), the hand-off step's header comment, the script docstring and `NON-BLOCKING FINDINGS` preamble, `README.md`, `agents.md`, and `docs/INVENTORY.md`.

## Non-goals

- No automated disproof mechanism is built here. Any future one is its own issue; it would plug in through `disproof_check`.
- No change to vote parsing, the ID manifest, `consensus_id` binding, evidence verification, `MIN_REJECTERS`, `LINE_TOLERANCE`, or the stable log lines. Votes stay as run-log diagnostics.
- No removal of the `NON-BLOCKING FINDINGS` block handling in the hand-off step, `--issue-ids`, or `--annotate` (§6). They simply never see a demoted entry in production.
- No change to `.claude/**` or `workflow-templates/.claude/**`. Their text says `NON-BLOCKING FINDINGS` entries need no fix, which stays true of any entry that ever lands there.
- No edit to other projects' changelog fragments (`changelog.d/4586-…`, `4687-…`, `4688-…`, `4976-…`). The new fragment states the change.

## Constraints

- §1: security first. The safer option wins, and every failure path keeps the finding blocking.
- §5: minimal change set. One gate added at the end of the existing checks. No refactor.
- §6: no identifier renamed or removed. `demote`, `demote_with_diagnostics`, `MIN_REJECTERS`, the log lines, and the keep reasons stay. `disproof_check` and `no_automated_proof` are new and unique in the repository (checked with `grep -rn` over `scripts/`, `tests/`, and the docs).
- §7: `README.md` / `agents.md` are updated because behaviour changes.
- §9: tabs in Python; the shell script keeps its existing 2-space style.
- §15: no GitHub API call is added.
- §20: one `changelog.d/5582-rejection-votes-need-automated-proof.md` fragment (`security`).
- §27: no workflow file changes.

## Approach

Add a keyword argument `disproof_check` (default `None`) to `demote_with_diagnostics` and `demote`. In the per-entry loop, after the `too_few_rejecters` check and before any bullet moves, build the record the demotion would append (`path`, `lines`, `flagger`, `consensus_id`, `rejecters`, `others`). Demote only when `disproof_check is not None and disproof_check(record) is True`; otherwise `keep("no_automated_proof")`. Because the check runs last, the log still names the first failing condition for every other entry. Production callers (the CLI, and through it the hand-off step) pass nothing, so votes can never demote a finding. Tests pass a stand-in check to keep exercising the vote-binding mechanics, which remain the necessary conditions.

Alternatives considered (auto-decided, see AD-1):
- An opt-in repo variable that re-enables vote-only demotion (default off). Rejected: it keeps a switch that reopens a high-severity hole (§1).
- Building an automated disproof now (for example a reviewer-named test that must pass). Rejected: no generic mechanism exists that proves an arbitrary review finding false, and designing one is far beyond a single-issue fix (§5).

## Phases & Merge Strategy

A single phase, as issue mode authorises (CLAUDE.md §28.A: `/implement-issue-claude` always writes a single-phase plan). The change is one gate plus its tests and docs. Splitting it would leave either the docs or the gate stale at a merge.

1. **Phase 1 — votes alone never demote.** Files: `scripts/review_claude_fixer_nonblocking.py`, `scripts/review_run_reviewers.sh`, `scripts/review_autofix_step_claude_fixer_handoff.sh` (comment only), `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `README.md`, `agents.md`, `docs/INVENTORY.md`, `changelog.d/5582-rejection-votes-need-automated-proof.md` [new]. Done when every goal G1–G4 holds, `tests/test_review_claude_fixer_nonblocking.py` and `tests/test_review_autofix_claude_fixer_mode.py` pass, and the repo's CI-equivalent checks for the touched files pass (`tests/inventory_parity.py`, `bash -n` on the two shell scripts, the changelog fragment lint). Rollback: revert the phase PR. The gate returns to vote-based demotion, which is the base branch's current behaviour.

## Implementation Steps

Phase 1:

1. `scripts/review_claude_fixer_nonblocking.py`:
   - `demote_with_diagnostics(..., disproof_check=None)`: after the `too_few_rejecters` check, build the candidate record. Call `keep("no_automated_proof")` and `continue` unless `disproof_check` is set and returns exactly `True`. Reuse the record for `demoted.append`.
   - `demote(..., disproof_check=None)`: pass it through.
   - Module docstring, both function docstrings, and the `NON-BLOCKING FINDINGS` preamble: state that votes are necessary but never sufficient (issue #5582), and that without a disproof check (the CLI never has one) nothing is demoted. Add `no_automated_proof` to the documented keep reasons.
2. `scripts/review_run_reviewers.sh`: replace the header sentence "A finding raised by one reviewer and rejected this way by a majority of the others is not handed to the fixer." with one saying that a rejection stays in the reviewer's output and is counted in the run log's diagnostics, and does not stop the finding from reaching the fixer, which checks every finding against the code. Update the comment block above `build_cross_pollination_summary` to match.
3. `scripts/review_autofix_step_claude_fixer_handoff.sh`: update the header comment paragraph (lines 25-43) and the inline comment at the filter call to say that votes alone never demote (issue #5582). No logic change.
4. Tests (`tests/test_review_claude_fixer_nonblocking.py`):
   - `_run` / `_kept` pass a stand-in `disproof_check` that always proves, so the existing mechanics tests keep testing the necessary conditions.
   - New: the #5582 exploit (majority votes whose evidence quotes the defective line) with no `disproof_check` gives `demoted == []`, `text == ledger`, and keep reason `no_automated_proof`.
   - New: a check returning `False` or a truthy non-`True` value keeps the entry blocking. The check receives the record fields `path`, `lines`, `flagger`, `consensus_id`, `rejecters`, `others`. It is not called for an entry that fails an earlier condition.
   - Update the CLI tests (`test_cli_writes_output_and_log_lines`, `test_each_reviewer_output_is_read_once_and_statuses_scanned_once`) to expect `demoted=0`, an unchanged output ledger, and `CLAUDE_FIXER_NONBLOCKING_KEPT file=README.md:1261 … reason=no_automated_proof`.
   - New: the pass-2 header in `scripts/review_run_reviewers.sh` no longer contains "is not handed to the fixer".
5. Tests (`tests/test_review_autofix_claude_fixer_mode.py`): the end-to-end hand-off tests that expected `nonblocking=1` and auto-merge from majority votes now expect `nonblocking=0`, no `NON-BLOCKING FINDINGS` block, and the finding handed off (`kind=findings`).
6. Docs: `README.md` ("Rejected single-reviewer findings are not handed off." paragraph and the keep-reason list), `agents.md` (the review-autofix bullet), and `docs/INVENTORY.md` (the script's entry) say that votes are diagnostics that never demote on their own. Add `no_automated_proof` to the keep-reason list.
7. `changelog.d/5582-rejection-votes-need-automated-proof.md` (`<!-- changelog: security -->`), per §20.

## Files & Modules

- `scripts/review_claude_fixer_nonblocking.py`
- `scripts/review_run_reviewers.sh`
- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `tests/test_review_claude_fixer_nonblocking.py`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`
- `agents.md`
- `docs/INVENTORY.md`
- `changelog.d/5582-rejection-votes-need-automated-proof.md` [new]
- `docs/implement-plan/issue-5582-rejection-votes-need-automated-proof.md` [new] (progress log)

## Tests

- Unit: `tests/test_review_claude_fixer_nonblocking.py` (the new and updated cases above).
- Integration: `tests/test_review_autofix_claude_fixer_mode.py` runs the real hand-off step with the real filter and a real git source.
- CI parity: `python3 -m pytest -q -p no:cacheprovider tests/test_review_autofix_claude_fixer_mode.py tests/test_review_claude_fixer_nonblocking.py` (the `ci.yml` step), `python3 tests/inventory_parity.py`, `bash -n` on both shell scripts, and the changelog-fragment tests.

## Risks & Mitigations

- More review rounds: single-reviewer false positives now reach the Claude fixer instead of being demoted. ACCEPTED: the fixer judges each finding and answers invalid ones through the verdict bot and a fresh reviewer panel, or, with no verdict bot configured (the default), leaves the PR blocked for a human. The cost is time and, without the bot, a human merge, not safety.
- Merge conflict with #4975's project, which edits the same function region and the same README / agents.md paragraphs. Mitigation: both merge into the #4586 project branch through reviewed PRs. The step 7a / Claude-fixer conflict flow resolves the conflict keeping both changes.
- Doc drift in `.claude/` command text. ACCEPTED: it describes the `NON-BLOCKING FINDINGS` block's meaning, which is unchanged. The block is simply never produced.

## Rollout

Lands on the #4586 project branch through the phase PR, then reaches `main` with #4586's final PR #4593. No flag and no migration. Consumer repos get it on the next `@stable` sync with the rest of the #4586 project. Rollback: revert the phase PR on the project branch.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should the gate stop quoted-source votes from demoting a finding? — Picked: A — demote only when a caller-supplied `disproof_check` independently proves the finding false. None exists in production, so votes never demote on their own; they stay as diagnostics. Alternatives: B — an opt-in repo variable that re-enables vote-only demotion (default off); C — build an automated disproof mechanism now. Why: model-written votes are untrusted however they are verified; B keeps a switch that reopens the hole (§1), and C has no generic design and is far beyond this issue (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the new keep reason sit among the existing checks? — Picked: A — last, after `too_few_rejecters`, as `no_automated_proof`. Alternatives: B — first, for every single-reviewer entry. Why: the log keeps naming the first failing condition, which the #4586-#4976 diagnostics depend on (§8). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] What happens to the pass-2 rejection instructions? — Picked: A — keep issuing IDs and asking for `REJECTED_FINDING` lines, but reword the sentence that promises a rejected singleton is not handed to the fixer. Alternatives: B — stop issuing IDs and asking for votes; C — leave the text as it is. Why: B removes a mechanism and its log lines (§6) that a future disproof check would build on; C leaves a false statement in the reviewer prompt. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Should other projects' changelog fragments and the `.claude/` command docs be edited? — Picked: A — no; the new `security` fragment states the change, and the `.claude/` text stays true of the (now never produced) `NON-BLOCKING FINDINGS` block. Alternatives: B — edit the #4586/#4976 fragments and the `.claude/` twins. Why: smallest change (§5), and no protected-path edit in an unattended session (§28.C). Applied in: phase 1. Status: pending review

## References

- Issue #5582 (this finding), security audit tracker #3576
- Issue #4586 / final PR #4593 (the base project), #4687, #4688, #4976 (earlier hardening), #4975 (sibling follow-up in flight)
- `docs/completed/issue-4976-source-grounded-rejection-votes-plan.md`
