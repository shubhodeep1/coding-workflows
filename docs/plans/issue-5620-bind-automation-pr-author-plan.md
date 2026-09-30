# Require the automation account as author before an automation-named head closes an issue

Source issue: shubhodeep1/coding-workflows#5620 (https://github.com/shubhodeep1/coding-workflows/issues/5620)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

`close_merged_issues_sweep` and `issue_pr_status.yml` accept a merge into an issue's non-default target branch when the PR's head is named like an automation branch (`ai/issue-<n>`, `fix/<n>-followup-<epoch>`) and lives in this repository. Any account with write access can create a branch with that name, so the name proves nothing about who made the PR. This plan also requires the PR's author to be the account `GH_PAT` authenticates as, which is the account every automation PR is opened with. The `issue_pr_status.yml` link that ties an `ai/issue-<n>` head to issue `<n>` needs the same author and same-repository proof.

## Context

- Issue #5620 is a `security-audit.yml` finding (STRIDE: Spoofing, medium, confidence 9/10) against `scripts/gh_helpers.sh:1845` on this project's base branch: `pr_head_ref_is_issue_automation_branch`, added by issue #5226 (plan `docs/completed/issue-5226-close-sweep-automation-identity-plan.md`).
- Exploit scenario from the issue: a repository writer creates an `ai/issue-N` head and merges its PR into a matching non-default target. The workflow and the sweep treat the name and the same-repository location as proof of automation, so the issue gets a terminal transition (`ai:merged`, then closed by the sweep or by the workflow for a managed child) without anyone checking that a bot made the PR.
- Recommendation from the issue: bind accepted PR numbers and head SHAs to trusted automation runs, verify the issuing identity, and protect automation branch refs against non-bot writes.
- Who opens automation PRs today, all with `GH_PAT`:
  - `.github/workflows/implement.yml` step `Create Pull Request` (`GH_TOKEN: ${{ secrets.GH_PAT }}`, `gh pr create`), `ai/issue-<n>` heads, body `Automated implementation. Closes <issue URL>`;
  - `scripts/orchestrate_poll_process.sh` judge follow-ups (`gh pr create`, `fix/<n>-followup-<epoch>` heads), run by `orchestrate_poll.yml` with `GH_TOKEN: ${{ secrets.GH_PAT }}`.
  So the PR author (`.user.login` in `pulls/<n>`, `github.event.pull_request.user.login` in the event) of every genuine automation PR is the `GH_PAT` login. A writer who is not that account cannot open a PR as it.
- Precedent for resolving the `GH_PAT` login: `review_autofix.yml` (`gate_fetch_marker_comments`, `gh api user --jq '.login // ""'` once per gate evaluation) and `review_autofix_sweep.yml` (`CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` falls back to `gh api user --jq .login`). Both authenticate workflow-owned markers against that login.
- `issue_pr_status.yml` also links a PR to issue `<n>` from its head name alone (`BRANCH_ISSUE_NUMBER`, `sed -nE 's#^ai/issue-([0-9]+)([-/].*)?$#\1#p'`). That link drives labelling and closing on every close event, including a default-branch merge and an unmerged close, and does not check the head repository. This is the same spoofing root cause (AD-4).

## Goals

- Off the default branch, `close_merged_issues_sweep` accepts a merge into the issue's integration branch or a managed child's project branch only when the head is an automation branch for the issue, the head repository is this repository (both unchanged), **and** `.user.login` of the PR is the `GH_PAT` login. Otherwise it logs `rejected=unverified_identity` (existing line, with an `author=<login>` field appended) and tries the next candidate.
- `issue_pr_status.yml` applies the same author rule in its per-issue gate for merges into a non-default target branch. A rejected PR leaves the issue's labels and state unchanged and is not finalized in lineage (#5227 list), with a log line naming the author.
- `issue_pr_status.yml` links a PR to issue `<n>` from its `ai/issue-<n>` head name only when the head repository is this repository and the PR author is the `GH_PAT` login. The check runs only when the head-derived issue is not already linked by a closing keyword or the body fallback, so genuine automation PRs (which carry `Closes <issue URL>`) cost no extra call.
- When the `GH_PAT` login cannot be resolved, the rule fails closed. The sweep skips that issue for the cycle with a `::warning::` and without the stale-label Telegram alert. The workflow leaves the issue unchanged with a `::warning::`.
- Default-branch merges keep today's closing-keyword identity (no author check).
- Tests cover each accept and reject path, the lookup failure, and the one-call cache. A `changelog.d/` fragment (§20, `security`) and the README rows describe the rule.

## Non-goals

- Binding head SHAs to automation runs with markers written at every automation push (AD-1 B). The implement, review-autofix, conflict-resolver and judge paths would all have to write and read them, which is a multi-workflow change for a residual risk (a writer pushing onto an automation-authored PR) that branch protection already governs.
- Changing repository settings (a ruleset that limits `ai/issue-*` and `fix/*-followup-*` refs to the automation account). That is a §23.C administrative write and is never done unattended. It is listed as an operator recommendation under Rollout (AD-5).
- `_pr_json_is_issue_implementation_pr`, stall recovery, validation fix-up evidence and linked-PR adoption. Those callers have their own base rules (as in #4813's non-goals).
- The unmerged-close behaviour for issues linked by a closing keyword (`ai:closed` plus close, whatever the base), already out of scope in #4813's plan.

## Constraints

- §1: security first. Every new branch fails closed. A missing helper in an older `gh_helpers.sh` behaves as "not the automation account".
- §5: the author check is added only where the head-name identity is accepted today (the sweep's #5226 block, the workflow's #5226 block, and the head-derived link).
- §6: no identifier is renamed. Existing log lines keep their text. The sweep's `rejected=unverified_identity` line gains a trailing `author=<login>` field; existing test assertions match its prefix. New identifiers, checked for collisions with `git grep` before use: `pr_author_is_automation_account`, `GH_HELPERS_AUTOMATION_LOGIN`, `PR_AUTHOR_LOGIN`, `_sweep_pr_author_login`, `_sweep_author_rc`, `sweep_identity_lookup_failed`, `automation_login_unavailable`, `_status_author_rc`, `_branch_author_rc`.
- §9: tabs in `scripts/gh_helpers.sh`, whose functions use tabs. `scripts/orchestrate_poll_process.sh`'s `close_merged_issues_sweep` uses 2-space indentation, and the edit matches it. YAML stays 2-space.
- §14: `issue_pr_status.yml` and `scripts/gh_helpers.sh` reach consumers through `@stable` together (the workflow fetches `gh_helpers.sh` from `stable`). The workflow keeps a fail-closed stub for the new helper.
- §15: one `gh api user` REST call at most per poller process and per workflow run, only when a candidate reaches the identity check, and cached on success. None of the existing calls (issue lists, timeline, `pulls/<n>`, the closing-references GraphQL, the event payload) exposes the login `GH_PAT` authenticates as, so the lookup cannot be merged into any of them.
- §19: PR bodies use `Refs #5620`. The final PR merges into a non-default base, so it also uses `Refs #5620`, and the final-merge stage closes the issue explicitly.
- §27: `issue_pr_status.yml` is about 40 KB, far below the 480,000-byte guard.

## Approach

1. **One helper.** Add `pr_author_is_automation_account <author login>` to `scripts/gh_helpers.sh`, after `pr_head_ref_is_issue_automation_branch`. It returns 0 when the login matches the `GH_PAT` login (case-insensitive, as GitHub logins are), 1 when it does not or the author is empty, and 2 when the login cannot be resolved. The login is resolved with `gh api user --jq '.login // ""'` on first use and cached in `GH_HELPERS_AUTOMATION_LOGIN` for the rest of the shell. Only a success is cached, so a transient failure is retried by the next caller. Callers invoke it directly, never in `$(...)`, so the cache survives.
2. **Sweep.** In the #5226 block of `close_merged_issues_sweep`, after the head-name and head-repository checks pass, read `_sweep_pr_author_login` (`.user.login`) and call the helper. rc 0 accepts. rc 1 falls through to the existing `rejected=unverified_identity` line, now with `author=<login>`. rc 2 logs `CLOSE_MERGED_SWEEP issue=<n> origin=<o> candidate_pr=<p> rejected=automation_login_unavailable`, sets `sweep_identity_lookup_failed=true`, and continues. After the loop, when no candidate was accepted and the flag is set, the issue is skipped for this cycle with `::warning::CLOSE_MERGED_SWEEP issue=<n> origin=<o> automation_login_unavailable — skipping this cycle.`, the same way `pr_fetch_failed` is handled (no Telegram alert).
3. **Workflow gate.** In `issue_pr_status.yml` step `Update linked issue labels when PR closes`, add `PR_AUTHOR_LOGIN: ${{ github.event.pull_request.user.login }}` and a fail-closed stub for the helper. After the #5226 head check passes for a non-default target, call the helper. rc 1 logs `PR #<p> merged into <base> is not an automation PR for issue #<n> (author <login> is not the automation account); leaving its labels and state unchanged.` rc 2 logs a `::warning::` saying the automation account could not be resolved. Both `continue` before the label, the close and the lineage list.
4. **Head-derived link.** When `BRANCH_ISSUE_NUMBER` is not already in `ISSUE_NUMBERS`, add it only when `PR_HEAD_REPO_FULL_NAME` equals `REPOSITORY` and the helper returns 0 for `PR_AUTHOR_LOGIN`. Otherwise log `Ignoring the link from head <ref> to issue #<n>: …` with the reason. When it is already linked, nothing changes.

Alternatives considered: SHA-to-run markers (AD-1 B), a new repository variable for the automation login (AD-2 B), and failing open when the login lookup fails (AD-3 B). The Auto-decisions section gives the reasons.

## Phases & Merge Strategy

**Single phase.** Issue mode (CLAUDE.md §28.A) authorises a single-phase plan. The helper, its two callers, and the tests are one invariant ("an automation-named head counts only when the automation account opened the PR"). Shipping one caller without the other would leave the same spoofing path open.

1. **Phase 1 — require the automation account as PR author before an automation-named head counts.**
   - Files: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/issue_pr_status.yml`, `tests/test_gh_helpers_issue_automation_branch.py`, `tests/test_issue_pr_status_target_branch_gate.py`, `tests/test_orchestrate_poll_process.py`, `README.md`, `changelog.d/5620-automation-pr-author-identity.md` [new]. No `.claude/**` path.
   - Done when: the helper, workflow-step, and sweep tests pass (new and existing); `bash -n` is clean on the touched scripts; the workflow YAML parses; `tests/test_workflow_file_size_limit.py` and `tests/test_issue_pr_status_payload_fallback_contract.py` pass; the README rows and the changelog fragment describe the rule.
   - Rollback: revert the phase PR. `ENABLE_CLOSE_MERGED_ISSUES=false` still disables the sweep as an emergency stop.

## Implementation Steps

Phase 1:

1. `scripts/gh_helpers.sh`: add `pr_author_is_automation_account()` after `pr_head_ref_is_issue_automation_branch()`, with a header comment naming both callers, issue #5620, the return codes, the cache, and the one-call budget.
2. `scripts/orchestrate_poll_process.sh` `close_merged_issues_sweep`:
   - header comment: extend the "Identity rule" paragraph with the author rule and the `automation_login_unavailable` skip;
   - per issue: initialise `sweep_identity_lookup_failed=false` next to `sweep_pr_fetch_failed`;
   - in the #5226 block: read `_sweep_pr_author_login`, call the helper (guarded with `type … >/dev/null 2>&1`; missing means reject), and handle rc 0 / 1 / 2 as in the Approach; append `author=${_sweep_pr_author_login:-unknown}` to the `rejected=unverified_identity` line;
   - after the loop: the `automation_login_unavailable` skip before the `ready_label` and `merged_label` no-PR policies.
3. `.github/workflows/issue_pr_status.yml` step `Update linked issue labels when PR closes`:
   - env: `PR_AUTHOR_LOGIN`;
   - stub: `pr_author_is_automation_account() { return 1; }` when the helper is missing;
   - head-derived link: the check from Approach step 4;
   - per-issue gate: the author check from Approach step 3, after the #5226 head check.
4. `tests/test_gh_helpers_issue_automation_branch.py`: helper tests with a PATH `gh` stub: exact and case-insensitive match; mismatch; empty author (no `gh` call); lookup failure (rc 2, not cached, retried); one `gh` call for repeated checks in one shell.
5. `tests/test_issue_pr_status_target_branch_gate.py`: serve `api user` from the stub state (with a failure switch) and count the calls; pass `PR_AUTHOR_LOGIN` (default: the automation login). New cases: writer-authored `ai/issue-<n>` head on the integration branch is left untouched and not finalized; the same for a managed child's project branch; lookup failure leaves the issue untouched with a warning; the automation-authored head still labels; a default-branch merge by another author still labels and closes with no `api user` call; a head-derived link from a writer or from a fork is ignored; a head-derived link from the automation account is kept.
6. `tests/test_orchestrate_poll_process.py`: add `user.login` to the mock `pulls/<n>` JSON (default: the automation login, overridable per PR) and serve `api user` from the store (with a failure switch). New sweep cases: writer-authored automation head is rejected with `author=`; lookup failure skips without closing and without the stale-label alert; two issues on non-default merges make one `api user` call; a default-branch merge by another author still closes.
7. `README.md`: extend the `ENABLE_CLOSE_MERGED_ISSUES` row and the `issue_pr_status.yml` row with the author rule.
8. `changelog.d/5620-automation-pr-author-identity.md` [new], section `security`.
9. `.github/workflows/ci.yml` already runs the two gate test files and the helper test file as scripts; update each file's `__main__` runner for the new tests.

## Files & Modules

- `scripts/gh_helpers.sh`
- `scripts/orchestrate_poll_process.sh`
- `.github/workflows/issue_pr_status.yml`
- `tests/test_gh_helpers_issue_automation_branch.py`
- `tests/test_issue_pr_status_target_branch_gate.py`
- `tests/test_orchestrate_poll_process.py`
- `README.md`
- `changelog.d/5620-automation-pr-author-identity.md` [new]

## Tests

- Unit and runtime (pytest and the files' script runners): the helper tests, the workflow-step runtime tests, the sweep tests in `tests/test_orchestrate_poll_process.py` (`-k close_merged_issues_sweep`), `tests/test_issue_pr_status_payload_fallback_contract.py`, `tests/test_gh_helpers_issue_body_integration_branch.py`.
- Static: `bash -n scripts/gh_helpers.sh scripts/orchestrate_poll_process.sh`; the YAML parses; `tests/test_workflow_file_size_limit.py`.
- End to end: the chain's conformance and runtime validation stages. The security pass is skipped for this automation-produced finding (plan header).

## Risks & Mitigations

- **A consumer whose automation PRs are opened by a different account** than `GH_PAT` would stop closing issues on non-default merges. Mitigation: every PR-creating path in the library uses `GH_PAT`, and the workflow and poller run with the same secret. A rejected merge falls through to the existing policies (the stale-label WARNING for `ai:merged`), so it is visible, never silent.
- **A transient `gh api user` failure.** The sweep skips the issue for one cycle without an alert and retries next cycle. The workflow leaves the issue unchanged with a warning; the sweep then closes it once the label is present, or the issue stays open, which fails closed.
- **The `GH_PAT` account is also a person** (here the repository owner). A PR that person opens by hand with an automation head name still counts. Accepted: that account already holds the credential the automation uses.
- **Residual: a writer pushes commits onto an automation-authored PR** before it merges. The PR is still the automation's PR for the issue, so the terminal transition is the intended one; content integrity belongs to branch protection and review. The ruleset recommendation under Rollout narrows this.

## Rollout

Ships to the base branch (`claude/implement-plan-issue-4813-close-sweep-target-branch-merges`) with this project's final PR, then to `main` with project #4813's final PR (#4826), then to consumers on the next `@stable` release (§14). No flag: the change narrows destructive actions (labelling and closing an issue). `ENABLE_CLOSE_MERGED_ISSUES=false` remains the sweep's emergency stop.

Operator recommendation (not performed by this project, §23.C): add a repository ruleset that restricts creating and updating `ai/issue-*` and `fix/*-followup-*` refs to the automation account. That covers the recommendation's third part at the source.

## Auto-decisions

- AD-1 [plan, 2026-09-30] What proves that an automation-named PR was made by the automation? — Picked: A — the PR's author is the account `GH_PAT` authenticates as (every automation PR is opened with it), checked in addition to the head name and head repository. Alternatives: B — bind each head SHA to trusted automation runs through markers written at every automation push; C — rely on a repository ruleset only. Why: A closes the reported writer path with one cached call and no change to the PR-producing workflows; B spans implement, review-autofix, the conflict resolver and the judge; C is an administrative write this chain never performs. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the automation login come from? — Picked: A — `gh api user` with the `GH_PAT` the caller already runs with, resolved lazily and cached on success, as `review_autofix.yml` does for its markers. Alternatives: B — a new repository variable with that lookup as the fallback; C — a hardcoded login. Why: A needs no new configuration in consumers and cannot drift from the credential that opens the PRs; B adds a setting that can be wrong. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens when the login cannot be resolved? — Picked: A — fail closed; the sweep skips the issue for the cycle with a warning and no stale-label alert, and the workflow leaves the issue unchanged with a warning. Alternatives: B — fail open and accept the head-name identity. Why: §1 security first; B reopens the reported path whenever `GET /user` fails. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Does the author check also cover `issue_pr_status.yml`'s link from an `ai/issue-<n>` head to issue `<n>`? — Picked: A — yes; a head-derived link counts only when the head repository is this repository and the author is the automation account, and only when the issue is not already linked by a closing keyword or the body fallback. Alternatives: B — no, limit the change to the non-default merge gate the finding names. Why: the link is the same name-as-identity trust and drives labels and closes on every close event, including a fork PR and an unmerged close; genuine automation PRs carry `Closes <issue URL>`, so A costs them nothing. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Does this project protect automation branch refs (the recommendation's third part)? — Picked: A — no; list a ruleset for `ai/issue-*` and `fix/*-followup-*` as an operator recommendation. Alternatives: B — change repository rulesets from the chain. Why: repository administration is §23.C ask-first and never performed unattended (§28.C). Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Do default-branch merges need the author check? — Picked: A — no; keep the closing-keyword identity there. Alternatives: B — require the automation author on every base. Why: GitHub closes the issue on a default-branch closing-keyword merge anyway, the #5226 plan kept this rule, and B would stop human PRs that fix an issue from labelling it. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Base branch check (2026-09-30): PR #4826 (head `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`, base `main`) is open, draft, unmerged.

## References

- Issue #5620 (this finding); audit tracker #3576.
- Issue #5226 and `docs/completed/issue-5226-close-sweep-automation-identity-plan.md` (the head-name identity rule); issue #5227 (lineage only for accepted merges); issue #4957 (managed means the label); issue #4813 and its plan (the target-branch rule).
- `review_autofix.yml` `gate_fetch_marker_comments` and `review_autofix_sweep.yml` (the `gh api user` precedent).
