# Merge claude/* PRs synchronously after the hold gate and cancel stale auto-merge

Source issue: shubhodeep1/coding-workflows#5565 (https://github.com/shubhodeep1/coding-workflows/issues/5565)
Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims
Security pass: skip (ai:security: automation-produced issue)

## Summary

The #5316 merge hold gate (`scripts/claude_merge_hold_gate.py`) reads claims and twin parity once, then `review_autofix.yml` enrolls the PR in GitHub auto-merge (`gh pr merge --auto`). When the base requires checks that are still pending, the enrollment outlives the gate: a `hold` posted afterwards, or a new head pushed afterwards, does not stop the armed merge. This plan removes the enrollment for `claude/*` heads. Both merge paths merge them **synchronously** (`gh pr merge --squash --match-head-commit <sha>`, no `--auto`) right after the gate allows, and refuse with a log line when GitHub cannot merge yet. Every review run also cancels any auto-merge enrollment it finds on an open `claude/*` PR, which covers a head change and enrollments left from before this change or made by hand.

## Context

- Finding `hold-after-auto-merge-enrollment` (STRIDE-Tampering, high, 8/10) at `scripts/review_enable_auto_merge.sh:221` on the #5316 project branch: "The gate reads claims once before enabling --auto. If required checks are pending, a hold posted afterward on the same head does not cancel the armed merge or trigger another gate check." Recommendation: revalidate head, claims, and twin parity before a synchronous head-bound merge, and cancel any existing enrollment on a hold or head change.
- The two merge enablements a `claude/*` PR reaches: `scripts/review_enable_auto_merge.sh` (codex-agent step "Enable auto-merge on PR"; tail `gh pr merge --squash --auto --match-head-commit`) and the `deterministic-skip-merge` job (tail `gh pr merge --squash --auto --match-head-commit`). Both already run the #5316 gate first for `claude/*` heads.
- In Claude-fixer mode the helper is reached only after `scripts/review_autofix_step_claude_fixer_handoff.sh` saw a fresh `collection_status: ready` check-run snapshot with no failed or incomplete run (the review run itself excluded), so the checks have settled when the synchronous merge runs. The deterministic-skip path does not wait for checks.
- This repo's `main` has no required status checks or required reviews (`rules/branches/main`: deletion, non_fast_forward, copilot_code_review only), and project branches have none, so `--auto` on a `claude/*` PR here already merged at once (PR #5301: `AUTOFIX_AUTO_MERGE_HEAD_BOUND` at 23:52:27Z, merged 23:52:30Z). The window the finding describes exists in repos whose base requires checks or reviews, including consumer repos that run this reusable workflow.
- GitHub keeps an auto-merge enrollment when someone with write access pushes a new head, and there is no REST endpoint to cancel it; `gh pr merge --disable-auto` (GraphQL `disablePullRequestAutoMerge`) does, and `GH_PAT` in Actions can call it. The REST `pulls/{n}` object carries `auto_merge` (null when not enrolled), and the gate job already reads that object.
- #4900 (open, project PR #4922) will add a pending-checks merge path. The gate's docstring and `agents.md` already require it to call the gate; with this plan it must also merge `claude/*` heads synchronously.

## Goals

- `review_enable_auto_merge.sh` and the `deterministic-skip-merge` job never call `gh pr merge --auto` for a `claude/*` head. After the gate allows, they call `gh pr merge <n> --repo <repo> --squash --match-head-commit <sha>` and log `action=squash_sync` on their existing head-bound audit line.
- A synchronous merge GitHub refuses (checks or reviews still required, merge conflict, head moved) enrolls nothing, logs `AUTOFIX_AUTO_MERGE_SKIPPED pr=<n> head_sha=<sha> reason=merge_not_ready` with a warning, and grants no merge-authorization labels.
- Every review run whose gate job sees an open `claude/*` PR with an auto-merge enrollment cancels it (`gh pr merge --disable-auto`) and logs `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED pr=<n> head_sha=<sha> result=disabled|failed`.
- Non-`claude/*` PRs behave byte-for-byte as before and make no extra API call.

## Non-goals

- The pending-checks merge path itself (#4900 / PR #4922).
- A separate hold-triggered cancellation (an `issue_comment` workflow or a sweep job); see AD-4.
- `scripts/review_rb_judge.sh` merges (never run in Claude-fixer mode; #5316 AD-5) and orchestrator merges (never `claude/*` heads).
- Any `.claude/**` or `CLAUDE.md` edit (AD-7).

## Constraints

- §1: security first. A refused synchronous merge leaves the PR open rather than arming a merge the gate cannot re-check (AD-2).
- §5 minimal change set; §6 no renames: `AUTOFIX_AUTO_MERGE_HEAD_BOUND`, `AUTOFIX_DET_SKIP_MERGE_BOUND`, `AUTOFIX_AUTO_MERGE_SKIPPED`, and `AUTO_MERGE_READY_LABELS_ALLOWED` keep their names and meanings; this plan adds the value `action=squash_sync`, the reason `merge_not_ready`, and one new key `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED` (unused anywhere today), registered in `agents.md` "Stable log prefixes (contractual)" in both the bullet list and the `LOG_PREFIX.name=` block (the #5316 lesson).
- §9 tabs in shell; YAML stays 2-space.
- §15: the cancellation reuses the gate job's existing `pulls/{n}` read (one more field in its `--jq` projection) and adds `gh pr merge --disable-auto` only when an enrollment exists on a `claude/*` head. The synchronous merge replaces the `--auto` call one for one.
- §18: no new script, schedule, or supervisor; no `docs/scripts-pending-removal.md` entry.
- §20 changelog fragment; §27 `review_autofix.yml` stays under 480,000 bytes (459,033 today).

### Automation (§18.E)

- Modifies existing code only: `scripts/review_enable_auto_merge.sh` and `.github/workflows/review_autofix.yml`.
- Entry points: `.github/workflows/review_autofix.yml` job `gate` step `evaluate`, job `codex-agent` step "Enable auto-merge on PR", and job `deterministic-skip-merge` (the review workflow's `pull_request` / `workflow_dispatch` / `workflow_call` triggers, via `internal-review.yml` here and `ai-review.yml` in consumers).
- No supervisor, no DB work, no registry entry.

## Approach

1. **`scripts/review_enable_auto_merge.sh`.** Just before the `--squash --auto` tail (after the forward-merge and orchestrator suppressors, which never match a `claude/*` head), add a `claude/*` branch: log `AUTOFIX_AUTO_MERGE_HEAD_BOUND pr=… head_sha=… action=squash_sync`, run `gh_retry gh pr merge "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --squash --match-head-commit "${INITIAL_HEAD_SHA}"`; on success set `AUTO_MERGE_READY_LABELS_ALLOWED=true`; on failure log `AUTOFIX_AUTO_MERGE_SKIPPED … reason=merge_not_ready` and a `::warning::`; `exit 0` either way. Update the header comment (log keys) and the gate block comment.
2. **`deterministic-skip-merge` job.** Add an `elif [[ "${PR_HEAD_REF}" == claude/* ]]` branch before the final `else` with the same synchronous merge, `AUTOFIX_DET_SKIP_MERGE_BOUND … action=squash_sync`, and `merge_not_ready` refusal (summary `REFUSED (… merge_not_ready)`, no labels).
3. **Gate job `evaluate` step.** Add `auto_merge: (.auto_merge != null)` to the existing `pulls/{n}` projection, record it as `pr_auto_merge`, and after the fetch, for an open `claude/*` PR with `pr_auto_merge=true`, run `gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" --disable-auto` and log `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED`; a failed cancel warns and continues (AD-5).
4. **`scripts/claude_merge_hold_gate.py`** docstring: callers merge `claude/*` heads synchronously; the #4900 path must do the same. No behaviour change.

Alternatives considered: keeping `--auto` and adding hold-triggered cancellation (the cancel would race the armed merge and needs a new `issue_comment` trigger in every consumer wrapper; AD-1/AD-4); falling back to `--auto` when the synchronous merge is refused (reopens the finding's window; AD-2).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — synchronous claude/* merge and stale auto-merge cancellation.**
   - Files: `scripts/review_enable_auto_merge.sh`, `.github/workflows/review_autofix.yml`, `scripts/claude_merge_hold_gate.py` (docstring), `tests/test_claude_merge_hold_gate.py`, `README.md`, `agents.md`, `changelog.d/5565-claude-sync-merge.md` [new].
   - Done when: the tests below pass locally; no `gh pr merge … --auto` call is reachable for a `claude/*` head in either path; the gate job cancels an enrollment on an open `claude/*` PR; non-`claude/*` heads keep their exact merge calls; `review_autofix.yml` < 480,000 bytes.
   - Rollback: revert the phase PR; no data or state to unwind.

## Implementation Steps

1. Helper: add the synchronous `claude/*` branch and update its header comment.
2. Workflow: deterministic-skip synchronous branch; gate-job projection field and cancellation block.
3. Gate docstring note.
4. Tests (below).
5. Docs: README "Merge hold gate" paragraph, `agents.md` claims section and stable log prefixes, changelog fragment.

## Files & Modules

- `scripts/review_enable_auto_merge.sh`
- `.github/workflows/review_autofix.yml`
- `scripts/claude_merge_hold_gate.py`
- `tests/test_claude_merge_hold_gate.py`
- `README.md`, `agents.md`
- `changelog.d/5565-claude-sync-merge.md` [new]

## Tests

- `tests/test_claude_merge_hold_gate.py` (fake `gh`): the helper merges a clean `claude/*` head with `["pr", "merge", "42", "--repo", REPO, "--squash", "--match-head-commit", HEAD]` and no `--auto`; a refused synchronous merge logs `reason=merge_not_ready`, leaves `AUTO_MERGE_READY_LABELS_ALLOWED=false`, and exits 0; a non-`claude/*` head keeps `--squash --auto --match-head-commit`; the deterministic-skip step merges a `claude/*` head synchronously, refuses with `merge_not_ready` on failure (no labels), and keeps `--auto` for other heads; no `--auto` merge text is reachable for `claude/*` heads (contract); the gate job's projection carries `auto_merge`, and its cancellation block runs `--disable-auto` only for an open `claude/*` PR with an enrollment (contract, plus executing the extracted block with a fake `gh`); `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED` is a registered stable prefix.
- Existing suites that must stay green: `tests/test_claude_merge_hold_gate.py`, `tests/test_review_autofix_review_pipeline_contract.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_workflow_file_size_limit.py`, `tests/inventory_parity.py`, and actionlint / shellcheck as CI runs them.

## Risks & Mitigations

- A `claude/*` PR whose base requires checks or reviews that are still pending when the merge step runs is left open (`merge_not_ready`) instead of armed — ACCEPTED (AD-2). In Claude-fixer mode the checks have already settled; the remaining cases are a required check on the review run itself, a required human review (the approver can merge), and deterministic-skip PRs in repos with required checks. The next review run (a push or a dispatch) re-evaluates; #4900's pending-checks path is the automated re-evaluation and must merge synchronously too.
- A base with a merge queue: `gh pr merge` without `--auto` adds the PR to the queue, which is itself an armed merge. No repo in `.github/ai/consumer_repos.json` is known to use a merge queue — ACCEPTED, documented in `agents.md`.
- Enrollments made before this change or by hand stay armed until the PR's next review run — ACCEPTED (AD-4); every push starts one.
- The seconds between the gate's claim read and the synchronous merge call — ACCEPTED; that is the recommended "revalidate immediately before a synchronous head-bound merge".

## Rollout

Ships with the #5316 project (its final PR carries this project branch's merge) and then the next `@stable` release through the reusable workflow; no flag. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How does the review workflow merge a `claude/*` head the gate allowed? — Picked: A — a synchronous head-bound merge (`gh pr merge --squash --match-head-commit`, no `--auto`) right after the gate, in both paths. Alternatives: B — keep `--auto` and add hold-triggered cancellation; C — keep `--auto` and accept the window. Why: an enrollment cannot re-run the gate when checks settle, so B still races the armed merge and C leaves the finding open (§1). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens when GitHub refuses the synchronous merge (checks or reviews still required)? — Picked: A — enroll nothing; log `AUTOFIX_AUTO_MERGE_SKIPPED reason=merge_not_ready`, warn, grant no ready labels; the next review run or #4900's pending-checks path re-evaluates. Alternatives: B — fall back to `--auto`. Why: B reopens the finding's window; a missed merge is recoverable, a held PR merging is not (§1, #5316 AD-4). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is an existing enrollment cancelled on a head change? — Picked: A — the gate job of every review run cancels any enrollment on an open `claude/*` PR (`gh pr merge --disable-auto`), reusing its `pulls/{n}` read. Alternatives: B — cancel only in the merge helper; C — no cancellation. Why: a push starts a review run but only a clean one reaches the helper, so B misses the push that carries findings (§15: one extra field, a mutation only when enrolled). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Add a separate cancellation when a hold is posted without a push? — Picked: A — no; after AD-1 the workflow never enrolls a `claude/*` PR, so a hold cannot be outrun by a workflow-made enrollment, and stray enrollments are cancelled on the next review run. Alternatives: B — an `issue_comment`-triggered workflow; C — a job in the hourly catch-all sweep. Why: B needs a new trigger in every consumer wrapper and C acts up to an hour late (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] What if the cancellation call fails? — Picked: A — log `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED … result=failed`, warn, and continue the run. Alternatives: B — fail the gate job. Why: failing the gate stops the review but not GitHub's armed merge, so it protects nothing; the run's own merge stays synchronous and gated. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which log keys carry the new outcomes? — Picked: A — `action=squash_sync` on the existing `AUTOFIX_AUTO_MERGE_HEAD_BOUND` / `AUTOFIX_DET_SKIP_MERGE_BOUND` lines, `reason=merge_not_ready` on `AUTOFIX_AUTO_MERGE_SKIPPED`, and one new key `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED`, registered in `agents.md`. Alternatives: B — a new key for every outcome. Why: extends the existing audit lines without renaming (§6, §5). Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Edit CLAUDE.md §26.H ("head-bound auto-merge")? — Picked: A — no; document in README and `agents.md`. Alternatives: B — edit CLAUDE.md and `workflow-templates/CLAUDE.md`. Why: the merge is still automatic and head-bound, and a CLAUDE.md edit ships to every consumer session for a workflow-internal detail (§5, #5316 AD-8). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` (2026-09-30): `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The issue text asks for nothing outside the finding's scope.
- The issue base is the #5316 project branch; its final PR #5323 (draft) targets `main`. This project's final PR targets that branch, so steps 12–13 do not run (`Activation: n/a`), and the final-merge stage closes #5565 with `ai:merged`.

## References

- Issue #5565; #5316 (merge hold gate), PR #5382, final PR #5323; #4900 / PR #4922 (pending-checks merge path); CLAUDE.md §1, §15, §26.H, §27, §28.
