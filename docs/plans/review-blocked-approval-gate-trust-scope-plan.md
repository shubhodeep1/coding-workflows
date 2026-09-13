# Review-blocked approval gate: trust-scoped policy, awaiting-approval state, and security-audit recommendation guardrails

## Summary

Narrow the review-blocked judge's trusted-human-approval gate so a person is
consulted only when the trust argument actually requires one (untrusted
author or commenter, fork head, protected automation paths, or a destructive
`close_and_reissue`), give the pipeline a first-class "awaiting human
approval" state so the stall ladder and the review sweep stop re-running the
judge every 30 minutes, and change the security-audit prompts so future
security-pass findings recommend automated controls instead of human gates.

## Automation & Wiring (§18.E)

- **New script vs extension:** no new standalone script. The change extends
  `scripts/review_rb_judge.sh`, `scripts/gh_helpers.sh`,
  `scripts/orchestrate_poll_process.sh`, `scripts/label_helpers.sh`,
  `.github/workflows/review_autofix.yml`,
  `.github/workflows/review_autofix_sweep.yml`, and the two security-pass
  prompts. Nothing requires manual invocation.
- **Scheduler entry points:** the existing review workflow
  (`.github/workflows/review_autofix.yml`, called `@main` by
  `internal-review.yml` here and by the synced `workflow-templates/ai-review.yml`
  wrapper in consumers), the orchestrator poller
  (`.github/workflows/orchestrate_poll.yml` via `internal-orchestrate-poll.yml`,
  cron every 5 minutes), and the review sweep
  (`.github/workflows/review_autofix_sweep.yml`, cron `*/30 * * * *`, this
  repository only). Every change runs inside those ticks.
- **Long-running supervisor:** none. The poller is the supervisor.
- **DB operations:** none. No MongoDB collections, indexes, or contracts (§10
  not applicable).
- **Future-removal registry (§18.F):** no single-use or long-running scripts
  are introduced; no `docs/scripts-pending-removal.md` entry.

## Decisions

### D1 — Implementation target: `main`, after project #3965 lands

- **Chosen:** implement against `main` once the `orchestrator/project-3965`
  integration branch has merged. The approval gate this plan reshapes exists
  only on that branch today (PR #4029, commit `3963708`, for security-pass
  finding `prompt-injection-authorizes-pr-merge` in issue #4017); `main` has
  no `review_blocked_*` helpers. The orchestrator project for this plan must
  not be started before #3965 is `ai:merged`.
- **Alternatives considered:** filing the work as another
  `ai:orchestrator-managed` issue against `orchestrator/project-3965`, as
  issue #4075 was.
- **Why:** user answer `Q4: A` asked for a plan of the narrower gate; a
  project of its own on `main` keeps #3965's security pass (currently
  `blocked`, fix issue #4076 active) from growing further, and every phase
  below is meaningless without the gate code being on the target branch.

### D2 — Default policy: `trust-scoped`, with `always` as the opt-out

- **Chosen:** a new `REVIEW_BLOCKED_APPROVAL_MODE` variable, default
  `trust-scoped`. `always` keeps today's behaviour for repositories that want
  every terminal decision approved by a person.
- **Alternatives considered:** keeping `always` as the default and making
  `trust-scoped` opt-in; removing the gate entirely (user rejected `Q4: C`).
- **Why:** CLAUDE.md §18 and `unattended_system_instructions.md` §20 make
  minimal human involvement the overarching goal; the review-blocked judge
  exists as the "autonomous escape path" for `ai:review-blocked` issues
  (`scripts/orchestrate_poll_process.sh`, comment above
  `_dispatch_rb_judge_for_pr`). A default that consults a person on every
  terminal decision inverts that goal. `always` preserves the security-pass
  finding's mitigation for anyone who needs it.

### D3 — Trust conditions are identity- and path-based, never content-based

- **Chosen:** approval is required when any of: the action is
  `close_and_reissue`; the PR head repository differs from the base
  repository; the PR author is not a GitHub `User` with `author_association`
  in `OWNER`, `MEMBER`, `COLLABORATOR`; any PR issue comment or review comment
  author fails the same test; the diff touches a protected path
  (`REVIEW_BLOCKED_APPROVAL_PROTECTED_PATHS`, default
  `.github/ .claude/ scripts/ prompts/ workflow-templates/ CLAUDE.md agents.md
  unattended_system_instructions.md`).
- **Alternatives considered:** a model-scored "injection likelihood";
  requiring approval only for merges into the default branch.
- **Why:** the finding is about untrusted text steering the judge. Identity
  and provenance bound who could have written that text; a model score is
  itself steerable. Integration-branch merges are not exempted separately
  because the protected-path rule already covers the automation code that
  makes a merge dangerous, and the project security pass plus the final PR
  to the default branch remain in place.

### D4 — "Awaiting human approval" is a label, not a new phase

- **Chosen:** register `ai:awaiting-human-approval` in `scripts/label_helpers.sh`
  as a non-phase marker label applied to the PR and its linked issue(s) while
  a `REVIEW_BLOCKED_APPROVAL_V1` request is pending, removed when the request
  is consumed, invalidated by a head change, or refused.
- **Alternatives considered:** a new phase in `_AI_PHASE_LABELS`; scanning PR
  comments for the pending marker on every poll tick.
- **Why:** a phase label would change the orchestrator state machine
  (`ai:clarification` → … → `ai:merged`, agents.md "Plan prompt note") and
  every consumer's stall thresholds. A marker label is additive, is already
  present in the PR list payload the sweep fetches and in the candidate
  details cache the poller fetches, so honouring it costs zero extra API
  calls (§15).

### D5 — Security-audit prompts forbid human-gate recommendations by default

- **Chosen:** add a "Mitigation policy" rule block to
  `prompts/mode-security-audit.txt` (and its `_templates` source) and to
  `prompts/mode-judge-security-pass-exhaustion.txt`: recommendations must be
  automated, deterministic controls; a human gate is admissible only when the
  recommendation starts with `HUMAN GATE REQUIRED:` and names the narrowest
  trigger condition. The consolidated fix-issue body composed in
  `scripts/orchestrate_poll_process.sh` repeats the rule for the implementer.
- **Alternatives considered:** post-filtering findings in
  `scripts/security_audit.sh`; leaving the prompt alone and relying on the
  planner's §20 knowledge.
- **Why:** the user asked for the prompts to prevent such gates from being
  added again. Issue #4017's recommendation ("require … independently
  authenticated human approval before merging") went straight into the
  fix-issue body and the implementer followed it literally. The planner never
  saw a rule against it because `unattended_system_instructions.md` §20 talks
  about scripts and supervisors, not about mitigations.

## Context

- PR #4079 (`ai/issue-4075` → `orchestrator/project-3965`) exhausted its
  autofix iterations; the review-blocked judge decided `merge` and, under the
  branch's gate, posted a `REVIEW_BLOCKED_APPROVAL_V1` request at
  2026-09-12T16:19:36Z. No approval was posted. The review sweep
  re-dispatched the review every 30 minutes; each run reused the pending
  request (`Reusing pending review-blocked approval request; skipping a new
  model decision.`), re-posted the same decision comment (22 copies), and
  `main`'s `review_autofix.yml` sent a CRITICAL Telegram alert per run (24
  alerts). The poller's standalone stall ladder kept choosing
  `retrigger_review` for issue #4075 (`[standalone-stall] Issue #4075 stuck
  in 'ai:done' for 2107m (attempt 1). Action: retrigger_review`).
- The two symptoms are being fixed ahead of this plan: the alert arm is
  ported to `main` in the PR that carries this plan, and the duplicate
  decision comment is fixed on the integration branch (`review_rb_judge:
  post the decision comment once per approval request`). Neither changes the
  gate's policy.
- Gate mechanics on the integration branch: `scripts/gh_helpers.sh`
  (`review_blocked_decision_digest`, `review_blocked_find_pending_request`,
  `review_blocked_build_approval_request`, `review_blocked_approval_status`,
  `review_blocked_post_approval_request`,
  `review_blocked_post_consumed_marker`), `scripts/review_rb_judge.sh`
  (pending lookup, model-skip reuse, terminal approval block ahead of
  `# Execute judge action`), the poller's review-blocked rung in
  `scripts/orchestrate_poll_process.sh` (same sequence, plus a CRITICAL
  `tg_notify` on request creation), the `approval_pending` arm in
  `.github/workflows/review_autofix.yml`, and the documentation in
  `README.md` (`ENABLE_REVIEW_BLOCKED_JUDGE` row, item 9 of the orchestrator
  walkthrough), `agents.md` (review-blocked decisions paragraph), and
  `docs/how-it-works.md`.
- Binding rules: CLAUDE.md §18 / `unattended_system_instructions.md` §20
  (automation bias), §6 (every existing identifier stays: the
  `/review-blocked-approve` command, the `REVIEW_BLOCKED_APPROVAL_V1` and
  `REVIEW_BLOCKED_APPROVAL_CONSUMED_V1` markers, `judge_action=approval_pending`,
  `judge_skip_reason=approval_request_created`, all helper names), §14
  (scripts, prompts, and `review_autofix.yml` reach consumers on the next
  `@stable` sync), §15 (no new per-item API calls), §20 (one changelog
  fragment per phase PR).

## Goals

- With `REVIEW_BLOCKED_APPROVAL_MODE=trust-scoped` (default), a `merge` or
  `merge_with_followup` decision on a same-repo PR authored and commented on
  only by trusted principals, whose diff touches no protected path, executes
  through the existing deterministic gates (open state, head unchanged,
  mergeable, check-runs) with no human comment.
- Every condition in D3 forces the existing approval request; `close_and_reissue`
  always does. `REVIEW_BLOCKED_APPROVAL_MODE=always` reproduces today's
  behaviour exactly.
- A pending request puts `ai:awaiting-human-approval` on the PR and its
  linked issue(s); while present, the stall ladder does not fire
  `retrigger_review` or `dispatch_rb_judge` for that issue, the sweep does not
  dispatch the review, and exactly one Telegram alert, carrying the exact
  approval command, is sent.
- The security-audit prompt and the exhaustion-judge prompt carry the
  mitigation policy; the assembled templates stay byte-identical to the
  legacy prompts (`tests/test_assemble_prompt.py`); the fix-issue body carries
  the implementer-facing rule.
- Every new identifier is unique on `main` and on `orchestrator/project-3965`
  (verified at planning time: `ai:awaiting-human-approval`,
  `REVIEW_BLOCKED_APPROVAL_MODE`, `REVIEW_BLOCKED_APPROVAL_PROTECTED_PATHS`,
  `review_blocked_approval_required`, `REVIEW_BLOCKED_APPROVAL_POLICY`,
  `HUMAN GATE REQUIRED:` have zero matches on either).

## Non-goals

- Removing the approval mechanism, its markers, or the
  `/review-blocked-approve` command (user rejected `Q4: C`).
- Changing the `fix` action, the deterministic merge gates, the
  `close_and_reissue` baseline-preservation logic, or the merge-train gate.
- Changing the security pass's scope, cycle budget, waiver commands, or the
  findings-JSON schema.
- Adding a new orchestrator phase or changing stall thresholds.
- Consumer-repo variable rollouts beyond the documented defaults.

## Constraints

- §6: no rename or removal. New env vars get defaults (§4). New names listed
  under Goals are the only new identifiers.
- §14: `scripts/`, `prompts/`, `review_autofix.yml`, and `label_helpers.sh`
  propagate to every repo in `.github/ai/consumer_repos.json` on the next
  `@stable` tag; `review_autofix_sweep.yml` is internal to this repository.
- §15: the policy helper reads only payloads the judge already holds
  (`PR_META_JSON`, `PR_COMMENTS`, `PR_REVIEW_COMMENTS`, the diff in
  `RB_JUDGE_PR_DIFF_FILE`); the sweep and poller read labels from payloads
  they already fetch. No new `gh api` call is added in any phase.
- §20: one `changelog.d/` fragment per phase PR.
- Security: trust is identity-based (`author_association`, `user.type`,
  head-repo equality) and path-based; no free-text content influences the
  policy. The approval matcher's exact-body check, the `maintain`/`admin`
  role lookup, and the SHA/digest binding are unchanged.

## Approach

Three independent phases. Phase 1 is prompt-only and can merge first or last.
Phase 2 adds the label and the wait-state plumbing without touching the
policy, so a repository on `always` also benefits. Phase 3 adds the policy
helper and consults it from both judge paths; with Phase 2 absent it still
works, it just alerts and re-runs as today for the (now rarer) pending
requests.

## Phases & Merge Strategy

1. **Security-audit mitigation policy in prompts and fix-issue body.**
   Files: `prompts/mode-security-audit.txt`,
   `prompts/_templates/mode-security-audit.txt`,
   `prompts/mode-judge-security-pass-exhaustion.txt`,
   `prompts/_templates/mode-judge-security-pass-exhaustion.txt`,
   `scripts/orchestrate_poll_process.sh` (fix-issue body composer and the
   advisory follow-up body), `tests/test_security_audit_prompt_policy.py`
   [new], `README.md` (security-pass item 12e), `changelog.d/`.
   Done when: both prompts contain the rule block verbatim, the fix-issue
   body carries the implementer rule, `tests/test_assemble_prompt.py` and the
   new test pass. Rollback: revert the PR; no state involved.
2. **`ai:awaiting-human-approval` state, single alert, ladder and sweep
   awareness.** Files: `scripts/label_helpers.sh`, `scripts/gh_helpers.sh`
   (apply/remove helpers beside the request helpers),
   `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh`
   (review-blocked rung, standalone and managed stall paths, candidate
   details cache), `.github/workflows/review_autofix_sweep.yml`,
   `.github/workflows/review_autofix.yml` (alert text carries the command),
   tests, `README.md`, `agents.md`, `changelog.d/`. Done when: creating a
   request applies the label, consuming/invalidating removes it, a labelled
   issue is skipped by the stall ladder with a `STALL_SKIP … reason=awaiting_human_approval`
   line, a labelled PR is skipped by the sweep with
   `AUTOFIX_SWEEP_SKIP … reason=awaiting_human_approval`, and the Telegram
   alert on `approval_request_created` includes the exact command. Rollback:
   revert the PR; stray labels are removed by the next judge run's consumed
   path or by hand-free label cleanup in the poller's merged-issue sweep.
3. **Trust-scoped approval policy.** Files: `scripts/gh_helpers.sh`
   (`review_blocked_approval_required`), `scripts/review_rb_judge.sh`,
   `scripts/orchestrate_poll_process.sh` (review-blocked rung),
   `.github/workflows/review_autofix.yml` and
   `.github/workflows/orchestrate_poll.yml` (export
   `REVIEW_BLOCKED_APPROVAL_MODE` and
   `REVIEW_BLOCKED_APPROVAL_PROTECTED_PATHS` from `vars` with defaults),
   `workflow-templates/ai-review.yml` and `workflow-templates/ai-orchestrate-poll.yml`
   if they enumerate forwarded vars, tests, `README.md`, `agents.md`,
   `docs/how-it-works.md`, `changelog.d/`. Done when: the policy matrix
   tests pass, a trusted same-repo `merge` decision merges without a comment
   in a dry run of the judge against fixture JSON, `always` reproduces the
   current path, and every log line `REVIEW_BLOCKED_APPROVAL_POLICY` names
   the mode and reasons. Rollback: set `REVIEW_BLOCKED_APPROVAL_MODE=always`
   as a repo variable (instant, no deploy), then revert the PR.

No phase assumes another has merged. Phase 2 and Phase 3 touch the same
files in different blocks; whichever merges second rebases trivially.

## Implementation Steps

### Phase 1

1. `prompts/mode-security-audit.txt` and `prompts/_templates/mode-security-audit.txt`
   — after the existing `Rules:` list, add:

   ```
   Mitigation policy:
   - Recommendations must preserve unattended operation. Do not recommend a human approval step, an operator-run command, a manual sign-off, or any control whose normal path needs a person as the mitigation for a finding.
   - Recommend deterministic, automated controls: identity- and provenance-scoped authorization (who authored or commented, fork versus same-repository head, protected paths), fail-closed validation, least-privilege tokens, sandboxing, and treating author-controlled text as data rather than instructions.
   - Only when no automated control can mitigate the finding may the recommendation involve a person. Such a recommendation must start with `HUMAN GATE REQUIRED:` and name the narrowest trigger condition under which a person is consulted, so the planner scopes the gate to that condition and not to the whole workflow.
   ```

   Keep the two files byte-identical outside the include lines so
   `test_assembled_templates_match_legacy_prompt_bytes` passes.
2. `prompts/mode-judge-security-pass-exhaustion.txt` and its template — add
   the same three bullets under the judge's decision rules so an
   `accept_with_followup` follow-up issue never recommends a human gate.
3. `scripts/orchestrate_poll_process.sh` — in the consolidated fix-issue body
   composer (the block that renders the `| ID | Category | Severity |` table)
   and in the advisory follow-up body (the `### Recommendation` block), add a
   `### Mitigation policy` paragraph: "Implement each mitigation as an
   automated, deterministic control (unattended_system_instructions.md §20).
   A human approval step is in scope only where the finding's recommendation
   starts with `HUMAN GATE REQUIRED:`; otherwise implement an automated
   equivalent and say so in the PR body."
4. `tests/test_security_audit_prompt_policy.py` [new] — pin the three bullets
   in both prompts and their templates, and pin the fix-issue paragraph in
   the poller via the same text-anchor style as
   `tests/test_security_audit_workflow_contract.py`.
5. `README.md` item 12e — one sentence on the mitigation policy.
   `changelog.d/<issue>-security-audit-mitigation-policy.md`, section
   `changed`.

### Phase 2

6. `scripts/label_helpers.sh` — add `ai:awaiting-human-approval` to
   `_AI_LABEL_COLORS` (`fbca04`) and `_AI_LABEL_DESCS` ("Review-blocked
   judge decision awaiting trusted human approval"); do not add it to
   `_AI_PHASE_LABELS`.
7. `scripts/gh_helpers.sh` — add `review_blocked_mark_awaiting_approval
   <repo> <pr> <issue_numbers_json>` and
   `review_blocked_clear_awaiting_approval <repo> <pr> <issue_numbers_json>`
   beside `review_blocked_post_approval_request`; both call
   `ensure_label_exists` and are fail-open with a `::warning::`.
8. `scripts/review_rb_judge.sh` — call the mark helper immediately after a
   successful `review_blocked_post_approval_request`; call the clear helper
   after `review_blocked_post_consumed_marker` and on the head-changed and
   refusal paths.
9. `scripts/orchestrate_poll_process.sh` review-blocked rung — same two
   calls at the same points; in the `tg_notify` on request creation, append
   the exact `/review-blocked-approve <request_id> <digest>` line.
10. `scripts/orchestrate_poll_process.sh` stall paths — before
    `recovery_action_for_phase` is applied in the managed and standalone
    stall loops, skip issues whose cached labels include
    `ai:awaiting-human-approval`, logging
    `STALL_SKIP issue=<n> reason=awaiting_human_approval phase=<phase>` and
    leaving the recovery count unchanged. Labels come from
    `_candidate_details_json`; no new call.
11. `.github/workflows/review_autofix_sweep.yml` — add `labels: [.labels[].name]`
    to the PR projection and skip labelled PRs with
    `AUTOFIX_SWEEP_SKIP pr=#<n> reason=awaiting_human_approval`; count them
    in a new `skipped_awaiting_approval` counter in `AUTOFIX_SWEEP_END`.
12. `.github/workflows/review_autofix.yml` `Telegram review-blocked judge
    decision` step — in the `approval_request_created` branch, read the
    request id and digest from `steps.rb_judge.outputs` (add
    `approval_request_id` and `approval_decision_digest` outputs in step 8)
    and append the command line to `MSG`.
13. Tests: extend `tests/test_review_rb_judge_label_propagation.py` for the
    mark/clear calls, `tests/test_orchestrate_poll_process.py` for the stall
    skip, a new `tests/test_review_autofix_sweep_awaiting_approval.py` for
    the sweep skip, and `tests/test_review_rb_judge_self_run_exclusion.py`
    for the alert text.
14. `README.md` (label table and item 9), `agents.md` (review-blocked
    paragraph), `changelog.d/<issue>-awaiting-human-approval-label.md`,
    section `added`.

### Phase 3

15. `scripts/gh_helpers.sh` — add
    `review_blocked_approval_required <mode> <action> <pr_meta_json> <comments_json> <review_comments_json> <diff_file> <protected_paths>`
    printing `{"required":true|false,"reasons":[...]}`. Reasons are fixed
    strings: `mode_always`, `action_close_and_reissue`, `fork_head`,
    `untrusted_author`, `untrusted_commenter:<login>`,
    `protected_path:<path>`. Diff paths are parsed from `diff --git a/… b/…`
    headers of the already-fetched diff. Unknown mode or unparseable input
    returns `required=true` with reason `policy_input_invalid` (fail closed).
16. `scripts/review_rb_judge.sh` — in the terminal block ahead of
    `# Execute judge action`, before building or reusing a request, call the
    helper; log `REVIEW_BLOCKED_APPROVAL_POLICY pr=<n> mode=<mode> action=<action> required=<bool> reasons=<csv>`;
    when `required=false`, skip the request/approval block entirely and fall
    through to the existing execute path. A pending request found for the
    head is still honoured when present (it was created under a stricter
    evaluation), so consumed markers stay consistent.
17. `scripts/orchestrate_poll_process.sh` review-blocked rung — same call and
    log line, same fall-through.
18. `.github/workflows/review_autofix.yml` and `orchestrate_poll.yml` — export
    `REVIEW_BLOCKED_APPROVAL_MODE: ${{ vars.REVIEW_BLOCKED_APPROVAL_MODE || 'trust-scoped' }}`
    and `REVIEW_BLOCKED_APPROVAL_PROTECTED_PATHS: ${{ vars.REVIEW_BLOCKED_APPROVAL_PROTECTED_PATHS || '.github/ .claude/ scripts/ prompts/ workflow-templates/ CLAUDE.md agents.md unattended_system_instructions.md' }}`
    on the judge step and the poll step; mirror in the consumer wrappers if
    they forward vars explicitly.
19. Tests: `tests/test_review_blocked_approval_policy.py` [new] runs the
    helper via `bash -c 'source scripts/gh_helpers.sh; …'` against fixture
    JSON for every reason and for `always`; extend
    `tests/test_review_rb_judge_label_propagation.py` and
    `tests/test_orchestrate_poll_process.py` to pin the call sites and the
    log line.
20. `README.md` (`ENABLE_REVIEW_BLOCKED_JUDGE` row, new rows for the two
    variables, item 9), `agents.md`, `docs/how-it-works.md`,
    `changelog.d/<issue>-trust-scoped-review-blocked-approval.md`, section
    `changed`.

## Files & Modules

- `prompts/mode-security-audit.txt`, `prompts/_templates/mode-security-audit.txt`
- `prompts/mode-judge-security-pass-exhaustion.txt`,
  `prompts/_templates/mode-judge-security-pass-exhaustion.txt`
- `scripts/orchestrate_poll_process.sh`
- `scripts/gh_helpers.sh`
- `scripts/review_rb_judge.sh`
- `scripts/label_helpers.sh`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/orchestrate_poll.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `workflow-templates/ai-review.yml`, `workflow-templates/ai-orchestrate-poll.yml`
  (only if they forward vars explicitly)
- `tests/test_security_audit_prompt_policy.py` [new]
- `tests/test_review_autofix_sweep_awaiting_approval.py` [new]
- `tests/test_review_blocked_approval_policy.py` [new]
- `tests/test_review_rb_judge_label_propagation.py`,
  `tests/test_orchestrate_poll_process.py`,
  `tests/test_review_rb_judge_self_run_exclusion.py`
- `README.md`, `agents.md`, `docs/how-it-works.md`
- `changelog.d/` (one fragment per phase) [new]

## Tests

- Unit: policy helper matrix (each D3 condition, `always`, invalid input),
  label mark/clear helpers against a stubbed `gh`.
- Contract (text-anchored, the repository's norm): prompt bullets present
  in both prompt and template; assemble parity; call sites in the judge and
  poller; sweep skip; stall skip; alert text.
- Integration (existing harness in `tests/test_review_rb_judge_label_propagation.py`
  that runs extracted script blocks with a fake `gh`): a trusted `merge`
  decision reaches the execute path with no request posted; a protected-path
  diff posts a request and applies the label.
- Manual verification after Phase 3 merges: the next review-blocked PR
  authored by the pipeline merges without a comment, and the tracking issue
  shows the `REVIEW_BLOCKED_APPROVAL_POLICY` line in the run log.

## Risks & Mitigations

- **A trusted commenter pastes injected text.** Mitigation: trust is the
  same boundary the pipeline already uses for issue bodies; protected paths
  still force approval for automation code; `always` remains available.
- **Sync conflicts if implemented while #3965 is open.** Mitigation: D1;
  the orchestrator project is started only after #3965 is `ai:merged`.
- **Stale `ai:awaiting-human-approval` labels after a manual merge.**
  Mitigation: the poller's merged-issue sweep removes it alongside the other
  `ai:*` labels; the judge's pending lookup ignores labels and only trusts
  markers, so a stale label can never grant approval.
- **Consumer repos on `always` see no change; on the default they see fewer
  human requests.** ACCEPTED — that is the intended behaviour and is
  documented in the changelog fragment.
- **Prompt change reduces audit strictness.** ACCEPTED — findings are
  unchanged; only the shape of recommendations is constrained.

## Rollout

- Phase 1 takes effect on the next security audit; no flag.
- Phase 2 is additive; no flag. Labels are created on first use via
  `ensure_label_exists`.
- Phase 3 is an instant cutover to `trust-scoped` on merge. Rollback is the
  repo variable `REVIEW_BLOCKED_APPROVAL_MODE=always`. Consumer propagation
  follows the next `@stable` tag (§14); consumer wrappers need no edit
  unless they forward vars explicitly.

## References

- Issue #4017 (security-pass fix cycle 1, finding `prompt-injection-authorizes-pr-merge`),
  PR #4029, commit `3963708` on `orchestrator/project-3965`
- Issue #4075, PR #4079, review run 34721550146, sweep run 34734416297,
  poller run 34733104046
- `docs/plans/security-pass-convergence-plan.md`
- CLAUDE.md §4, §6, §14, §15, §18, §20; `unattended_system_instructions.md` §20
