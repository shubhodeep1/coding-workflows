# Gate review auto-merge on hold claims and twin parity

Source issue: shubhodeep1/coding-workflows#5316 (https://github.com/shubhodeep1/coding-workflows/issues/5316)
Base branch: main
Security pass: run

## Summary

A twin-first phase PR can merge before its `.claude/` copy lands: the stage posts a `hold` claim after its push, but the review run for that head already started, and when it finishes clean `review_autofix.yml` enables head-bound auto-merge without looking at claims. This plan adds one merge gate, run immediately before every in-workflow merge enablement on a `claude/*` head, that refuses the merge when the head carries a live `hold` claim (same trust rules as `check_in_status.py`) or when the PR breaks `workflow-templates/.claude/**` ↔ `.claude/**` twin parity.

## Context

- Incident, issue #5259 / PR #5301: review run 36646306677 started at 23:38:50Z on head `0069bf4`; the hold claim was posted at 23:39:07Z; the run logged `CLAUDE_FIXER_HANDOFF … kind=none … action=auto_merge`, then `AUTOFIX_AUTO_MERGE_HEAD_BOUND pr=5301 … action=squash` at 23:52:27Z, and the PR merged at 23:52:30Z. Six `.claude/` copies never reached the project branch. Issue #5127 / PR #5182 stranded its `[claude-twin-sync]` commit `f4b1e75` the same way (recovered in #5274). Both break §21.
- The merge in both cases ran through `scripts/review_enable_auto_merge.sh` (workflow step "Enable auto-merge on PR", `.github/workflows/review_autofix.yml` ~line 5632), reached in Claude-fixer mode when `scripts/review_autofix_step_claude_fixer_handoff.sh` exports `CLAUDE_FIXER_ZERO_FINDINGS=true` after a clean ledger and a fresh ready check snapshot.
- The only other merge enablement that a `claude/*` PR can reach inside `review_autofix.yml` is the `deterministic-skip-merge` job (~line 1783, doc-only / small-diff skip; CLAUDE.md §26.H says `claude/*` PRs take it). The `claude-fixer-auto-merge` job is a disabled legacy stub. `scripts/review_merge_train.sh` never merges: it holds and later re-dispatches the review run, which then merges through the gated helper. The pending-checks merge path of #4900 is not on `main` yet. The review-blocked judge (`scripts/review_rb_judge.sh`) never runs in Claude-fixer mode (§26.H).
- Claims are read by `.claude/scripts/check_in_status.py`: `read_fix_claims` (only owner/member/collaborator comments by a trusted login count; the latest trusted claim on the current head decides; `hold` never expires on the same head; a newer claim lifts it) and `_fix_claim_trusted_logins` (PR author plus `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`). `scripts/claude_pr_sweep.py` already imports that module from Actions by file path.
- On `main` five command twins differ on purpose (`validate-consumer-issue.md`, `verify-activation.md`, `analyze-log.md`, `investigate-issue.md`, `deploy-activate.md`), and `.claude/commands/claude-issue-pickup.md` has no twin, so "twin differs on the head" alone cannot be the parity condition.

## Goals

- A clean review run that reaches merge enablement on a `claude/*` head whose latest trusted claim on that head is `hold` does not call `gh pr merge`, and logs `AUTOFIX_AUTO_MERGE_SKIPPED pr=<n> head_sha=<sha> reason=hold_claim`. This holds when the hold was posted after the run started.
- A hold on an older head, a hold lifted by a newer claim on the same head, or a hold from an untrusted author does not block the merge.
- A `claude/*` PR that changes a `workflow-templates/.claude/**` twin so that a pair in parity at the merge base is out of parity at the head does not merge (`reason=twin_parity`), whether or not a hold was posted.
- The gate covers both merge enablements a `claude/*` PR can reach in `review_autofix.yml`: `scripts/review_enable_auto_merge.sh` and the `deterministic-skip-merge` job. A contract test fails if either loses the gate.
- Non-`claude/*` PRs are unaffected and issue no extra API calls.

## Non-goals

- `scripts/review_rb_judge.sh` merges (AD-5) and orchestrator merges in `scripts/orchestrate_poll_process.sh` (never `claude/*` heads).
- Implementing #4900's pending-checks merge path. The gate is a standalone helper that path calls when it lands; this plan only notes that in `agents.md`.
- Disabling an auto-merge that was enabled before a hold was posted (see Risks).
- Any `.claude/**` edit (AD-6), and the #4785 Actions twin sync.

## Constraints

- §1: fail closed. A gate that cannot decide (read error, missing module, truncated tree, >300-file compare) refuses the merge (AD-4).
- §5 minimal change set; §6 no renames: `AUTOFIX_AUTO_MERGE_HEAD_BOUND`, `AUTO_MERGE_READY_LABELS_ALLOWED`, and existing log lines keep their names and meanings. `AUTOFIX_AUTO_MERGE_SKIPPED` is a new log key (unique; no existing use).
- §9 tabs in new Python/shell; YAML stays 2-space.
- §15: the gate runs only for `claude/*` heads; it reuses the PR object the helper already fetched, reads the PR's comments (1 call per 100), one compare call, and the two recursive tree reads only when the compare lists a changed `workflow-templates/.claude/` path or is truncated. The fresh comment read is required: the race is a hold posted after the run's earlier reads.
- §18: no standalone script; the helper runs inside the existing `review_autofix.yml` steps. No `docs/scripts-pending-removal.md` entry (not single-use, not long-running, not a supervisor).
- §20 changelog fragment; §27 `review_autofix.yml` stays under 480,000 bytes (454,700 today).

### Automation (§18.E)

- New file `scripts/claude_merge_hold_gate.py`, a helper called by existing steps; no new schedule, no supervisor, no DB work.
- Entry points: `.github/workflows/review_autofix.yml` job `codex-agent` step "Enable auto-merge on PR" (via `scripts/review_enable_auto_merge.sh`), and job `deterministic-skip-merge` (pull_request / workflow_dispatch / workflow_call triggers of the review workflow).

## Approach

1. **`scripts/claude_merge_hold_gate.py` [new].** `--repo O/R --pr N --head SHA [--pr-json FILE]`. Loads `check_in_status` from `<repo root>/.claude/scripts/check_in_status.py` (the `claude_pr_sweep.py` pattern). Returns one JSON line `{"merge": bool, "reason": "...", "skip_reason": "hold_claim|twin_parity|gate_unavailable"|null, ...}`; exit 0 = merge allowed, 1 = refused, 2 = could not decide (the caller treats 2 as refused).
   - Non-`claude/*` head (`CLAUDE_BRANCH_PREFIX`) → allow with no API call.
   - Head mismatch between `--head` and the PR object → refuse (`gate_unavailable`).
   - Claims: `gh_api_list issues/N/comments`, `read_fix_claims(comments, head, now, trusted_logins=_fix_claim_trusted_logins(pr))`; `claim.state == "held"` → refuse `hold_claim`.
   - Twin parity: `compare/<base.sha>...<head>` → `merge_base_commit.sha` and `files`. If no changed file is under `workflow-templates/.claude/` and the list has fewer than 300 entries → done. Otherwise read `git/trees/<head>?recursive=1` and `git/trees/<merge base>?recursive=1` (truncated → `gate_unavailable`) and refuse `twin_parity` for any twin path `workflow-templates/.claude/<p>` whose blob changed between merge base and head, where the pair (`workflow-templates/.claude/<p>`, `.claude/<p>`) had equal blob ids (or both absent) at the merge base and unequal ones at the head.
2. **`scripts/review_enable_auto_merge.sh`.** After the existing reviewed-head freshness check, when `.head.ref` starts with `claude/`, write the already-fetched `_ORCH_PR_META_JSON` to a temp file and run the gate (`${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/claude_merge_hold_gate.py`, overridable by `CLAUDE_MERGE_HOLD_GATE_SCRIPT`). On refusal log `AUTOFIX_AUTO_MERGE_SKIPPED pr=… head_sha=… reason=<skip_reason>`, a `::warning::` with the gate's reason, keep `AUTO_MERGE_READY_LABELS_ALLOWED=false`, and exit 0. A missing gate script on a `claude/*` head refuses with `reason=gate_unavailable`.
3. **`deterministic-skip-merge` job.** Add a sparse checkout of the verified support source (`needs.gate.outputs.review_support_sha`, the `fingerprint-cap-block` pattern) with `scripts/claude_merge_hold_gate.py` and `.claude/scripts/check_in_status.py`, plus its verification step; before either `gh pr merge` in that job, run the gate for a `claude/*` `PR_HEAD_REF` and skip the merge enablement (and ready labels) on refusal with the same log key.
4. **Workflow env.** Pass `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN: ${{ vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN || '' }}` to both steps (AD-7).

Alternatives considered: putting the gate in `check_in_status.py` (protected path, twin-first plus a sync blocker; AD-6); gating only through the Claude-fixer hand-off script (misses the deterministic skip); disabling auto-merge from the stage session when it posts a hold (a `.claude/` command change, and it does not cover a stage that dies before posting).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — merge gate on hold claims and twin parity.**
   - Files: `scripts/claude_merge_hold_gate.py` [new], `scripts/review_enable_auto_merge.sh`, `.github/workflows/review_autofix.yml`, `tests/test_claude_merge_hold_gate.py` [new], `tests/test_review_autofix_review_pipeline_contract.py`, `.github/workflows/ci.yml`, `README.md`, `agents.md`, `changelog.d/5316-merge-gate-hold-claims.md` [new].
   - Done when: the tests below pass locally; a `claude/*` head with a trusted hold or a parity break never reaches `gh pr merge` in either path; non-`claude/*` heads behave byte-for-byte as before in the existing helper tests; `review_autofix.yml` < 480,000 bytes.
   - Rollback: revert the phase PR; no data or state to unwind.

## Implementation Steps

1. Add `scripts/claude_merge_hold_gate.py` with the CLI, JSON output, exit codes, and a docstring stating inputs, outputs, API calls, and fail-closed behaviour (§15).
2. Wire the gate into `scripts/review_enable_auto_merge.sh` after the head-freshness check; update its header comment (inputs, new log key).
3. In `review_autofix.yml`: add `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` to the "Enable auto-merge on PR" step env; add the sparse support checkout + verification to `deterministic-skip-merge` and call the gate before its merge enablement.
4. Tests (below); register the new test file in `ci.yml` next to the Claude-fixer tests and satisfy `tests/inventory_parity.py`.
5. Docs: README (Claude-fixer mode / claims section near line 1462; the `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` row "Used by" column) and `agents.md` (claims section near line 999; note that #4900's merge path must call the gate); changelog fragment.

## Files & Modules

- `scripts/claude_merge_hold_gate.py` [new]
- `scripts/review_enable_auto_merge.sh`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/ci.yml`
- `tests/test_claude_merge_hold_gate.py` [new]
- `tests/test_review_autofix_review_pipeline_contract.py`
- `README.md`, `agents.md`
- `changelog.d/5316-merge-gate-hold-claims.md` [new]

## Tests

- Unit (`tests/test_claude_merge_hold_gate.py`, fake `gh` on `PATH`): a hold posted after the run started blocks (`hold_claim`); a hold on an older head allows; a newer non-hold claim on the same head lifts the hold; a hold by an untrusted login or association is ignored; a non-`claude/*` head allows with zero `gh` calls; a twin changed so a formerly identical pair differs blocks (`twin_parity`); a new twin with no `.claude/` counterpart blocks; a change to an already divergent pair allows; a matching `.claude/` copy on the head allows; a truncated tree, a 300-file compare needing trees, an API error, and a head mismatch refuse with exit 2 / `gate_unavailable`.
- Shell (`tests/test_review_autofix_review_pipeline_contract.py`, existing fake-`gh` harness for `review_enable_auto_merge.sh`): a `claude/*` head with a hold makes no `gh pr merge` call and logs `AUTOFIX_AUTO_MERGE_SKIPPED … reason=hold_claim`; a `claude/*` head with no claims merges as before; a non-`claude/*` head never invokes the gate; a missing gate script on a `claude/*` head refuses.
- Contract: the `deterministic-skip-merge` job checks out the gate and `check_in_status.py`, and runs the gate before each `gh pr merge`; the "Enable auto-merge on PR" step passes `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`.
- Existing suites that must stay green: `tests/test_review_autofix_review_pipeline_contract.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_check_in_status*.py`, `tests/test_claude_pr_sweep.py`, `tests/test_workflow_file_size_limit.py`, `tests/inventory_parity.py`, `actionlint`/ruff/shellcheck as CI runs them.

## Risks & Mitigations

- A fail-closed refusal on a transient read error leaves a clean `claude/*` PR unmerged until the next push or dispatch — ACCEPTED (AD-4); the same trade the e2e-label and PR-metadata guards in the helper make, and the warning names the cause.
- Residual window: a hold posted after `gh pr merge --auto` was enabled on a base with required checks — ACCEPTED; project branches have no required checks (merge is immediate, so the gate read is the last moment), and for twin-first phases the parity check blocks regardless of when the hold lands.
- A deliberate template-only `.claude/` file added in a `claude/*` PR is refused by the parity check — ACCEPTED; adding the matching `.claude/` copy (or merging outside the review workflow) resolves it, and no such file exists today.
- Consumers: the gate lives in the support source (always coding-workflows at the verified ref), so consumer repos need no change; their PRs have no twin tree, so only the claim check applies.

## Rollout

Ships with the next `@stable` release through the reusable workflow; no flag. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which claims block the merge? — Picked: A — only a `hold` that is the latest trusted claim on the current head (the `check_in_status.py` `held` state). Alternatives: B — any live claim; C — any hold on the head even after a newer claim. Why: B would block the convergence path, whose fixer's `review` claim is still live when the clean re-review merges; C would keep a head parked after the fixer resumed. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which PRs does the gate read claims for? — Picked: A — heads starting with `claude/` only. Alternatives: B — every PR. Why: claims are only posted on `claude/*` PRs; B adds a paginated comment read to every merge enablement (§15). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What twin-parity condition fails the merge? — Picked: A — a twin the PR changed whose pair was in parity at the merge base and is out of parity at the head. Alternatives: B — any changed twin that differs from `.claude/` on the head; C — no parity check. Why: B would block every edit to the five intentionally divergent command twins forever; C leaves a stage that dies before posting its hold uncovered. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] What happens when the gate cannot decide? — Picked: A — refuse the merge (`gate_unavailable`) with a warning. Alternatives: B — allow the merge. Why: §1 correctness first, matching the helper's existing fail-closed guards; a missed merge is recoverable, a stranded `.claude/` sync is not. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which merge paths get the gate? — Picked: A — `review_enable_auto_merge.sh` and the `deterministic-skip-merge` job (merge train covered through the re-dispatched review; #4900 to call the helper when it lands). Alternatives: B — also the three `review_rb_judge.sh` merges. Why: the review-blocked judge never runs in Claude-fixer mode, and its merge ladder carries judge-state outputs a gate would have to thread through (§5). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Where does the gate live? — Picked: A — new `scripts/claude_merge_hold_gate.py` importing `check_in_status.py` by path. Alternatives: B — a new mode in `.claude/scripts/check_in_status.py`. Why: A reuses the exact trust rules without a protected-path edit (no twin-first sync blocker); `scripts/claude_pr_sweep.py` is the precedent. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which logins count as trusted in the workflow? — Picked: A — the PR author plus `vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` when set. Alternatives: B — also fall back to the `GH_PAT` login via `gh api user`. Why: holds are posted by the PR author's sessions; the sweep's `GH_PAT` claims are reservations, never holds, so B spends a call for nothing. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Update CLAUDE.md §26.H to mention the merge gate? — Picked: A — no; document in README and `agents.md` only. Alternatives: B — edit CLAUDE.md and its `workflow-templates/CLAUDE.md` twin. Why: §26.H makes no claim the gate contradicts, and a CLAUDE.md edit ships to every consumer's session context for a workflow-internal guard (§5). Applied in: no code change. Status: pending review

## Notes

- The issue text has nothing that widens access or scope; its "Fix direction" is followed, with the judgement calls above.

## References

- Issue #5316; incidents #5259 / PR #5301 (run 36646306677) and #5127 / PR #5182 / #5274.
- #4785 (Actions twin sync), #4900 (pending-checks merge path), #4622 (claim trust rules), CLAUDE.md §21, §26.H, §28.C.
