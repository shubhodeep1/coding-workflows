# Run the claude/* merge hold gate before ready labels and at the poller's ready-to-merge merges

Source issue: shubhodeep1/coding-workflows#5564 (https://github.com/shubhodeep1/coding-workflows/issues/5564)
Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims
Security pass: skip (ai:security: automation-produced issue)

## Summary

Security follow-up (A01, high) to project #5316. The merge hold gate (`scripts/claude_merge_hold_gate.py`) runs only when `ENABLE_AUTO_MERGE` is `true`. With auto-merge disabled, a current `claude/*` head that carries a live `hold` claim, or that took a twin pair out of parity, still gets merge-authorization labels (`ai:ready-to-merge` on its linked issues), and the orchestrator poller then merges ready PRs without any hold check. This plan runs the gate before those labels on both review paths, and before the poller's two ready-to-merge merges, bound to the gated head.

## Context

- Finding (issue #5564, `Refs #3576`): `.github/workflows/review_autofix.yml:1953` on the base branch, the `ENABLE_AUTO_MERGE != true` branch of the `deterministic-skip-merge` job sets `auto_merge_ready_labels_allowed=true` after only a head-freshness read; the gate branch (`elif [[ "${PR_HEAD_REF}" == claude/* ]] && ! merge_hold_gate_allows`) is never reached. The same shape exists in `scripts/review_enable_auto_merge.sh` (the `ENABLE_AUTO_MERGE != true` early exit records `AUTO_MERGE_READY_LABELS_ALLOWED=true` before the gate at its line ~211), which feeds the codex-agent "Mark linked issues ready to merge" step.
- The poller (`scripts/orchestrate_poll_process.sh`) merges the linked PR of every `ai:ready-to-merge` issue in two places: the current-wave loop ("Auto-merge: merge PRs that are ready-to-merge", `gh pr merge "${RTM_PR}" … --squash --auto` then `--squash`) and the prior-wave backward scan (`gh pr merge "${PW_PR}" …`). Neither reads claims or binds the merge to a head.
- `orchestrate_poll.yml` checks out the full support source at `.codex-workflow-src` (and `.codex-workflow-src-main` when the ref is not `main`), so the gate and `.claude/scripts/check_in_status.py` it imports are on disk next to each other; only a list of scripts is staged into `scripts/`.
- Project #5316's AD-4 (fail closed), AD-5 (judge merges out of scope), and AD-7 (trusted logins) carry over.

## Goals

- With `ENABLE_AUTO_MERGE != true`, a `claude/*` head the gate refuses gets no merge-authorization labels in `review_enable_auto_merge.sh` (`AUTO_MERGE_READY_LABELS_ALLOWED` stays `false`) or in the `deterministic-skip-merge` job (no `ai:review-skipped`, no `ai:ready-to-merge`), and logs `AUTOFIX_AUTO_MERGE_SKIPPED pr=<n> head_sha=<sha> reason=<skip_reason>`. A gate that allows logs `AUTOFIX_MERGE_HOLD_GATE … action=allow` and the labels are set as before.
- The poller never merges a `claude/*` PR at either ready-to-merge point unless the gate allows its current head, and then merges with `--match-head-commit <that head>`. A refusal logs `ORCH_MERGE_HOLD_GATE pr=<n> head_sha=<sha> action=refuse reason=<skip_reason>` and skips the PR for this tick.
- Non-`claude/*` heads are unaffected on every path: no gate process, no extra API call, byte-identical merge commands.
- A missing gate script refuses (`gate_unavailable`), on every path.

## Non-goals

- The poller's other merges (stall `attempt_merge`, the review-blocked judge merges, the noop-suspicious force-merge, the final integration merge): they act on judge or stall verdicts, not on ready labels (AD-2; #5316 AD-5).
- `scripts/review_rb_judge.sh` ready labels (never runs in Claude-fixer mode; #5316 AD-5).
- Any `.claude/**` edit, and CLAUDE.md.

## Constraints

- §1 fail closed; §5 minimal change; §6 no renames: `AUTO_MERGE_READY_LABELS_ALLOWED`, `AUTOFIX_AUTO_MERGE_SKIPPED`, `AUTOFIX_MERGE_HOLD_GATE`, `CLAUDE_MERGE_HOLD_GATE_SCRIPT` keep their meaning. `ORCH_MERGE_HOLD_GATE` is a new log key (no existing use), registered in `agents.md` "Stable log prefixes".
- §9 tabs in shell/Python, YAML 2-space. §15: the gate runs only for `claude/*` heads; the helper's disabled path replaces its `--jq '.head.sha'` read with the same single `pulls/{n}` read (full object, reused as `--pr-json`); the poller passes the PR object it already fetched. §18: no new script or schedule. §20 changelog fragment. §27: `review_autofix.yml` stays under 480,000 bytes (459,033 today).

### Automation (§18.E)

- Only modifies existing code; no new script, supervisor, or DB work, and no `docs/scripts-pending-removal.md` entry.
- Entry points: `review_autofix.yml` jobs `codex-agent` (step "Enable auto-merge on PR" → `scripts/review_enable_auto_merge.sh`) and `deterministic-skip-merge`; `orchestrate_poll.yml` step running `scripts/orchestrate_poll_process.sh` (schedule / dispatch triggers).

## Approach

1. `scripts/review_enable_auto_merge.sh`: move the inline gate block into a function `claude_merge_hold_gate_allows <head_ref> <pr_json> <what>` (same log lines; `<what>` names what is refused in the warning). `reviewed_head_is_current_for_labels` fetches the full PR object once (`gh api repos/…/pulls/N`) and keeps it; the disabled branch records `true` only when the head is current and, for a `claude/*` head, the gate allows. The enabled path calls the same function where the inline block was.
2. `review_autofix.yml` `deterministic-skip-merge`: in the `ENABLE_AUTO_MERGE != true` branch, require `merge_hold_gate_allows` for a `claude/*` `PR_HEAD_REF` after the freshness read, before `auto_merge_ready_labels_allowed=true`.
3. `scripts/orchestrate_poll_process.sh`: new function `_orch_claude_merge_hold_gate_allows <pr> <head_sha> <pr_json>`: resolves the gate (`CLAUDE_MERGE_HOLD_GATE_SCRIPT`, else `.codex-workflow-src/scripts/claude_merge_hold_gate.py`, else `.codex-workflow-src-main/…`), writes the PR object to a temp file, runs it, logs `ORCH_MERGE_HOLD_GATE`, returns 0 only on exit 0. Both ready-to-merge merges call it for `claude/*` heads immediately before `gh pr merge` and, when it allows, append `--match-head-commit <head>` to both merge attempts.
4. `orchestrate_poll.yml`: pass `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN: ${{ vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN || '' }}` to the poll step (#5316 AD-7).

Alternatives: staging the gate into `scripts/` (breaks its `ROOT/.claude/scripts/check_in_status.py` import in consumers, whose workspace `.claude/` is their own; AD-4); gating every poller merge (AD-2).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — hold gate before ready labels and at the poller's ready-to-merge merges.**
   - Files: `scripts/review_enable_auto_merge.sh`, `.github/workflows/review_autofix.yml`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml`, `tests/test_claude_merge_hold_gate.py`, `tests/test_orchestrate_poll_process.py`, `README.md`, `agents.md`, `changelog.d/5564-hold-gate-ready-labels-poller.md` [new].
   - Done when: the tests below pass locally; a held `claude/*` head gets no ready labels with auto-merge disabled on either review path and is not merged by the poller at either ready-to-merge point; non-`claude/*` behaviour is unchanged in the existing suites; `review_autofix.yml` < 480,000 bytes.
   - Rollback: revert the phase PR; no state to unwind.

## Implementation Steps

1. Refactor the gate block of `review_enable_auto_merge.sh` into `claude_merge_hold_gate_allows`; make the disabled path fetch the full PR, check freshness, and run the gate for `claude/*` heads; update the header comment.
2. Add the gate requirement to the disabled branch of the `deterministic-skip-merge` step.
3. Add `_orch_claude_merge_hold_gate_allows` to `orchestrate_poll_process.sh` with a §15 docstring; call it at the current-wave and prior-wave ready-to-merge merges with `--match-head-commit` for allowed `claude/*` heads.
4. Pass `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` to the poll step in `orchestrate_poll.yml`.
5. Tests, docs (`README.md` merge-gate paragraph, `agents.md` claims section and stable log prefixes), changelog fragment.

## Files & Modules

- `scripts/review_enable_auto_merge.sh`
- `.github/workflows/review_autofix.yml`
- `scripts/orchestrate_poll_process.sh`
- `.github/workflows/orchestrate_poll.yml`
- `tests/test_claude_merge_hold_gate.py`
- `tests/test_orchestrate_poll_process.py`
- `README.md`, `agents.md`
- `changelog.d/5564-hold-gate-ready-labels-poller.md` [new]

## Tests

- `tests/test_claude_merge_hold_gate.py`: helper with `ENABLE_AUTO_MERGE=false` — held `claude/*` head → `AUTO_MERGE_READY_LABELS_ALLOWED` stays `false`, `reason=hold_claim`; unheld `claude/*` head → `true`; non-`claude/*` head → `true` with no comments read; missing gate → `false`. Deterministic-skip step with `ENABLE_AUTO_MERGE=false` — held head → no label calls, unheld → labels. Poller helper extracted from the script — refuse/allow/missing-gate and the log key. Contract: both ready-to-merge merges are preceded by the gate call and carry `--match-head-commit` for `claude/*` heads; `orchestrate_poll.yml` passes the login; `ORCH_MERGE_HOLD_GATE` registered in `agents.md`.
- `tests/test_orchestrate_poll_process.py` (poller harness, stub gate via `CLAUDE_MERGE_HOLD_GATE_SCRIPT`): a ready-to-merge `claude/*` PR whose gate refuses is not merged; one whose gate allows is merged; an `ai/*` PR is merged without invoking the gate.
- Existing suites that must stay green: `tests/test_claude_merge_hold_gate.py`, `tests/test_orchestrate_poll_process.py`, `tests/test_orchestrate_poll_workflow_contract.py`, `tests/test_review_autofix_review_pipeline_contract.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_workflow_file_size_limit.py`, `tests/test_merge_probe.py`, shellcheck/actionlint as CI runs them.

## Risks & Mitigations

- A transient gate read error leaves a `claude/*` PR unlabelled or unmerged until the next run or tick — ACCEPTED (#5316 AD-4); non-`claude/*` PRs never hit it.
- Consumer repos on `@stable` before this ships keep the old poller and gate together; after it ships both come from the same support checkout. A support checkout without the gate refuses `claude/*` ready-to-merge merges — ACCEPTED (fail closed).
- The poller's judge/stall/force merges stay ungated for `claude/*` heads — ACCEPTED (AD-2): they need a judge or stall verdict, not a ready label; recorded for the reviewer.

## Rollout

Ships with the next `@stable` release through the reusable workflows; no flag. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which label paths get the gate when auto-merge is disabled? — Picked: A — both: `review_enable_auto_merge.sh` and the `deterministic-skip-merge` job. Alternatives: B — only the job line the finding cites. Why: the helper has the identical bypass and feeds the codex-agent "Mark linked issues ready to merge" step. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which poller merges get the gate? — Picked: A — the two ready-to-merge merges (current wave and prior-wave backward scan). Alternatives: B — every poller merge (also stall `attempt_merge`, judge merges, noop force-merge); C — only the current-wave merge. Why: the finding is the ready-label → poller-merge path, and both merges consume that label; the others act on judge/stall verdicts (#5316 AD-5), and B touches eight sites with different control flow (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is the poller merge bound to the gated head? — Picked: A — `--match-head-commit <gated head>` on both merge attempts for `claude/*` heads only. Alternatives: B — on every ready-to-merge merge; C — no binding. Why: the recommendation asks for a head-bound gate; B changes non-`claude/*` merges (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where does the poller find the gate? — Picked: A — `CLAUDE_MERGE_HOLD_GATE_SCRIPT`, else `.codex-workflow-src/scripts/`, else `.codex-workflow-src-main/scripts/`; missing refuses. Alternatives: B — stage it into `scripts/` in `orchestrate_poll.yml`. Why: the gate imports `.claude/scripts/check_in_status.py` relative to its own checkout root, which a staged copy in a consumer workspace would not have. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which log key does the poller use? — Picked: A — new `ORCH_MERGE_HOLD_GATE pr= head_sha= action=allow|refuse reason=`, registered in `agents.md`. Alternatives: B — reuse `AUTOFIX_AUTO_MERGE_SKIPPED`. Why: `AUTOFIX_*` keys belong to the review workflow; log searches by pipeline stay clean. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which log keys does the disabled label path use? — Picked: A — the existing `AUTOFIX_AUTO_MERGE_SKIPPED` / `AUTOFIX_MERGE_HOLD_GATE` lines, with a warning that names the ready labels. Alternatives: B — a new key. Why: same gate, same verdict for the same head. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which login besides the PR author counts in the poller? — Picked: A — `vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` passed to the poll step. Alternatives: B — PR author only. Why: matches the review workflow (#5316 AD-7), so a hold is read the same way everywhere. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The issue text asks for nothing outside this repo or its rules.

## References

- Issue #5564, tracker #3576; project #5316 (plan `docs/plans/issue-5316-gate-auto-merge-on-hold-claims-plan.md`, final PR #5323, phase PR #5382).
