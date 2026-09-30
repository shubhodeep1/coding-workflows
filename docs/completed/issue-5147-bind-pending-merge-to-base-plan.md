# Claude-fixer pending-checks merge: bind the marker to the reviewed base

Source issue: shubhodeep1/coding-workflows#5147 (https://github.com/shubhodeep1/coding-workflows/issues/5147)
Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
Security pass: skip (ai:security: automation-produced issue)

## Summary

The pending-checks auto-merge added for #4900 trusts a marker bound only to the reviewed head. A PR author can retarget the PR (for example from a project branch to `main`) after a clean review without moving the head, and the hourly sweep would then enable auto-merge into a base whose diff nobody reviewed. This plan binds the marker to the reviewed base ref and base commit, makes the sweep refuse to merge when either changed, and makes the review gate stop skipping re-reviews for that head, so the existing 30-minute review sweep runs a fresh review automatically.

## Context

- Security finding #5147 (`A01:2021-Broken Access Control`, severity high, confidence 9/10), filed by `.github/workflows/security-audit.yml` against the #4900 project branch (`Refs #3576`). Location: `scripts/claude_fixer_pending_checks.py:354` (the `verify_review_run` call in `evaluate`).
- `scripts/review_autofix_step_claude_fixer_handoff.sh:170-194` posts `## Review round <n>: clean review, waiting for check runs` with `<!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->`. Nothing in it names the base.
- `scripts/claude_fixer_pending_checks.py:311-368` (`evaluate`) reads the PR once (`repos/<repo>/pulls/<n>`), finds the live marker for the current head (`find_pending_marker`, lines 113-167), snapshots the checks, verifies the linked review run, and runs `scripts/review_enable_auto_merge.sh` (`--match-head-commit` only). The PR's `base` is never compared.
- `.github/workflows/review_autofix.yml:740-746` (`gate_claude_pending_checks_on_head`) skips every dispatched re-run on a head with a pending-checks marker (`SKIP_REASON=claude_fixer_pending_checks`, lines 839-842). The 30-minute `sweep` job of `.github/workflows/review_autofix_sweep.yml` dispatches `internal-review.yml` for every open non-draft PR; `internal-review.yml` does not trigger on `pull_request: edited`, so a retarget alone starts no review. Today that skip also blocks the only automated path to a fresh review of the new base.
- The review run's PR snapshot is `PR_PAYLOAD_FILE` (`scripts/review_collect_pr_metadata.sh:209`, the REST PR object), exported to `GITHUB_ENV` in the `codex-agent` job (`review_autofix.yml:2761`), the same job as the hand-off step. `BASE_BRANCH` is derived from it (`review_collect_pr_metadata.sh:488-493`), and the reviewers' diff is taken against that base.
- The REST PR object's `base.sha` is a snapshot, not the live base tip: on 2026-09-29 ten open PRs in this repo (for example #4964 and #5051 into `main`) carried a `base.sha` older than their base branch's tip. It changes when the PR is retargeted or synchronized, so comparing it catches a retarget without invalidating a pending merge on every unrelated push to the base branch.
- The pending-checks feature exists only on the base branch (project #4900, final PR #4922 still a draft), so no `ai:claude-fixer-pending-checks:v1` comment exists in production or in any consumer repo.

## Goals

- G1. The hand-off step's pending-checks comment carries a new `<!-- ai:claude-fixer-pending-checks:v2 head=<sha> round=<n> ledger=<sha256> base_sha=<sha> base_ref_sha256=<sha256> -->` line, beside the unchanged v1 line, plus a human-readable `Reviewed base:` line. `base_sha` and the ref come from the review run's `PR_PAYLOAD_FILE`.
- G2. When `PR_PAYLOAD_FILE` gives no 40-hex `base.sha` or no non-empty `base.ref`, the step posts no pending-checks comment and falls through to the existing fail-closed path (warning plus findings hand-off).
- G3. `scripts/claude_fixer_pending_checks.py` never enables auto-merge unless the live marker has a v2 line matching its v1 head, round and ledger, and that line's `base_sha` equals the PR's current `base.sha` and its `base_ref_sha256` equals sha256 of the PR's current `base.ref`. Otherwise `evaluate` returns the new state `base_changed` (binding mismatch) or `base_unbound` (no v2 line), before any check-run, run, or variable read.
- G4. The review gate skips a dispatched re-run on a pending-checks head only when a trusted comment for that head carries a v2 line bound to the PR's current `base.sha` and `base.ref`. A retargeted PR therefore gets a fresh reviewer run on the next 30-minute review sweep, and that run's clean result posts a new marker bound to the new base (or hands findings to the Claude session).
- G5. The gate's existing PR read supplies the base fields (no new API call); the sweep's existing PR read does the same (§15).
- G6. Tests cover every rule above, including the issue's exploit (retarget after a clean review, same head: no merge, no gate skip, fresh review on dispatch). Docs and a `changelog.d/` fragment updated (§7, §20).

## Non-goals

- Binding the in-run zero-findings auto-merge (`CLAUDE_FIXER_ZERO_FINDINGS=true`, the workflow's own auto-merge step seconds after the review) to the base (AD-7).
- Changing the findings, conflict, or verdict-convergence hand-offs, `.claude/scripts/check_in_status.py`, or any `.claude/**` file.
- Re-validating base-branch content that moved without a retarget (unrelated merges into the base branch); GitHub merges into the current base tip on every auto-merge path today.
- A new dispatch path for the fresh review (AD-4).

## Constraints

- §1 / §3: security first; every missing or mismatched binding fails closed (no merge; no gate skip).
- §5: extend the hand-off script, the gate predicate and its existing PR read, and `evaluate`; no new module, workflow, or job.
- §6: no renames. The v1 marker line and every existing identifier stay unchanged. New identifiers were checked for collisions in their scope: the marker `ai:claude-fixer-pending-checks:v2`, `PENDING_CHECKS_V2_MARKER_RE`, `base_ref_digest` (function), the `evaluate` states `base_changed` / `base_unbound`, the gate variables `pr_base_ref_gate` / `pr_base_sha_gate`, and the hand-off variables `claude_fixer_base_ref` / `claude_fixer_base_sha` / `claude_fixer_base_ref_digest`.
- §9: tabs in the Python bodies and the shell script's existing 2-space style kept as the file uses it; YAML 2-space.
- §15: zero new API calls; the gate's `repos/<repo>/pulls/<n>` jq projection and the sweep's PR read already return `base`.
- §18: no new script or schedule; the fresh review comes from the existing `review_autofix_sweep.yml` `sweep` job.
- §19: every PR of this project uses `Refs #5147`; the final PR targets a non-default base, so the final-merge stage closes #5147 explicitly.
- §20: `changelog.d/5147-pending-merge-base-binding.md` (`security` section).
- §27: `review_autofix.yml` is 456,143 bytes; the change adds well under 1 KB (stays below 480,000).

## Approach

1. **Hand-off step** (`scripts/review_autofix_step_claude_fixer_handoff.sh`). Before the pending-checks branch, read `.base.ref` and `.base.sha` from `PR_PAYLOAD_FILE` with `jq`. The pending branch additionally requires a 40-hex base sha and a non-empty ref (AD-5). The comment gains `Reviewed base: \`<ref>\` at \`<sha>\`.` and the v2 marker line after the unchanged v1 line; `base_ref_sha256` is `printf '%s' "<ref>" | sha256sum` (AD-3). The `CLAUDE_FIXER_HANDOFF ... kind=pending-checks` log line gains `base_sha=<sha>`.
2. **Sweep** (`scripts/claude_fixer_pending_checks.py`). `find_pending_marker` also parses the v2 line (exactly one, with head, round, and ledger equal to the v1 line's; otherwise the comment carries no binding) and returns `base_sha` / `base_ref_sha256` (None when unbound). `evaluate` reads `base.ref` / `base.sha` from the PR it already fetched and, right after finding the marker, returns `base_unbound` or `base_changed` unless both match. A new helper `base_ref_digest(ref)` computes the sha256. The latest trusted marker still wins, so after a fresh review the new, rebound marker is the one evaluated.
3. **Gate** (`.github/workflows/review_autofix.yml`). The existing PR fetch projects `base_ref` and `base_sha`; the gate stores them as `pr_base_ref_gate` / `pr_base_sha_gate`. `gate_claude_pending_checks_on_head` computes the ref digest in bash, returns false unless the base sha is 40-hex, and matches a v2 line for the current head with exactly that `base_sha` and `base_ref_sha256` (plus the existing header and author rule). The v1 test is dropped from the predicate (a bound v2 line implies the v2 comment format). The Claude-fixer comment block documents the base binding.
4. **Fresh review.** No new code: with the gate no longer skipping, the next 30-minute review sweep dispatch runs the reviewer panel against the new base (AD-4). While it runs, `evaluate` keeps returning `base_changed`, and `check_in_status.py` keeps reporting `wait` as today.

Alternatives considered: binding the live base-branch tip (AD-1 B) invalidates a pending merge on every push to a busy base and costs a read per evaluation; changing the v1 line in place (AD-2 B) repurposes an identifier; dispatching the review from the catch-all sweep (AD-4 B) adds a write path that the 30-minute sweep already covers.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — bind the pending-checks marker to the reviewed base.** Files: see [Files & Modules](#files--modules). Done when the hand-off step posts the v2 line bound to `PR_PAYLOAD_FILE`'s base (and no pending comment without one), `evaluate` returns `base_changed` / `base_unbound` and never merges on a missing or mismatched binding while still merging a matching one, the gate skips a dispatch only for a marker bound to the current base, the #5147 retarget scenario is reproduced in tests, and the repo's CI suites for these files pass. Rollback: revert the PR; the pending-checks feature returns to head-only binding (the #4900 behaviour), and any v2 lines already posted are ignored by the reverted readers.

## Implementation Steps

Phase 1:
1. `scripts/review_autofix_step_claude_fixer_handoff.sh`: header comment (lines 24-31, 39-45) documents the v2 line and `PR_PAYLOAD_FILE`; read the base ref and sha before line 170; add the base conditions to the pending branch; emit the `Reviewed base:` line and the v2 marker; extend the log line.
2. `scripts/claude_fixer_pending_checks.py`: module docstring (fail-closed list and API budget); `PENDING_CHECKS_V2_MARKER_RE`; `base_ref_digest`; `find_pending_marker` parses and returns the binding; `evaluate` compares it after the marker lookup and documents the two new states.
3. `.github/workflows/review_autofix.yml`: add `base_ref` / `base_sha` to the gate's PR jq projection and parse them; rewrite `gate_claude_pending_checks_on_head` to require the bound v2 line; update the Claude-fixer comment block (lines 759-764).
4. `scripts/claude_pr_sweep.py`: docstring only (the pass now needs a base-bound marker).
5. Tests (below), `README.md` / `agents.md` pending-checks paragraphs, `changelog.d/5147-pending-merge-base-binding.md`.

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `scripts/claude_fixer_pending_checks.py`
- `scripts/claude_pr_sweep.py` (docstring)
- `.github/workflows/review_autofix.yml`
- `tests/test_claude_fixer_pending_checks.py`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`, `agents.md`
- `changelog.d/5147-pending-merge-base-binding.md` [new]

## Tests

Unit / integration (pytest, run by the existing `ci.yml` steps that already list both files):
- `tests/test_claude_fixer_pending_checks.py`: the marker fixture gains the v2 line; `find_pending_marker` returns the binding, and returns it unbound for a missing, duplicated, or mismatched (head, round, ledger) v2 line; `evaluate` returns `base_changed` for a retargeted PR (same head, different `base.ref`), for a different `base.sha` on the same ref, and `base_unbound` for a v1-only marker, in each case before any check-run read and without calling the merge helper; a matching binding still reaches `merge_enabled`; a later marker bound to the new base (fresh review) merges; the PR #4869 end-to-end replay passes the base through the hand-off step.
- `tests/test_review_autofix_claude_fixer_mode.py`: the hand-off step writes the `Reviewed base:` line and the v2 line with the payload's base and the ref's sha256; with no payload base it posts no pending-checks comment; the gate skips a dispatch for a marker bound to the current base, and does not skip for a retargeted base, a changed base sha, or a v1-only marker (the #5147 exploit), and the gate's PR read carries the base fields.
- Regression: the full existing suites of both files, plus `tests/test_claude_pr_sweep.py`, `tests/test_check_in_status_hand_back.py`, and `tests/test_workflow_file_size_limit.py`.

## Risks & Mitigations

- A `base.sha` change without a retarget (a GitHub-side PR re-sync) forces one extra reviewer run. ACCEPTED — fail closed; the fresh review re-binds the marker.
- Clean reviews posted by a pre-change workflow (v1-only) would never merge through the sweep. ACCEPTED — the feature is unreleased (only on the #4900 project branch); a fresh review re-posts a bound marker.
- Retarget back to the reviewed base with an unchanged `base.sha` would match again. ACCEPTED — that restores exactly the reviewed state.
- A base ref containing characters that break a marker line. Mitigated by hashing the ref (AD-3).

## Rollout

Ships with the #4900 project (this project's final PR merges into its branch, and #4922 carries both into `main`), then to consumers on the next `@stable` sync. No flag: the change only narrows when an existing, unreleased merge path fires. Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] What does the marker bind as the "reviewed base commit"? — Picked: A — the PR's `base.ref` and `base.sha` as the review run read them (`PR_PAYLOAD_FILE`), compared with the same fields of the sweep's and gate's existing PR reads. Alternatives: B — the base branch's live tip (extra read; re-review on every base push); C — the merge base from the compare API (extra read). Why: catches the retarget exploit and any PR re-sync with zero new API calls (§15), without invalidating pending merges on every unrelated base push. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does the marker carry the binding? — Picked: A — a new `ai:claude-fixer-pending-checks:v2` line beside the unchanged v1 line; readers require the bound v2 line. Alternatives: B — extend the v1 line in place. Why: §6 forbids repurposing an existing marker. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the base ref encoded in the marker? — Picked: A — `base_ref_sha256=<sha256 of the ref name>`, with the readable ref on a separate `Reviewed base:` line that no reader parses. Alternatives: B — the raw ref restricted to `[A-Za-z0-9._/-]`. Why: git ref names may contain `>` and `-`, so a raw ref could end the HTML comment early; a digest is unambiguous for every ref. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What triggers the "automated fresh review" the issue requires? — Picked: A — the existing 30-minute `review_autofix_sweep.yml` dispatch, which the gate now skips only for a marker bound to the current base. Alternatives: B — the catch-all sweep dispatches `internal-review.yml` itself. Why: no new write path or dispatch (§5, §15, §23.C); the next sweep tick (at most 30 minutes) reviews the retargeted PR. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] What does the hand-off step do when `PR_PAYLOAD_FILE` has no valid base? — Picked: A — post no pending-checks comment; fall through to the existing fail-closed warning and findings hand-off. Alternatives: B — post the v1 line only. Why: an unbound marker could never merge and would leave the head pending with no owner; the existing path hands it to a Claude session. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How are v1-only pending-checks comments treated? — Picked: A — unbound: the sweep returns `base_unbound` (no merge) and the gate does not skip (fresh review). Alternatives: B — accept v1 when the PR's base is unchanged since the comment (not knowable without a timeline read). Why: fail closed as the issue asks; no v1 comment exists outside the unreleased #4900 branch. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] Should the in-run zero-findings auto-merge also be base-bound? — Picked: A — no, out of scope. Alternatives: B — add a base re-read to the workflow's auto-merge step. Why: §5; the finding is about the sweep's delayed authorization, and the in-run merge follows the review within the same run. Applied in: no code change. Status: pending review

## References

- #5147 (this finding), #4900 (pending-checks auto-merge), #4922 (its final PR), #4942 (its phase PR), #4618 (sweep dispatches from the default branch), #3576 (security audit tracker).
- `docs/plans/issue-4900-claude-fixer-pending-checks-auto-merge-plan.md` on the base branch.
