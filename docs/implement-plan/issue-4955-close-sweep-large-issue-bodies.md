# Implement-Plan Log — Stream the close sweep's issue lists to jq instead of passing them as arguments

- Plan: docs/completed/issue-4955-close-sweep-large-issue-bodies-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#4955
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4955-close-sweep-large-issue-bodies   Final PR: #4997 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges)
- Waiting on: completion PR (branch claude/implement-plan-issue-4955-close-sweep-large-issue-bodies-complete)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_013iwU8rwpq7ivQWQuB3r3bp
- Last updated: 2026-09-29
- Last note: conformance run 1 CONFORMANT (no fixes); validation skipped by Q1: A (standing decision Q17: A); completion PR opened.

## Phases
1. [x] Phase 1 — stream the close sweep's queue build through stdin and surface its failures (orchestrate_poll_process.sh + tests + README + changelog)   — PR #5007 merged 2026-09-29 (9a9e2c0); review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). Every acceptance criterion traces to the merged code. Checks: both new tests pass and fail on the pre-fix script; all 19 sweep tests pass; tests/test_linked_pr_implementation_guard.py 18/18, tests/test_issue_pr_status_target_branch_gate.py, tests/test_workflow_file_size_limit.py, and tests/test_changelog_fragment_contract.py pass; `bash -n` is clean.

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified).

## Validation
- Skipped (covered by #4813's project validation). validate.yml binds `target_ref` only to a final PR into the default branch, and final PR #4997 targets claude/implement-plan-issue-4813-close-sweep-target-branch-merges. Blocked 2026-09-29 before dispatch; Q1 asked at https://github.com/shubhodeep1/coding-workflows/issues/4955#issuecomment-5884142095 and answered Q1: A by the owner (standing decision Q17: A) at https://github.com/shubhodeep1/coding-workflows/issues/4955#issuecomment-5884307107. Long-term fix: #4734.

## Completion
- Completion PR (branch claude/implement-plan-issue-4955-close-sweep-large-issue-bodies-complete) — doc moved to docs/completed/issue-4955-close-sweep-large-issue-bodies-plan.md
- Final PR #4997 draft (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges)

## Activation
- n/a: the issue base is the #4813 project branch, so this change goes live with #4813's lifecycle (Issue Mode).

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the dedup queue receive the two issue lists? — Picked: A — stream both lists to one `jq -n` on stdin (`printf '%s\n' … | jq -n '[inputs] as $lists | …'`). Alternatives: B — write each list to a temp file and pass `--slurpfile`; C — drop `body` from `gh issue list` and fetch each issue's body separately. Why: smallest change with no temp files and no argument limit; C adds one API call per issue (§15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What should the sweep do when the queue cannot be built? — Picked: A — log `::warning::CLOSE_MERGED_SWEEP queue_build_failed merged_bytes=<n> ready_bytes=<n> — skipping this cycle.` with the first 300 bytes of `jq`'s stderr, and return 0. Alternatives: B — return non-zero; C — process whichever list still parses. Why: the poller runs under `set -e` and calls the sweep bare, so B aborts the poll cycle; C mixes label-origin policies. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should the `gh issue list` fetch fallbacks (`|| echo "[]"`) also report failures? — Picked: A — no, leave them unchanged. Alternatives: B — add a warning on fetch failure too. Why: not triggerable from issue content, fail-open before #4813 (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Should other large `--argjson` / `--arg` call sites in the poller change too? — Picked: A — no; listed under the plan's Non-goals. Alternatives: B — convert every large `jq` argument in the file to stdin. Why: they predate #4813, hold no list of issue bodies, and are outside this finding (§5). Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] When a shell script needs to move a JSON payload that can hold user-controlled text (issue or PR bodies) into jq or another command, stream it on stdin, never as an `--arg` / `--argjson` / `argv` value: Linux caps one argument at 128 KiB, and a failed exec behind a `|| echo "[]"` fallback silently empties the work queue. (files: scripts/orchestrate_poll_process.sh)
- [source:plan-deviation] An issue-mode project whose base is another project's branch cannot run runtime validation, because validate.yml authorizes `target_ref` only through an open final PR into the default branch; its plan should state up front that the parent project's validation covers it. (files: .github/workflows/validate.yml)

## Notes
- Security pass skipped per the plan header: `security_pass_skip.py` verified #4955 as an automation-produced `ai:security` follow-up (`ai:security: created and labelled by the issue automation`).
- Issue base's own final PR #4826 (into `main`) was open (draft) at project start; the base has not merged.
- Progress comment on #4955: id 5883203945.
- Plan deviation (phase 1): the jq filter keeps `def normalize` first and reads `[inputs] as $lists` with an exactly-two-documents check instead of `input as $merged | input as $ready`, so an extra document in a payload fails loudly; the unparseable-list test uses the existing `mock_store_extra` hook (`mock_gh_issue_list_raw_by_label`, scoped to the sweep's `--json number,labels,body` listing) instead of a new `_run_poller` parameter. The plan was updated to match.
- PR #5007 merged 2026-09-29T04:20:07Z (merge commit 9a9e2c0) with no review rounds.
- Conformance 1/3 stage (2026-09-29, session_017hunhJwEyh3BygMnLEKa6z): CONFORMANT; validation could not be dispatched (see ## Validation). Blocked comment https://github.com/shubhodeep1/coding-workflows/issues/4955#issuecomment-5884142095 and `ai:claude-blocked` added.
- Resumed 2026-09-29 by `/reclarify` (queue item #5048) in session_01KmcJMdd2RQfzHQRaZbmFAp (auto mode): `ai:claude-blocked` removed; the issue base had not merged and the project branch was already in sync with it; project checker session_013iwU8rwpq7ivQWQuB3r3bp reused. No `mcp__github__*` tools in this session: GitHub writes go through the REST API.
