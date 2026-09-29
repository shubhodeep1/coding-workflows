# Implement-Plan Log — Bind the flagger's consensus_id citation to its own structured finding

- Plan: docs/plans/issue-4975-bind-flagger-citation-to-finding-plan.md
- Source issue: shubhodeep1/coding-workflows#4975
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4975-bind-flagger-citation-to-finding   Final PR: #5026 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3 — review round
- Activation: not started
- Waiting on: PR #5060 (conformance fix 1, review round 2)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_014rdmhD1aoaX4meJcY3BWPS (project checker, reused)
- Last updated: 2026-09-29
- Last note: PR #5060 review round 1: fixed the `path:N` reader so a version (`3.14:40`) or a URL's `host:port` reads no line; rejected the `:40` leading-colon finding (editor line shorthand, intended). Earlier: conformance run 1 INCOMPLETE (Step 4 FAIL: the record parser read code-text digits as lines, missed numbered/heading File: lines, and read prose File: values as paths, so a second same-line flagger finding escaped ambiguous_flagger_nearby); conformance fix PR opened (AD-6..AD-8).

## Phases
1. [x] Phase 1 — bind the flagger citation to a structured finding record (scripts/review_claude_fixer_nonblocking.py, scripts/review_run_reviewers.sh header sentence, tests, README.md, agents.md, changelog fragment)   — PR #5032 merged 2026-09-29; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-29: INCOMPLETE (Step 4 FAIL, 3 EVIDENCE-BASED findings in `flagger_finding_records()`) — fix PR #5060 (waiting; review rounds: 1) (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue; `.claude/scripts/security_pass_skip.py` printed `"skip": true`).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What counts as the flagger's "specific structured finding"? — Picked: A — a `File:` record in the flagger's raw pass-2 output, outside fences, ending at the first blank line, next `File:` / `Requirement:` / `REJECTED_FINDING` line, heading, or fence; only a whole `consensus_id: <id>` line inside it counts. Alternatives: B — any standalone `consensus_id:` line outside fences; C — a model-graded same-defect judgement. Why: the smallest deterministic binding to one finding. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to verify the citing record describes the same defect? — Picked: A — exactly one citing record, same file as the pass-1 and pass-2 entries, a line reference overlapping both ranges; an unreadable file or line binds nothing. Alternatives: B — A plus text similarity of the problem lines; C — file match only. Why: location is the only field both passes carry verbatim. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] When is a same-line flagger finding ambiguous? — Picked: A — any other flagger record in the same file within 3 lines of either range, or in the same file (or an unreadable file) with no readable line, keeps the entry blocking. Alternatives: B — only an overlapping record; C — no flagger-side ambiguity check. Why: matches `LINE_TOLERANCE` and fails toward blocking (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Reason keys for the new cases? — Picked: A — keep `flagger_did_not_cite`, add `flagger_citation_mismatch` and `ambiguous_flagger_nearby`. Alternatives: B — fold the new cases into existing keys. Why: §6 keeps existing log keys' meaning. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Tell reviewers where the `consensus_id:` line goes? — Picked: A — add one sentence to the cross-pollination header. Alternatives: B — no prompt change. Why: well-behaved reviewers keep demotion; minimal change. Applied in: phase 1 PR. Status: pending review
- AD-6 [conformance 1/3, 2026-09-29] Which values of a finding record's line field count as a line reference? — Picked: A — only an explicit reference: a leading number or range, `path:N[-M]` (path with `.` or `/`), `line N` / `lines N-M`, or `LN`; code text reads no line, which keeps the entry blocking. Alternatives: B — strip inline code spans, then take any number; C — keep taking the first number anywhere. Why: `Line or code reference:` carries code text by design (the runner prompt's own example is code), B still misreads unquoted code, and A fails toward blocking (§1). Applied in: conformance fix PR. Status: pending review
- AD-7 [conformance 1/3, 2026-09-29] Which `File:` line shapes start a finding record? — Picked: A — plain, `-` / `*` bulleted, numbered (`1.` / `1)`), and Markdown-heading (`### File:`) lines. Alternatives: B — add numbered items only; C — keep plain and bulleted only. Why: a second finding in a numbered or heading item was invisible to the ambiguity check; more records only keep more entries blocking. Applied in: conformance fix PR. Status: pending review
- AD-8 [conformance 1/3, 2026-09-29] When does a `File:` value name a readable path? — Picked: A — when its leading token contains `.` or `/`, or is the whole value (optionally `:N`); prose such as `the install example, line 1261` names no file, which keeps a nearby entry blocking. Alternatives: B — any leading token (as before); C — require `.` or `/` always. Why: B let a prose-described second finding escape the check; C would also drop `File: Makefile:12`. Extensionless files followed by prose lose demotion, the fail-toward-blocking direction. Applied in: conformance fix PR. Status: pending review

## Lessons
- [source:plan-deviation] Tightening how a reviewer's raw output is parsed breaks test fixtures that model that output loosely; search every test that writes review_<slug>.txt (tests/test_review_autofix_claude_fixer_mode.py as well as the script's own suite) before changing the parser. (files: tests/test_review_autofix_claude_fixer_mode.py, scripts/review_claude_fixer_nonblocking.py)
- [source:conformance] A parser that reads locations from free-form reviewer output must fail toward "unreadable": take a line only from an explicit reference (`path:N`, `line N`, a leading number), never the first digit in the value, and treat prose where a path belongs as no path, because a wrong readable location silently defeats proximity-based safety checks. (files: scripts/review_claude_fixer_nonblocking.py)

## Notes
- Issue mode: plan written by /implement-issue-claude from #4975; security pass skipped per plan header.
- The session had neither `gh` nor the `mcp__github__*` tools at start (repo cloned after SessionStart); the repo's SessionStart hook was run by hand to install `gh`, and GitHub writes go through `gh api` REST.
- Conformance run 1: `tests/test_review_issue_ledger.py`, `tests/test_review_parse_consolidator.py`, `tests/test_review_pipeline_integration.py`, and `tests/test_review_reject_verify.py` fail in the stage container because `gawk` is not installed; they fail identically without the fix and do not touch the filter.
