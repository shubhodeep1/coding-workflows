# Bind review rejection votes to per-run finding IDs, so quoted REJECTED_FINDING text cannot demote a finding

Source issue: shubhodeep1/coding-workflows#4688 (https://github.com/shubhodeep1/coding-workflows/issues/4688)
Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/review_claude_fixer_nonblocking.py` counts every `REJECTED_FINDING:` line in a pass-2 reviewer's raw output as a rejection vote, so a PR that plants such a line (in a doc, a code comment, a test fixture) can have it counted twice when two reviewers quote it, and a genuine single-reviewer finding is demoted before auto-merge. This plan binds each vote to an unpredictable finding ID issued at run time, so only a line naming an ID from this run's manifest, with a reason, outside a code block, is a vote.

## Context

- Security audit finding #4688 (`STRIDE: Tampering`, severity high, confidence 8/10), filed by `.github/workflows/security-audit.yml` against the #4586 project branch (`Refs #3576`, the audit tracker).
- #4586 (plan `docs/plans/issue-4586-rejected-singleton-findings-hold-reason-plan.md`, on the base branch) added the rejection path. `build_cross_pollination_summary` in `scripts/review_run_reviewers.sh` (≈line 4879) tells pass-2 reviewers to write `REJECTED_FINDING: <file>:<line> | flagged_by: <slug> | reason: <one sentence>`. `reviewer_rejections()` in `scripts/review_claude_fixer_nonblocking.py:183-194` matches `REJECTED_FINDING_RE` (line 66) on **every** line of `review_<slug>.txt`: leading whitespace is allowed (so indented and fenced code counts), the `reason:` field is optional, and nothing ties the line to this run. `demote()` then demotes an entry when at least two, and a strict majority, of the other successful reviewers have a matching line.
- The vote is therefore forgeable by passive quoting: a PR only has to contain a correctly shaped line naming the flagger, and a reviewer discussing or quoting it produces a vote.
- The hand-off step (`scripts/review_autofix_step_claude_fixer_handoff.sh:121-145`) calls the script with `--ledger --reviews-dir --output` and keeps the original ledger on any failure. `scripts/review_claude_fixer_nonblocking.py` is already staged through `REQUIRED_BOOTSTRAP_SCRIPTS` (`scripts/stage_workflow_support.sh:53`), so `review_run_reviewers.sh` can call it from `SUPPORT_SCRIPTS_DIR`.
- Same-head resume: `scripts/review_autofix_step_partial_finalize.sh` persists `PREVIOUS_REVIEWS_DIR` files by glob (`status_pass1_*.txt`, `status_review_*.txt`, `pass1_*.txt`, `review_*.txt`, `consensus_pass1.txt`), and `review_run_reviewers.sh` reuses cached pass-1 ledgers and successful pass-2 slots.

## Goals

- A `REJECTED_FINDING` line counts as a vote only when all hold: it names a finding ID listed in this run's manifest (`${PREVIOUS_REVIEWS_DIR}/rejection_ids_pass1.json`); it carries a non-empty `reason:`; it starts the line (at most 3 leading spaces, an optional `-`/`*` bullet) outside any fenced code block; and it comes from a successful reviewer other than the flagger. Each reviewer counts once per finding.
- Finding IDs are random (`RF-` + 16 hex from `secrets.token_hex`), issued fresh every time the pass-2 header is built, so no PR content, and no ID published by an earlier run, can contain a valid one.
- A vote counts for a consensus entry only when the manifest entry it names has the same file, a line range within 3 lines, and exactly the entry's single flagger. The file and flagger a reviewer echoes in the line are informational and never decide.
- Legacy-format lines (no ID), quoted lines, lines in code fences, lines without a reason, and unknown IDs never count. A missing or unreadable manifest counts no vote, so every finding stays blocking.
- Tests prove the #4688 exploit (two reviewers quoting a PR-supplied line) no longer demotes, and that a genuine ID-bound majority still does.

## Non-goals

- Prompt injection that makes a majority of reviewers write fresh, correctly ID-bound votes. That is the same class of attack as injecting reviewers to omit a finding altogether, which needs no rejection path (AD-7).
- Changing the demotion thresholds (`MIN_REJECTERS = 2`, strict majority, `LINE_TOLERANCE = 3`), the `NON-BLOCKING FINDINGS` block, the hand-off markers, or single-pass review.
- The GPT editor path and `REVIEWER_CONSENSUS_FILE` itself stay unchanged.

## Constraints

- §1: security first, and fail toward blocking: any error in issuing IDs removes the rejection instructions from the header and leaves no manifest, so no vote counts.
- §5: extend the existing script and header function; no new script, no workflow-file edit (§27 not touched).
- §6: no identifier renamed or removed. The script's existing CLI (`--ledger --reviews-dir --output`), its stdout lines (`CLAUDE_FIXER_NONBLOCKING …`), and `REJECTED_FINDING_RE` stay; a new `--issue-ids` mode and a new optional `--ids-manifest` flag are added. New names (`REJECTION_VOTE_RE`, `RF-` IDs, `rejection_ids_pass1.json`) are checked for collisions.
- §9: tabs in the Python file; the shell scripts keep their existing 2-space style.
- §15: no GitHub API calls are added.
- §20: a `changelog.d/` fragment (`security`).

## Approach

1. **Issue IDs after pass 1.** `review_claude_fixer_nonblocking.py --issue-ids --ledger <consensus_pass1.txt> --ids-manifest <PREVIOUS_REVIEWS_DIR>/rejection_ids_pass1.json` parses the pass-1 ledger with the existing `parse_ledger` / `entry_location` / `entry_flaggers`, gives every `CONSENSUS FINDINGS` entry with a parseable location and exactly one flagger an ID `RF-<16 hex>`, writes the manifest (`schema: rejection_ids.v1`, `ledger_sha256`, `entries: [{id, path, start, end, flagger}]`), and prints one `<ID> -> <file>:<range> | flagged_by: <slug>` line per entry. Every call issues fresh IDs (AD-5).
2. **Header.** `build_cross_pollination_summary` runs that mode before writing the summary. On success it prints the rejection instructions with the new line shape `REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence>` and the ID list; it tells reviewers that only these IDs count and that the line must not be quoted from the PR or placed in a code block. On failure (or no eligible entry) it prints no rejection instructions and a `::warning::` to stderr.
3. **Gate.** `demote()` reads the manifest (`--ids-manifest`, default `<reviews-dir>/rejection_ids_pass1.json`) and each reviewer's votes through a new fence-aware `reviewer_votes()` using `REJECTION_VOTE_RE`. Votes are mapped to manifest entries; matching against the final ledger uses the manifest's path, range, and flagger.
4. **Resume.** `rejection_ids_pass1.json` joins the `PREVIOUS_REVIEWS_DIR` patterns persisted by `review_autofix_step_partial_finalize.sh` (both lists), so a resume that skips the whole reviewer phase keeps its votes. A resume that rebuilds the header issues fresh IDs, and cached pass-2 votes stop counting (fail toward blocking).
5. **Summariser.** The `REJECTED_FINDING` rule in `scripts/summarize_reviewer_consensus.sh` names the new shape (it still maps by the echoed file, line and slug for its informational `rejected_by:` line).

Alternatives considered: hardening the parser only (still forgeable by a column-0 quote in prose, AD-1 B); dropping the rejection path (reopens #4586, AD-1 C).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for one standalone issue; the change is one coherent security fix across the issuer, the header, the gate, resume persistence, docs, and tests, and none of it is useful alone.

1. **Phase 1 — ID-bound rejection votes.** Files: see [Files & Modules](#files--modules). Done when: the tests in [Tests](#tests) pass, the exploit test fails on the base branch's gate and passes after, and docs describe the new vote shape. Rollback: revert the phase PR; the base branch's behaviour (file/line-matched votes) returns.

## Implementation Steps

1. `scripts/review_claude_fixer_nonblocking.py`: add `REJECTION_VOTE_RE`, `MANIFEST_NAME`, fence tracking, `issue_ids()` (fresh IDs on every call), `load_manifest()`, `reviewer_votes()`; switch `demote()` to manifest-bound votes (optional `manifest_path` argument, default under `reviews_dir`); add `--issue-ids` and `--ids-manifest` to `main()` keeping the existing flags; update the module docstring. Keep `REJECTED_FINDING_RE` and `reviewer_rejections()` (unused by the gate, retained per §6).
2. `scripts/review_run_reviewers.sh` `build_cross_pollination_summary`: call the issuer, print instructions and the ID list only on success.
3. `scripts/review_autofix_step_partial_finalize.sh`: add `rejection_ids_pass1.json` to both `PREVIOUS_REVIEWS_DIR` pattern lists.
4. `scripts/summarize_reviewer_consensus.sh`: update the `REJECTED_FINDING` rule text to the ID-bearing shape.
5. `scripts/review_autofix_step_claude_fixer_handoff.sh`: comment update only (votes are ID-bound).
6. Docs: `README.md` (≈line 1432), `agents.md` (≈line 93), `docs/INVENTORY.md` (line 213), `changelog.d/4586-rejected-singleton-findings-hold-reason.md` (matching row, AD-6), new `changelog.d/4688-bind-rejection-votes-to-finding-ids.md`.
7. Tests: `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`.

## Files & Modules

- `scripts/review_claude_fixer_nonblocking.py`
- `scripts/review_run_reviewers.sh`
- `scripts/review_autofix_step_partial_finalize.sh`
- `scripts/summarize_reviewer_consensus.sh`
- `scripts/review_autofix_step_claude_fixer_handoff.sh` (comment only)
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/4586-rejected-singleton-findings-hold-reason.md`
- `changelog.d/4688-bind-rejection-votes-to-finding-ids.md` [new]
- `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`

## Tests

- Unit (`tests/test_review_claude_fixer_nonblocking.py`): ID-bound majority demotes; two reviewers quoting the same legacy-format PR line (the #4688 scenario) do not; a valid-looking ID not in the manifest does not; votes inside a fenced block, indented ≥4 spaces, or without a reason do not; a missing or corrupt manifest demotes nothing; an ID for a different entry (other file, other flagger, far lines) does not count; a reviewer repeating a vote counts once; `--issue-ids` writes the manifest, prints one line per single-flagger entry, skips multi-flagger entries, and never reuses an ID (votes for an earlier run's IDs do not count); the existing CLI still works.
- Contract: `build_cross_pollination_summary` invokes `--issue-ids`; partial-finalize persists `rejection_ids_pass1.json`.
- Integration (`tests/test_review_autofix_claude_fixer_mode.py`): the hand-off fixture's votes use manifest IDs; a quoted legacy line keeps the round blocking.
- Run: those two files, plus the `ci.yml` pytest step that includes them, `bash -n` on the edited shell scripts, and `shellcheck` where the repo runs it.

## Risks & Mitigations

- A reviewer omits or mangles the ID → its vote is not counted and the finding stays blocking (fails safe; the pre-#4586 behaviour).
- Same-head resume that rebuilds the header → new IDs, cached pass-2 votes do not count → stays blocking (fails safe). ACCEPTED — resumes are rare, and reusing IDs would let an ID published in an earlier run's uploaded reviewer output count again.
- Prompt-injected majority writes valid votes → ACCEPTED — residual, same class as injecting reviewers to omit findings (AD-7).
- Header gets longer by one line per single-reviewer pass-1 finding → negligible next to the ledger it wraps.

## Rollout

No flag. Scripts are fetched from the verified workflow commit at run time, so consumers get the change on the next `@stable` sync after this project's base merges. Runs that start before the change keep the old header and old gate together; a run that mixes them (cached outputs from an older run) counts no vote, so it fails toward blocking. Rollback: revert the phase PR.

## References

- #4688 (this finding), #4586 (the rejection path), #3576 (security audit tracker), #4575 (the original false-positive incident).

## Auto-decisions

- AD-1 [plan, 2026-09-28] How are rejection votes bound so quoted text cannot count? — Picked: A — per-run random finding IDs, issued after pass 1 into a manifest, and only manifest IDs count. Alternatives: B — keep file/line matching and only harden parsing (column 0, no fences, required reason); C — remove the rejection path. Why: B still counts a column-0 quote in prose; C reopens #4586; A is what the finding recommends ("bound to a unique finding ID"). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] Where does ID issuing live? — Picked: A — a new `--issue-ids` mode of `scripts/review_claude_fixer_nonblocking.py`. Alternatives: B — a new script. Why: the script is already bootstrapped and owns the ledger parser, so issuer and gate cannot drift. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] What happens to legacy-format `REJECTED_FINDING` lines without an ID? — Picked: A — ignored; the entry stays blocking. Alternatives: B — accept them during a transition. Why: B keeps the exploit open. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What is the vote line shape? — Picked: A — `REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence>`, where only the ID and a non-empty reason are required and the gate ignores the echoed location. Alternatives: B — `REJECTED_FINDING: <ID> | reason: …` only. Why: A keeps the summariser's informational `rejected_by:` mapping working while the gate trusts only the manifest. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28; revised phase 1, 2026-09-28] How do IDs survive a same-head resume? — Picked: A — every rebuilt pass-2 header issues fresh IDs; the manifest is persisted with the partial-finalize artifacts only for a resume that skips the whole reviewer phase. Alternatives: B — reuse the manifest when its `ledger_sha256` matches. Why: under B an ID from an earlier run, which can be published in that run's uploaded `review_*.txt` artifacts, would count again in newly generated pass-2 output; A only drops cached votes on a rare resume, which fails toward blocking (§1). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] The unreleased #4586 changelog fragment says a rejection matches by "same file, line ranges within 3 lines, same flagging reviewer". — Picked: A — correct that row in place and add this issue's own `security` fragment. Alternatives: B — add only this issue's fragment. Why: B would ship a stale statement to `CHANGELOG.md` (§12.B stale docs). Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-28] Should the fix also defend against a PR that prompt-injects a majority of reviewers into writing valid votes? — Picked: A — no; record it as an accepted residual risk. Alternatives: B — disable demotion entirely. Why: an injected majority can already suppress a finding by not reporting it, so the rejection path adds no new capability once passive quoting is closed. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-28] Which pass-1 entries get an ID? — Picked: A — only `CONSENSUS FINDINGS` entries with a parseable location and exactly one flagger. Alternatives: B — every entry. Why: only single-reviewer entries can be demoted, which matches the base branch's effective behaviour. Applied in: phase 1. Status: pending review

## Notes

- Security pass: `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 4688` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
