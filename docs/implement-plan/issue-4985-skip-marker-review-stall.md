# Implement-Plan Log — Honour only an intentional skip-AI marker, log every gate skip, and hand stalled claude/* reviews to a fixer

- Plan: docs/plans/issue-4985-skip-marker-review-stall-plan.md
- Source issue: shubhodeep1/coding-workflows#4985 (https://github.com/shubhodeep1/coding-workflows/issues/4985)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4985-skip-marker-review-stall   Final PR: #5031 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR 1 (branch `claude/implement-plan-issue-4985-skip-marker-review-stall-conformance-fix-1`; number in the conformance 1/3 report)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_011mS4g4dN4yMqg1hT4BY67M (reused; the conformance 1/3 stage session session_01NqLcDV7uCAMDAichM1En9x arms it with a new safety net and hand-back, ids in its report)
- Last updated: 2026-09-29
- Last note: conformance 1/3: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS). One EVIDENCE-BASED concern: plan step 4's `pr_title` / `pr_body` input-description update never landed (`review_autofix.yml:16,21`). Fixed in the conformance fix PR with a contract test; no other defect found.

## Phases
1. [x] Phase 1 — intentional-marker rule, gate skip log and comment, review-stall detection and fixer re-dispatch   — PR #5056 merged 2026-09-29 (by the operator, issue Q1: A, as `00aac09`; twin sync 0e9976d); review rounds: 2; interventions: 0; protected paths: `.claude/scripts/check_in_status.py`, `.claude/scripts/dispatch_workflow.py`, `.claude/settings.json`, `.claude/hooks/gh_api_write_guard.py`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/implement-plan-claude.md` (all edited through their `workflow-templates/.claude/` twins)
   - gate: marker rule, `AUTOFIX_GATE_SKIP` line on every skip, one `claude/*` skip comment per head (`.github/workflows/review_autofix.yml`)
   - sweeps: same rule in `.github/workflows/review_autofix_sweep.yml` and `scripts/claude_pr_sweep.py`; `review-stalled` is due; `CLAUDE_REVIEW_STALL_HOURS` in the catch-all env
   - checker twin: `has_skip_ai_marker`, `review-stalled` → `hand_back_fixer`, `stall_redispatched`
   - fixer twin: `review-stalled` → claim `review`, re-dispatch `internal-review.yml` / `ai-review.yml`; dispatch helper and settings twins allow `internal-review.yml`
   - tests: `tests/test_skip_ai_marker_rule.py` [new], hand-back, sweep, dispatch parity
   - docs: CLAUDE.md (+ template), README.md, agents.md, `changelog.d/4985-skip-marker-review-stall.md`
   - Done: new and updated tests pass against the twins; ruff and yamllint clean; `review_autofix.yml` < 480,000 bytes; stage stops BLOCKED for `[claude-twin-sync]`

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Implemented COMPLETE; Correctness CONCERNS: 1 EVIDENCE-BASED concern, the `pr_title` / `pr_body` input descriptions of plan step 4) — conformance fix PR 1 (pre-security)

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How does phase 1 change `.claude/**`? — Picked: A — twin-first per Q40, as the issue says. Alternatives: B — edit `.claude/**` directly. Why: the operator's standing rule and the issue text. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What exactly counts as an intentional body marker? — Picked: A — outside ``` / ~~~ fences, a line matching `^ {0,3}\[skip ai\][ \t]*$`; the title keeps substring matching. Alternatives: B — also list items / headings; C — title only. Why: the issue's "a body line that consists of the marker alone". Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Do the two sweeps adopt the same rule? — Picked: A — yes, parity-tested. Alternatives: B — gate only. Why: the §26.H catch-all skipped #4807 too. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Add `ai:review-skipped` to a marker-skipped PR? — Picked: A — no; the comment marker is the signal. Alternatives: B — add it. Why: the label contract is the deterministic doc/size skip. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Comment on drafts? — Picked: A — no; drafts get the log line and are never stalled. Alternatives: B — comment on drafts too. Why: the draft final PR would collect one comment per phase merge. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Where do the skip log and comment live? — Picked: A — inline at the end of the gate step. Alternatives: B — a new step script. Why: the gate job has no support checkout; the file stays about 20 KB under the 480,000-byte guard. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What proves a head was reviewed or deliberately not reviewed? — Picked: A — hand-off for the head, `auto_merge`, gate-skip comment, marker, draft, `ai:merge-queued`, active run. Alternatives: B — also check-run names. Why: no new read types. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] What starts the stall clock? — Picked: A — the head commit's committer date, `CLAUDE_REVIEW_STALL_HOURS` default 2. Alternatives: B — PR `updated_at`. Why: `updated_at` moves on every comment. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-29] Which claim kind does `review-stalled` use? — Picked: A — `review`. Alternatives: B — a new `stall` kind. Why: no claim or queue payload change. Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-29] How are repeated re-dispatches bounded? — Picked: A — `stall_redispatched`; the fixer holds instead of dispatching again. Alternatives: B — no guard. Why: a traceless skip would loop every lease period. Applied in: phase 1. Status: pending review
- AD-11 [plan, 2026-09-29] Which workflow does the fixer re-dispatch? — Picked: A — `internal-review.yml` here (allowlisted in the twins), `ai-review.yml` in consumers. Alternatives: B — `review_autofix.yml`. Why: only `internal-review.yml` runs are bound to the PR by title. Applied in: phase 1. Status: pending review
- AD-12 [plan, 2026-09-29] Does plain `--pr` mode also report stalls? — Picked: A — no, hand-back mode only. Alternatives: B — plain mode too. Why: §5; the catch-all covers implement-plan PRs. Applied in: phase 1. Status: pending review
- AD-13 [plan, 2026-09-29] Log every skip even with an earlier `AUTOFIX_GATE_SKIP` line? — Picked: A — yes, one uniform end-of-gate line. Alternatives: B — only when none was logged. Why: one searchable line per run. Applied in: phase 1. Status: pending review

## Lessons
- [source:intervention] In Claude-fixer mode, a review round whose findings are all rejected converges only through the dedicated verdict bot (`CLAUDE_FIXER_VERDICT_BOT_LOGIN` plus its private credentials). Without them the phase stops BLOCKED until an operator merges the PR, so provision the bot before relying on unattended convergence. (files: .github/workflows/review_autofix.yml)
- [source:plan-deviation] A stop with no PR in flight leaves the committed progress log at `Status: IN_PROGRESS`. A resuming session must check the issue's `ai:claude-blocked` comment and the checker's pending triggers, not the log's `Status:` alone, before it concludes a chain is still running. (files: .claude/commands/implement-issue-claude.md)
- [source:conformance] Plan steps that only change wording (workflow input descriptions, helper docstrings) are the easiest to drop. Pin them with a contract assertion in the same phase. (files: .github/workflows/review_autofix.yml, tests/test_skip_ai_marker_rule.py)
- [source:intervention] A jq filter that matches a whole comment line (`split("\n")` + an anchored `test`) must strip `\r` first (`gsub("\r"; "")`): comments edited in the GitHub web UI are stored with CRLF, and the Python (`splitlines`) and awk parsers of the same marker already ignore it. (files: .github/workflows/review_autofix.yml)
- [source:plan-deviation] Adding a workflow to `dispatch_workflow.py`'s allowlist also means adding it to the `.claude/settings.json` allow rules and to `gh_api_write_guard.py`'s `DISPATCHABLE_WORKFLOWS`; tests keep all three equal, and the settings and hook changes need the operator's approval window. (files: .claude/scripts/dispatch_workflow.py, .claude/settings.json, .claude/hooks/gh_api_write_guard.py)

## Notes
- 2026-09-29, review round 2 (head b763c69): all 12 findings rejected. Convergence needs the dedicated verdict bot (`CLAUDE_FIXER_VERDICT_BOT_LOGIN` is empty), so the stage stopped BLOCKED on the issue (comment 5891294075) with a hold claim. The operator answered Q1: A and merged #5056 into the project branch by hand as `00aac09` (comment 5894925076). The BLOCKED status lived only on the issue because no PR was in flight, so this log still read IN_PROGRESS.
- 2026-09-29, resume: the `/reclarify` (comment 5898555437) queued the issue, and the pickup started `/implement-issue-claude` in session_01NqLcDV7uCAMDAichM1En9x. The checker was idle with no enabled trigger, so the session resumed instead of stopping as already in progress. It ran as stage `conformance 1/3`, and synced `main` into the project branch as `0277e2e` (clean merge; the 16 project suites: 1012 passed, 1 skipped).
- Protected-path approval: phase 1 — twin-first per Q40 (issue #4985 body, owner-authored: "In `check_in_status.py` (edit the `workflow-templates/.claude/**` twin first, Q40)", 2026-09-29). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy; the `settings.json` twin needs the operator's approval window (Q62/Q64).
- The session started before the repository was cloned, so the SessionStart hook had not run; `bash .claude/hooks/session-start.sh` installed `gh` (2.101.0).
- Issue progress comment: 5883835523.
- 2026-09-29: phase 1 PR #5056 (head of the implementation commit `f992868`, then this log commit). Verification: `ruff check --select E,F --ignore E501` clean on changed Python; `yamllint -s` clean; actionlint and shellcheck findings unchanged before/after (only line numbers moved); `review_autofix.yml` 460,048 bytes.
- Protected-path approval: phase 1 — Q1: A (2026-09-29), all six twins synced by the master session as `[claude-twin-sync]` 0e9976d (settings.json and the hook in the operator's approval window, Q62/Q64); summary comment 5887995914. The master's wake (trigger `trig_01LpkipHer3chPx5osbdjWfP`) stood in for `/reclarify`.
- Every `check_in_status.py` call and checker prompt passes `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (#5057).
