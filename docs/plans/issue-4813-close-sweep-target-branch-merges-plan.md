# Close and label issues only on merges into their target branch

Source issue: shubhodeep1/coding-workflows#4813 (https://github.com/shubhodeep1/coding-workflows/issues/4813)
Base branch: main
Security pass: run

## Summary

`close_merged_issues_sweep` and `issue_pr_status.yml` treat any merged PR that names an issue with a closing keyword as that issue's finished work, whatever branch the PR merged into. This plan makes both count a merge only when it lands on the issue's target branch: the default branch, the branch the issue's own `Integration branch:` / `Target branch:` line names, or (for orchestrator-managed child issues) their integration branch, as today.

## Context

Incident, 2026-09-28 (issue #4813):

- #4688 is an issue-mode security follow-up of project #4586. Its body names `Integration branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`.
- Its completion PR #4748 merged at 10:40:21Z into #4688's **own** project branch `claude/implement-plan-issue-4688-bind-rejection-votes-to-finding-ids`. That is neither `main` nor #4688's integration branch. The body said `Refs #4688`, but its prose also read "the chain closes #4688 explicitly".
- Run 36411092888 of `Internal: Issue-PR Status Sync` (`.github/workflows/issue_pr_status.yml`, step `Update linked issue labels when PR closes`) found #4688 through the PR body/title fallback (`extract_repo_scoped_issue_refs_from_text` reads `closes #4688` as a closing keyword). It applied `ai:merged` at 10:41:05Z with no base-branch check, then logged `PR merged into claude/implement-plan-issue-4688-…; issue #4688 remains open.` The `shubhodeep1` actor is the workflow's `GH_PAT` account. This answers the issue's item 2.
- At 10:47:35Z `close_merged_issues_sweep` (`scripts/orchestrate_poll_process.sh:3886`) picked #4688 up by its `ai:merged` label. `_pr_json_is_issue_implementation_pr` (`scripts/orchestrate_poll_process.sh:15047`) accepted #4748 on the same closing-keyword match, and the sweep closed the issue. The chain still had its final merge (#4694 into #4586's branch) to do.

Two existing flows rely on merges into a branch other than the default branch counting:

- **Orchestrator-managed child issues** (`ai:orchestrator-managed` label or `Managed by: AI Orchestrator` body marker) merge into `orchestrator/project-<N>`. `issue_pr_status.yml` closes them on that merge regardless of base, and the sweep is their documented backstop (README `ENABLE_CLOSE_MERGED_ISSUES` row).
- **Security follow-ups built by the Codex pipeline** name `Integration branch: claude/implement-plan-<slug>` (`scripts/security_audit.sh`), and their `ai/issue-<n>` PRs target that branch. Today they get `ai:merged` from `issue_pr_status.yml` and are closed by the sweep. The parent project's security-pass checker waits for "closed or `ai:merged`" (`.claude/commands/implement-plan-claude.md` step 9).

So "default branch only", read literally, would strand both. The rule this plan uses keeps them working (AD-1).

## Goals

- `close_merged_issues_sweep` closes an issue on a merged implementation PR only when the PR's `base.ref` is the repository's default branch, the issue's declared integration branch, or the issue is orchestrator-managed. Any other merged PR is rejected with a `CLOSE_MERGED_SWEEP … rejected=non_target_base` log line and falls through to the existing no-merged-PR policy of the issue's label class.
- The sweep reads the base and the default branch from the `pulls/<n>` JSON it already fetches (`.base.ref`, `.base.repo.default_branch`) and the issue body from the `gh issue list` calls it already makes. No new API call (§15).
- `issue_pr_status.yml` applies `ai:merged` on a merged PR only under the same rule. A merge into any other branch leaves the issue's labels and state untouched and logs why.
- Tests prove: a closing-keyword PR merged into a `claude/implement-plan-*` base leaves the issue open; a default-branch merge still closes it; a merge into the issue's declared integration branch still closes it; an orchestrator-managed child still closes on its integration-branch merge.
- A `changelog.d/` fragment (§20, `fixed`).

## Non-goals

- `_pr_json_is_issue_implementation_pr` itself does not change. Stall recovery, validation fix-up evidence and linked-PR adoption also call it, and they have their own base rules.
- The closing-keyword parser `extract_repo_scoped_issue_refs_from_text` does not change. Reading prose like "closes #N" as a closing keyword matches GitHub's own grammar.
- The issue-mode chain's explicit close + `ai:merged` after a non-default final merge. It is already specified in `.claude/commands/implement-plan-claude.md` ([Issue Mode](../../.claude/commands/implement-plan-claude.md#issue-mode), "Closing the issue"), so no `.claude/**` edit is needed (AD-5).
- The existing `issue_pr_status.yml` behaviour for a PR closed **without** merging (label `ai:closed` and close) is out of scope.

## Constraints

- §5 minimal change set: the gate is added where the two decisions are made. Nothing else in the sweep or the workflow step changes.
- §6: no identifier is renamed. The existing log prefixes (`CLOSE_MERGED_SWEEP`, `origin=merged_label` / `origin=ready_label`) stay. New identifiers (`issue_body_integration_branch`, `_sweep_issue_body`, `_sweep_pr_base_ref`, `_sweep_default_branch`, `PR_BASE_DEFAULT_BRANCH`, `ISSUE_INTEGRATION_BRANCHES`) are checked for collisions before use.
- §9: tabs in new shell functions where the surrounding file uses tabs; YAML stays 2-space.
- §14: `issue_pr_status.yml` and `scripts/gh_helpers.sh` reach consumer repos through `@stable` (the workflow fetches `gh_helpers.sh` from `stable`). Both ship together, and the workflow keeps a fail-closed fallback when the helper is missing.
- §15: no new GitHub API call in either path. The workflow reads issue bodies from payloads it already fetches (`closingIssuesReferences { body }`, the batched `issue(number:) { body }` GraphQL alias query, and the per-issue REST fallback).
- §19: PR bodies use `Refs #4813` except the final PR, which uses `Fixes #4813` (the base is the default branch).
- §27: `issue_pr_status.yml` is about 36 KB, far below the 480,000-byte guard.

## Approach

1. **One parser for the issue's target branch.** Add `issue_body_integration_branch <body>` to `scripts/gh_helpers.sh`. It prints the branch from the canonical `Integration branch:` line, else the `Target branch:` alias, using the same two regexes as `scripts/orchestrate_lib.py` `extract_integration_branch` (`INTEGRATION_BRANCH_LINE_RE`, `TARGET_BRANCH_LINE_RE`). It prints nothing when neither line is present. Both the poller (line 20) and `issue_pr_status.yml` already source `gh_helpers.sh` (AD-4).
2. **Sweep.** Add `body` to both `gh issue list --json` field lists and carry it through the dedup `jq`. After a candidate passes `_pr_json_is_issue_implementation_pr`, accept it only when `.base.ref` equals `.base.repo.default_branch` (non-empty), or equals `issue_body_integration_branch` of the issue body (non-empty), or the issue is orchestrator-managed (`ai:orchestrator-managed` label or `Managed by: AI Orchestrator` in the body, the same test `issue_pr_status.yml` uses). Otherwise log `CLOSE_MERGED_SWEEP issue=<n> origin=<o> candidate_pr=<p> rejected=non_target_base base=<ref> default_branch=<d> issue_base=<b>` and try the next candidate. With no accepted candidate, the existing `no_merged_pr_found` policy applies unchanged (AD-6). An empty default branch in the PR JSON fails closed: the merge does not count as a default-branch merge (AD-2).
3. **`issue_pr_status.yml`.** Pass `PR_BASE_DEFAULT_BRANCH: ${{ github.event.pull_request.base.repo.default_branch }}` and resolve it with `main` as the fallback (the value hardcoded today). While classifying linked issues, record each issue's `issue_body_integration_branch` from the payloads the step already holds. In the per-issue loop, for a merged PR: when the base is the default branch, the issue's integration branch, or the issue is managed, keep today's behaviour (label, then close on a default-branch or managed merge). Otherwise skip the label and log `PR merged into <base>, which is not issue #<n>'s target branch (default <d>, integration <b|none>); leaving its labels and state unchanged.` The close gate compares against the resolved default branch instead of the literal `main` (AD-3).

Alternatives considered: default branch only (AD-1 B), which strands managed children and Codex-built follow-ups; and a separate `repos/<r>` read for the default branch (AD-2 B), which adds an API call that §15 forbids when the payload already has the value.

## Phases & Merge Strategy

**Single phase.** Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes the scope, and the sweep change, the label change and their tests are one invariant ("a merge counts only on the issue's target branch"). Shipping one half would leave the other source of the same mistake in place.

1. **Phase 1 — gate issue close and `ai:merged` on the target branch.**
   - Files: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/issue_pr_status.yml`, `tests/test_orchestrate_poll_process.py`, `tests/test_issue_pr_status_target_branch_gate.py` [new], `tests/test_gh_helpers_issue_body_integration_branch.py` [new], `README.md`, `changelog.d/4813-close-sweep-target-branch-merges.md` [new]. No `.claude/**` path.
   - Done when: the new and existing sweep tests pass, the workflow step's runtime test passes, the parser parity test passes, `bash -n` is clean on the touched scripts, and the README row and changelog fragment describe the rule.
   - Rollback: revert the phase PR. `ENABLE_CLOSE_MERGED_ISSUES=false` disables the sweep entirely as an emergency stop without a revert.

## Implementation Steps

Phase 1:

1. `scripts/gh_helpers.sh`: add `issue_body_integration_branch()` after `extract_repo_scoped_issue_refs_from_text` (line ~1662). It reads the body from `$1` and runs `python3 -c` with the two regexes copied from `scripts/orchestrate_lib.py:46-63`, with a "keep in sync" comment naming both other copies. It exits 0 and prints nothing on no match or a parse failure.
2. `scripts/orchestrate_poll_process.sh` `close_merged_issues_sweep` (lines 3844-4057):
   - header comment: document the target-branch rule and the `rejected=non_target_base` line;
   - both `gh issue list` calls: `--json number,labels,body`;
   - dedup `jq`: carry `body: (.body // "")`;
   - per issue: read `_sweep_issue_body`, compute `_sweep_issue_base` via `issue_body_integration_branch` (guarded with `type … >/dev/null 2>&1`, empty when missing) and `_sweep_issue_managed` (label or body marker);
   - in the candidate loop, after the implementation-PR check: read `_sweep_pr_base_ref` (`.base.ref`) and `_sweep_default_branch` (`.base.repo.default_branch`), and accept only on the target-branch rule; otherwise log the rejection and `continue`.
3. `.github/workflows/issue_pr_status.yml` step `Update linked issue labels when PR closes` (lines 186-474):
   - env: add `PR_BASE_DEFAULT_BRANCH`;
   - resolve `DEFAULT_BRANCH_NAME="${PR_BASE_DEFAULT_BRANCH:-main}"`;
   - build `ISSUE_INTEGRATION_BRANCHES` (lines `<issue>\t<branch>`) from `CLOSING_ISSUES_RESP` node bodies, the `ORCH_RESP` alias bodies, and the REST fallback's `_orch_meta.body`, with a fallback stub for `issue_body_integration_branch` when `gh_helpers.sh` lacks it (prints nothing, so only default-branch and managed merges count);
   - per-issue loop: apply the target-branch gate before `set_issue_phase_label_resilient` for merged PRs, and replace the literal `main` in the close gate and its log line with `${DEFAULT_BRANCH_NAME}`.
4. `tests/test_orchestrate_poll_process.py`:
   - mock `pulls/<n>` JSON: add `base.repo.default_branch` from the store (`main`);
   - `test_close_merged_issues_sweep_closes_ready_to_merge_with_verified_merged_pr`: its #10 is an orchestrator child merged into `orchestrator/project-192`, so give it the `ai:orchestrator-managed` label real children carry;
   - new `test_close_merged_issues_sweep_leaves_issue_open_when_pr_merged_into_other_project_branch` (closing-keyword PR with base `claude/implement-plan-issue-10-x`, issue body naming a different integration branch → not closed, `rejected=non_target_base` logged, `no_merged_pr_found` policy);
   - new `test_close_merged_issues_sweep_closes_on_default_branch_merge` (standalone issue, base `main` → closed);
   - new `test_close_merged_issues_sweep_closes_on_declared_integration_branch_merge` (body `- Integration branch: \`claude/implement-plan-parent\``, PR base the same → closed).
5. `tests/test_issue_pr_status_target_branch_gate.py` [new]: extract the step's `run:` script, as `tests/test_issue_pr_status_payload_fallback_contract.py` does, and run it under `bash` with a PATH `gh` stub that serves the GraphQL payloads and records label and close calls. Cases: merge into another project branch → no label, no close; default-branch merge → label + close; integration-branch merge → label, no close; managed child on its integration branch → label + close; resolved default branch other than `main` (`master`) → label + close.
6. `tests/test_gh_helpers_issue_body_integration_branch.py` [new]: parity between `issue_body_integration_branch` and `orchestrate_lib.extract_integration_branch` over bodies with the canonical line, the bold form, the bullet form, the `Target branch:` alias with trailing prose, both lines (canonical wins), and none.
7. `README.md`: extend the `ENABLE_CLOSE_MERGED_ISSUES` row (line 200) with the target-branch rule, and the `issue_pr_status.yml` row (line 1184) with "labels `ai:merged` only on a merge into the issue's target branch".
8. `changelog.d/4813-close-sweep-target-branch-merges.md` [new], section `fixed`.
9. Check `ci.yml` picks up the two new test files, and register them if it lists tests explicitly.

## Files & Modules

- `scripts/gh_helpers.sh`
- `scripts/orchestrate_poll_process.sh`
- `.github/workflows/issue_pr_status.yml`
- `tests/test_orchestrate_poll_process.py`
- `tests/test_issue_pr_status_target_branch_gate.py` [new]
- `tests/test_gh_helpers_issue_body_integration_branch.py` [new]
- `README.md`
- `changelog.d/4813-close-sweep-target-branch-merges.md` [new]
- `.github/workflows/ci.yml` (only if it lists test files explicitly)

## Tests

- Unit/integration (pytest): the sweep tests in `tests/test_orchestrate_poll_process.py` (new and existing `close_merged_issues_sweep` tests, plus `tests/test_linked_pr_implementation_guard.py`), the new workflow-step runtime test, the parser parity test, and the existing `tests/test_issue_pr_status_payload_fallback_contract.py`.
- Static: `bash -n scripts/gh_helpers.sh scripts/orchestrate_poll_process.sh`; the YAML parses (`python3 -c 'import yaml; yaml.safe_load(...)'`); `tests/test_workflow_file_size_limit.py`.
- End to end: the chain's conformance, security and runtime validation stages.

## Risks & Mitigations

- A Codex-built follow-up whose body lacks an `Integration branch:` line but whose PR targets a project branch would no longer close. Mitigation: `scripts/security_audit.sh` always writes the line, and `resolve_integration_ref.sh` routes the PR by the same line, so a PR on a project branch implies the line exists.
- A consumer run of `issue_pr_status.yml@stable` with an older `gh_helpers.sh`: both are fetched from `stable`, so they move together. The fallback stub fails closed (only default-branch and managed merges count).
- An `ai:merged` issue whose only implementation PR merged into a non-target branch now triggers the existing stale-label Telegram WARNING. ACCEPTED: this is the issue's requested fall-through, and after step 3 the label is no longer applied in that case.

## Rollout

Ships to `main` with the final PR, then to consumers on the next `@stable` release (§14). No flag: the change narrows a destructive action (closing an issue) and has no data migration. `ENABLE_CLOSE_MERGED_ISSUES=false` remains the emergency stop for the sweep.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Which merges count as an issue's finished work for the sweep and for `ai:merged`? — Picked: A — the default branch, the branch the issue's `Integration branch:` / `Target branch:` line names, or any base for an orchestrator-managed child. Alternatives: B — the default branch only, as the issue words it; C — change only the label source and leave the sweep's rule. Why: B strands orchestrator child issues (the sweep is their backstop) and Codex-built security follow-ups whose PRs target the named project branch; C leaves the sweep closing on a stale label. A still leaves #4688 open on #4748 (a third branch). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the default branch come from? — Picked: A — `.base.repo.default_branch` in the `pulls/<n>` JSON the sweep already fetches (empty fails closed) and `github.event.pull_request.base.repo.default_branch` in the workflow (empty falls back to `main`, today's hardcoded value). Alternatives: B — one extra `repos/<r>` read per sweep. Why: §15 forbids a new call when the payload already carries the value. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Should `issue_pr_status.yml`'s close gate keep the literal `main`? — Picked: A — compare against the resolved default branch, same line as the new label gate. Alternatives: B — keep `main` for the close gate only. Why: item 2 asks for "default-branch merge" semantics, and a consumer whose default branch is not `main` would otherwise get the label without the close. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Where does the integration-branch parser live for shell callers? — Picked: A — a new `issue_body_integration_branch` in `scripts/gh_helpers.sh`, already sourced by the poller and the workflow, with a parity test against `orchestrate_lib.extract_integration_branch`. Alternatives: B — inline `python3` regexes in each caller. Why: one copy for both callers, and the parity test keeps it in step with the Python parser. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Does item 2's "the chain applies `ai:merged` itself after its final merge" need a change? — Picked: A — no; `.claude/commands/implement-plan-claude.md` Issue Mode already has the `final-merge` stage close the issue with `ai:merged` for a non-default base, so no `.claude/**` edit. Alternatives: B — restate it in the command file (a protected path). Why: the behaviour exists; restating it adds an unattended protected-path edit for no change. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-28] What happens to an issue whose only implementation PR merged into a non-target branch? — Picked: A — log `rejected=non_target_base` and fall through to the existing no-merged-PR policy of its label class (WARNING for `ai:merged`, silent for `ai:ready-to-merge`). Alternatives: B — a new silent skip for this case. Why: the issue asks for the existing policy, and a stale `ai:merged` on such an issue is worth the alert. Applied in: phase 1 PR. Status: pending review

## References

- Issue #4813; incident issues #4688 and #4687; PRs #4748, #4694; run 36411092888.
- Issue #3817 / PR #3825 (the implementation-PR guard this plan builds on).
- `scripts/orchestrate_lib.py` `extract_integration_branch`; `scripts/resolve_integration_ref.sh`.
