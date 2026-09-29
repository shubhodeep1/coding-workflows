# Implement-Plan Log — Require owner authorization before protected `.claude` changes merge or ship

- Plan: docs/plans/issue-4919-gate-protected-path-merges-plan.md
- Source issue: shubhodeep1/coding-workflows#4919
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs
- Project branch: claude/implement-plan-issue-4919-gate-protected-path-merges   Final PR: #4973 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR from `claude/implement-plan-issue-4919-gate-protected-path-merges-phase-1` (the PR that carries this line)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: see the stage report and the next `— resume.` block (checker, safety net, and hand-back ids are armed after this PR opens)
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (73 new tests; every existing suite touching the changed files passes; the 114 other failures of the full suite fail identically on the unchanged base); phase PR opened.

## Phases
1. [ ] Phase 1 — protected-path merge and release authorization gate (scripts/protected_path_authorization.py, scripts/protected_path_gate.sh, merge-site wiring in review_enable_auto_merge.sh / review_rb_judge.sh / orchestrate_poll_process.sh, stage_workflow_support.sh + orchestrate_poll.yml staging, release gate in test-and-mark-stable.yml / mark-stable.yml, tests, ci.yml, CLAUDE.md §23.I, agents.md, changelog)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What trusted provenance authorizes a protected-equivalent change? — Picked: A — an unedited PR comment whose whole body is `/authorize-protected-paths <head SHA>`, by a `User` with OWNER/MEMBER/COLLABORATOR association and no `performed_via_github_app`. Alternatives: B — an approving review from a non-author collaborator; C — an `ai:protected-path-approved` label; D — the progress log's `Protected-path approval:` line. Why: head-bound, per-PR, and a Claude session cannot produce it; B is impossible with one account, C is not head-bound, D is ruled out by the finding. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Where is the gate enforced at merge? — Picked: A — a shared `protected_path_guarded_merge` wrapper around every `gh pr merge` in `scripts/*.sh`, with a static test. Alternatives: B — a required status check plus branch protection; C — only `review_enable_auto_merge.sh`. Why: A covers review, judge, and orchestrator paths without an admin operation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Wrap `review_autofix.yml`'s deterministic-skip merge too? — Picked: A — no; `PROTECTED_SKIP_SUPPRESSED` already refuses the skip for `.claude/*`, `workflow-templates/*`, `scripts/*`. Alternatives: B — yes. Why: no added coverage for a near-limit workflow edit (§27). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Where is the gate enforced at release? — Picked: A — a `validate`-job step in `test-and-mark-stable.yml` and `mark-stable.yml`. Alternatives: B — also before the stable branch fast-forward; C — no release gate. Why: consumers read the tag the `release` job moves. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] How does the release gate treat pre-gate changes? — Picked: A — grandfather PRs merged at or before the gate script first landed on the candidate's first-parent history. Alternatives: B — no grandfathering. Why: B would block the first release on dozens of already-reviewed PRs. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Which paths are protected-equivalent for the gate? — Picked: A — `.claude/**`, `workflow-templates/.claude/**`, `tests/test_claude_template_parity.py`, and the gate's own two scripts. Alternatives: B — plus CLAUDE.md and workflows; C — only the two trees. Why: A covers the finding and protects the gate itself; B widens past §5. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How is a blocked merge surfaced? — Picked: A — one PR comment per head with marker `<!-- ai:protected-path-authorization:v1 head=<sha> -->`, no label. Alternatives: B — `ai:needs-human`; C — a new label. Why: a block label wakes Claude fixers that cannot authorize. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Gate consumer repos too? — Picked: A — yes, only for PRs touching the protected set. Alternatives: B — coding-workflows only. Why: a consumer's `.claude/**` also steers its unattended sessions. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] What happens on a read error? — Picked: A — retry three times, then refuse the merge for this attempt. Alternatives: B — fail open. Why: §1. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-29] How is an authorized PR bound to its head on an unbound merge call? — Picked: A — the wrapper adds `--match-head-commit <authorized head>`. Alternatives: B — leave it. Why: otherwise a push between check and merge merges an unapproved head. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] `orchestrate_poll_process.sh` runs from the copy `orchestrate_poll.yml` stages from a fixed script list, not from `stage_workflow_support.sh`; a new helper it sources must be added to that list too, or every orchestrator run fails at staging. (files: .github/workflows/orchestrate_poll.yml, scripts/stage_workflow_support.sh)
- [source:plan-deviation] A GitHub Actions step `name:` containing ` #` must be quoted: YAML reads the rest as a comment and silently truncates the name (yamllint reports it only as a comment-spacing warning). (files: .github/workflows/test-and-mark-stable.yml, .github/workflows/mark-stable.yml, .github/workflows/ci.yml)
- [source:plan-deviation] A gate wrapped around a shared merge path must leave the unaffected case byte-for-byte unchanged: requiring a well-formed head SHA for every PR broke 40 orchestrator tests whose fakes use placeholder SHAs, so the SHA is required only where it is used (a protected PR). (files: scripts/protected_path_authorization.py, tests/test_orchestrate_poll_process.py)

## Notes
- Issue mode: permission mode `auto` (recorded, not asked).
- Security pass skipped per `security_pass_skip.py`: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Base branch final PR #4684 is open (draft); the base has not moved (checked 2026-09-29).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4919#issuecomment-5882706456
- Phase 1 plan deviations (recorded in the plan and as lessons): both gate files were also added to `orchestrate_poll.yml`'s staging list; two existing tests whose fakes model the merge scripts were updated (`tests/test_review_autofix_review_pipeline_contract.py` serves the gate's files read and pins the gated call shape; `tests/test_review_rb_judge_label_propagation.py` sources the gate and serves an empty files list).
- Verification (2026-09-29): `tests/test_protected_path_authorization.py` 73 passed; the judge, review-pipeline, workflow-size, section-number, template-parity, and permission-prompt suites pass; full suite `-n 4`: 4740 passed, 156 failed, of which 40 were caused by this phase (fixed by requiring a head SHA only for protected PRs; all 40 then passed) and the other 114 failed identically on the unchanged base (environment: validation template renderers, node/mongo family runtimes, consolidator/reject-verify, `test_workflow_retro.py` import error). Mutation: unwrapping one orchestrator merge call fails `test_every_scripted_gh_pr_merge_is_wrapped`. `yamllint -s` and `actionlint` clean on the four touched workflows. Read-only smoke against the live API: `pr` mode allows #4973 (unprotected) and blocks #4783 (pin change, no owner comment); the wrapper passes #4973's merge call through unchanged.
- This session had no `mcp__github__*` tools; `gh` was installed by the repo's SessionStart hook, and GitHub writes go through `gh api` routine calls and the allowlisted helpers.
