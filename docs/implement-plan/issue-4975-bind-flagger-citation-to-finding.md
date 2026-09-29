# Implement-Plan Log — Bind the flagger's consensus_id citation to its own structured finding

- Plan: docs/plans/issue-4975-bind-flagger-citation-to-finding-plan.md
- Source issue: shubhodeep1/coding-workflows#4975
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4975-bind-flagger-citation-to-finding   Final PR: #5026 draft
- Status: IN_PROGRESS
- Stage: conformance 2/3 — review round
- Activation: not started
- Waiting on: PR #5116 (conformance fix 2, review round 2)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_014rdmhD1aoaX4meJcY3BWPS (project checker, reused)
- Last updated: 2026-09-29
- Last note: PR #5116 review round 1: kept `_same_file`'s suffix match (AD-11: a `docs/README.md` over-match only keeps an entry blocking, narrowing it would let `b/README.md` escape), corrected its docstring and the README / agents.md / changelog wording, and pinned the boundary and the `&` list separator in tests; rejected the `&` finding (AD-10 behaviour, prose after a reference does not widen). Earlier: conformance run 2 INCOMPLETE (Step 4 FAIL: a second flagger finding escaped `ambiguous_flagger_nearby` when its `File:` path was decorated or absolute, or its lines were written `N to M` / `N, M`); conformance fix PR #5116 opened (AD-9, AD-10). Before the run the project branch took the issue base (which had merged the issue-4976 project) as `[claude-merge-resolve]` a22f301. Earlier: PR #5060 merged 2026-09-29 as 453b439.

## Phases
1. [x] Phase 1 — bind the flagger citation to a structured finding record (scripts/review_claude_fixer_nonblocking.py, scripts/review_run_reviewers.sh header sentence, tests, README.md, agents.md, changelog fragment)   — PR #5032 merged 2026-09-29; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-29: INCOMPLETE (Step 4 FAIL, 3 EVIDENCE-BASED findings in `flagger_finding_records()`) — fix PR #5060 merged 2026-09-29 as 453b439; review rounds: 2 (round 2: both findings rejected, does not reproduce / no defect; head held with no verdict bot, merged by the master session under its Q46) (pre-security)
- Run 2 — 2026-09-29: INCOMPLETE (Step 4 FAIL, 2 EVIDENCE-BASED BLOCKERs in `flagger_finding_records()`: decorated / absolute `File:` paths read as another file, `N to M` / `N, M` / `N-M.` read only line N; both let a second same-line flagger finding escape `ambiguous_flagger_nearby`) — fix PR #5116 (waiting; review rounds: 1) (pre-security)

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
- AD-9 [conformance 2/3, 2026-09-29] Which spellings of a record's `File:` path name the entry's file? — Picked: A — drop wrapping markup, quotes, angle and link brackets and a trailing `.` / `;`; for the second-finding check only, also count a path that ends with `/` plus the entry's path (or the reverse). Alternatives: B — treat any decorated path as unreadable, leaving absolute paths escaping; C — no change. Why: fails toward blocking (§1) in the ambiguity check while the citation stays bound to an exact file. Applied in: PR #5116. Status: pending review
- AD-10 [conformance 2/3, 2026-09-29] How are multi-line references read? — Picked: A — `N to M` / `N through M` is a range, a list right after the first reference (`, N` / `and N` / `& N`) widens the span, and a closing sentence period no longer drops a range. Alternatives: B — treat any multi-number value as unreadable; C — no change. Why: a record never reads narrower than the lines it names; B would also stop well-formed citations from demoting. Applied in: PR #5116. Status: pending review
- AD-11 [conformance 2/3 — review round, 2026-09-29] Should the second-finding check stop counting a relative path with extra leading directories (`docs/README.md`) as the entry's file (`README.md`)? — Picked: A — keep the suffix match: any `/`-prefixed spelling counts (absolute runner path, repo or workspace prefix, a `b/` diff prefix), and a different file sharing the suffix only keeps the entry blocking; document it and pin both cases in tests. Alternatives: B — count the suffix only when the longer path is absolute; C — absolute paths plus a fixed prefix list (`a/`, `b/`, repo name). Why: B and C let a prefixed spelling of the entry's own file escape `ambiguous_flagger_nearby` (fail-open, §1), while A only costs a demotion in the rare two-files-same-suffix-same-lines case. Applied in: PR #5116. Status: pending review

## Lessons
- [source:plan-deviation] Tightening how a reviewer's raw output is parsed breaks test fixtures that model that output loosely; search every test that writes review_<slug>.txt (tests/test_review_autofix_claude_fixer_mode.py as well as the script's own suite) before changing the parser. (files: tests/test_review_autofix_claude_fixer_mode.py, scripts/review_claude_fixer_nonblocking.py)
- [source:conformance] A parser that reads locations from free-form reviewer output must fail toward "unreadable": take a line only from an explicit reference (`path:N`, `line N`, a leading number), never the first digit in the value, and treat prose where a path belongs as no path, because a wrong readable location silently defeats proximity-based safety checks. (files: scripts/review_claude_fixer_nonblocking.py)
- [source:conformance] When a safety check compares locations parsed from free-form output, normalize every spelling of the same location (decorated or absolute paths, `N to M` ranges, line lists) before comparing, and let the comparison over-match where a match keeps the safe outcome; an exact-string compare silently turns a variant spelling into "somewhere else". (files: scripts/review_claude_fixer_nonblocking.py)
- [source:intervention] A deliberate over-match in a safety check must say so in its docstring and be pinned by a test that exercises the over-matching case (not a fixture that sidesteps it, like `docs/README.md.bak`), or every review round re-flags it as a defect. (files: scripts/review_claude_fixer_nonblocking.py, tests/test_review_claude_fixer_nonblocking.py)

## Notes
- Issue mode: plan written by /implement-issue-claude from #4975; security pass skipped per plan header.
- The session had neither `gh` nor the `mcp__github__*` tools at start (repo cloned after SessionStart); the repo's SessionStart hook was run by hand to install `gh`, and GitHub writes go through `gh api` REST.
- Conformance run 1: `tests/test_review_issue_ledger.py`, `tests/test_review_parse_consolidator.py`, `tests/test_review_pipeline_integration.py`, and `tests/test_review_reject_verify.py` fail in the stage container because `gawk` is not installed; they fail identically without the fix and do not touch the filter.
- Conformance run 2: the issue base had merged the issue-4976 project (source-grounded rejection votes, #5027); the stage sync conflicted in the parser constants and the `REJECTING_REVIEWS` fixture, resolved keeping both sides (a22f301). The four gawk-dependent review suites pass once `gawk` is installed (975 passed).
