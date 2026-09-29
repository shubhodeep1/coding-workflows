# Start clarify for a /reclarify on any line of a trusted comment

Source issue: shubhodeep1/coding-workflows#5243 (https://github.com/shubhodeep1/coding-workflows/issues/5243)
Base branch: main
Security pass: run

## Summary

The clarify intake starts only when a trusted comment's body **starts** with `/reclarify`, so an answer that puts `/reclarify` on a later line is ignored silently and the blocked project stalls. This plan makes the job-level gate accept `/reclarify` at the start of **any** line of a trusted, marker-free comment. It keeps every existing gate, adds a matching step-level gate as a second check, and marks the one automation comment that ends with a bare `/reclarify` line so the automation cannot trigger itself.

## Context

- `.github/workflows/clarify.yml:21`, `.github/workflows/internal-clarify.yml:17` and `workflow-templates/ai-clarify.yml:17` (the consumer wrapper, synced to the 13 repos in `.github/ai/consumer_repos.json`) all gate the comment route on `startsWith(github.event.comment.body, '/reclarify')`. The README's copy-paste example (`README.md:363`) repeats it, and `tests/test_phase_wrapper_predicate_contract.py` requires all four to match.
- The step-level check in `Decide clarify route` (`clarify.yml:431`, `grep -q '^/reclarify'`) is already line-anchored, but it only sets `is_forced_reclarify` and never decides whether the run proceeds.
- Incident (2026-09-29): 13 answered issue-mode projects (#5127, #5125, #5124, #5114, #5094, #5093, #4985, #4975, #4952, #4927, #4891, #4886, #4867) got an answer with `/reclarify` on its last line. Every run was `skipped`, `ai:claude-blocked` stayed, and the projects stalled 3–14 h. The #5127 answer (comment 5894236387) is the shape to accept: answer text, then a `/reclarify` line, then a `---` footer, with no `<!-- ai:` marker.
- Every automation comment is posted as the same `User` account (`shubhodeep1`, `OWNER`) through `GH_PAT`, so association and user type cannot tell a human from the automation. Automation comments carry `<!-- ai:…` markers (`ai:claude-blocked:v1`, `ai:claude-issue-progress:v1`, `ai:claude-issue-routed:v1`, `ai:clarification-questions`, …). The retire-master plan (`docs/plans/retire-master-session-plan.md`, risk at line 297) uses the same rule: a comment carrying any `<!-- ai:` marker is never a human answer.
- One automation comment ends with a bare `/reclarify` line and has **no** marker: `plan.yml` "Post implementation plan" (lines 1981–1996) prints "To restart clarification reply:" followed by `/reclarify`. With a line-anchored gate it would start clarify on every plan post. The two stall-recovery comments in `scripts/orchestrate_poll_process.sh` (lines 13467, 16277) start with `/reclarify` on purpose and keep working unchanged.
- The Claude intake authorizes an untrusted-author issue only when a trusted `User` commented `/reclarify` (`scripts/claude_issue_route.py` `has_trusted_reclarify`, line 332, `body.startswith`). Without the same widening, an answer ending in `/reclarify` on an outside-author issue would route to Claude and then be refused at intake.
- Detection of answered-but-unrouted blockers (the issue's fix direction 2) is the retire-master project's phase 2: `claude_blocked_sweep.py` in the hourly pickup resumes an `ai:claude-blocked` issue whose trusted, marker-free answer is newer than its blocker, with or without `/reclarify` (goals G3/G3b). See AD-5.

## Goals

- A trusted `User` issue comment (not on a PR) whose body has `/reclarify` at the start of any line, and that carries no `<!-- ai:` marker, starts the clarify job in this repo and in consumer repos.
- A comment that starts with `/reclarify` behaves exactly as today, marker or not (stall recovery).
- These never start the job: an inline mention (`please /reclarify`, `` `/reclarify` ``), an indented `/reclarify`, a comment with a `<!-- ai:` marker whose `/reclarify` line is not the first, and the `plan.yml` implementation-plan comment.
- A run the job-level gate lets through for a comment that is not a `/reclarify` command does nothing: the route step skips with `AI_PHASE_GATE_V1 phase=clarify gate=route reason=not_reclarify_command outcome=skip`, so no label, `/answer`, Codex run, or Claude handoff happens.
- `has_trusted_reclarify` accepts the same comment shape as the gate, so the Claude intake authorizes what clarify routes.
- Every existing gate stays: `action == 'created'`, `pull_request == null`, `user.type == 'User'`, the `OWNER`/`MEMBER`/`COLLABORATOR` association, and the opened-issue route.

## Non-goals

- Detection of answered-but-unrouted blocked issues in the poller or the pickup (issue fix direction 2). It belongs to the retire-master plan's phase 2 sweep, which also resumes such issues instead of only flagging them (AD-5).
- `/reclarify` on a closed issue (#5222).
- The `/answer` and `/approved` predicates of `plan.yml` and `implement.yml`.
- Leading whitespace before `/reclarify` (the existing step check is column-0 only).
- `docs/operations/master-session.md` (the master session's own tracking doc).

## Constraints

- §6: no identifier is renamed or removed. The predicate keeps its `startsWith(…, '/reclarify')` clause; new names (`IS_RECLARIFY_COMMAND`, `is_reclarify_command`, `AUTOMATION_MARKER_PREFIX`, the `ai:implementation-plan:v1` marker) are checked for collisions first.
- §12 security posture and §1: the widening must not let automation trigger clarify. The marker exclusion is case-insensitive, and the step gate repeats the job gate.
- §14 / consumer sync: `workflow-templates/ai-clarify.yml` must stay identical to the reusable predicate (the parity test). Consumer wrappers reach consumers on the `@stable` sync. The old and new wrapper/reusable pairs are safe in any order, because each filter only narrows.
- §15: no new GitHub API call.
- §20: one changelog fragment (`fixed`).
- §27: `clarify.yml` (78 KB) and `plan.yml` (114 KB) stay far under 480,000 bytes.
- §9: YAML stays 2-space. Python in `scripts/` uses tabs.

## Approach

1. **Job-level predicate**, in all four copies: replace `startsWith(github.event.comment.body, '/reclarify')` with
   `(startsWith(github.event.comment.body, '/reclarify') || (contains(github.event.comment.body, fromJson('"\n/reclarify"')) && !contains(github.event.comment.body, '<!-- ai:')))`.
   `fromJson('"\n/reclarify"')` decodes to a newline followed by `/reclarify`: the expression language has no escape sequences or regex, and a folded YAML scalar would turn a literal newline into a space. A body with `\r\n` line endings still contains `\n/reclarify`. `startsWith` and `contains` are case-insensitive, as today.
2. **Step-level gate** in `Decide clarify route`: compute `IS_RECLARIFY_COMMAND` with the same rule (bash, lowercased body). An `issue_comment` run where it is false sets `SKIP_CODEX=true` with the `not_reclarify_command` telemetry line and never takes the orchestrator fast path. `is_forced_reclarify` is unchanged.
3. **Plan comment marker**: `plan.yml` appends `<!-- ai:implementation-plan:v1 -->` as the last line of the implementation-plan comment. `implement.yml` finds the plan by its `Implementation Plan` heading, which is unchanged.
4. **Intake mirror**: `claude_issue_route.py` gains `is_reclarify_command(body)`, which returns true when the body starts with `/reclarify`, or when a later line does and the body has no `<!-- ai:` marker. `has_trusted_reclarify` uses it. The command match stays case-sensitive, as `has_trusted_reclarify` is today.

Alternative considered: `contains(body, '/reclarify')` at job level with the step check deciding (the issue's example). Rejected because every automation comment that mentions "comment `/reclarify`" would then start a runner and a skipped run.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the predicate, the step gate, the plan-comment marker and the intake mirror have to ship together. For example, a widened gate without the plan marker would let plan comments trigger clarify.

1. **Phase 1: line-anchored `/reclarify` intake with self-trigger guards.**
   - Files: see Files & Modules.
   - Done when: the four predicates match and carry the new clause; the step gate skips an inline mention, a marker comment and the plan comment, and passes a trailing `/reclarify` line; `has_trusted_reclarify` agrees; the named test suites pass; README, agents.md and the changelog fragment are updated.
   - Rollback: revert the phase PR. The old predicate is stricter, so a partial revert (wrapper or reusable only) is still safe.

## Implementation Steps

1. `.github/workflows/clarify.yml` line 21, `.github/workflows/internal-clarify.yml` line 17, `workflow-templates/ai-clarify.yml` line 17, `README.md` line 363: the new predicate clause (Approach 1).
2. `.github/workflows/clarify.yml` `Decide clarify route` (lines 420–518): add `IS_RECLARIFY_COMMAND`, the `not_reclarify_command` skip branch after `issue_closed`, the fast-path guard, and `reclarify_command=` in the `clarify_route` notice.
3. `.github/workflows/plan.yml` `Post implementation plan` (lines 1981–1996): append the `<!-- ai:implementation-plan:v1 -->` line after `/reclarify`.
4. `scripts/claude_issue_route.py`: `AUTOMATION_MARKER_PREFIX`, `is_reclarify_command`, and use it in `has_trusted_reclarify`.
5. Tests (see Tests).
6. Docs: the README trusted-comment paragraph (lines 334–345) and the intake paragraph (line 1246), the agents.md intake bullet (line 242), `docs/how-it-works.md` line 85, and `changelog.d/5243-reclarify-any-line.md`.

## Files & Modules

- `.github/workflows/clarify.yml`
- `.github/workflows/internal-clarify.yml`
- `workflow-templates/ai-clarify.yml`
- `.github/workflows/plan.yml`
- `scripts/claude_issue_route.py`
- `tests/test_phase_wrapper_predicate_contract.py`
- `tests/test_phase_skip_gate_telemetry_contract.py`
- `tests/test_claude_issue_route.py`
- `README.md`, `agents.md`, `docs/how-it-works.md`
- `changelog.d/5243-reclarify-any-line.md` [new]

## Tests

- Unit, `tests/test_phase_wrapper_predicate_contract.py`: the parity test covers the four copies. A new test pins the exact comment clause, checks that `fromJson('"\n/reclarify"')` decodes to a newline plus `/reclarify`, and runs a small evaluator of the clause over these bodies:
  - a trailing `/reclarify` line → starts;
  - `\r\n` endings → starts;
  - `/reclarify` first → starts;
  - `/Reclarify` first → starts;
  - an inline mention → skipped;
  - a backticked mention → skipped;
  - an indented `/reclarify` → skipped;
  - an `ai:claude-blocked:v1` comment with a `/reclarify` line → skipped;
  - the plan comment → skipped;
  - the #5127 answer → starts.
- Unit, `tests/test_phase_skip_gate_telemetry_contract.py`: runs the real `Decide clarify route` step body with the same bodies. It asserts `skip_codex`, `orchestrator_fast_path=false` for a non-command comment on an orchestrator-managed issue, and the `not_reclarify_command` line. It also asserts that the plan step writes the marker after the `/reclarify` line.
- Unit, `tests/test_claude_issue_route.py`: `is_reclarify_command` and `has_trusted_reclarify` accept a trailing line and reject an inline mention, a marker comment and an indented line. The existing refusal cases still hold.
- Run: `python3 tests/test_phase_wrapper_predicate_contract.py`, `python3 tests/test_phase_skip_gate_telemetry_contract.py`, `pytest tests/test_claude_issue_route.py`, the `ci.yml` workflow-lint steps that cover `clarify.yml` (actionlint when available), and `tests/test_update_workflows_guardrails.py` / `tests/test_changelog_fragment_contract.py`.
- End to end (after merge, observed): the next answered blocker whose answer ends with `/reclarify` gets `ai:claude-issue-routed` within minutes.

## Risks & Mitigations

- **Automation comment with a later `/reclarify` line and no marker.** A background sweep of every issue-comment poster (recorded in `## Notes`) found only the plan comment, which this plan marks. The step gate repeats the rule. ACCEPTED for model text inside a marker-free automation comment: such text would need a column-0 `/reclarify` line, and the effect is one extra clarify run.
- **A human answer that quotes an automation marker.** Such a comment is ignored, as it is today. ACCEPTED: the human can post `/reclarify` first.
- **Consumer wrapper and reusable out of step during the `@stable` sync.** Each is a filter that only narrows, so any mix is at worst today's behaviour.
- **Wider `has_trusted_reclarify`.** The comment author must still be a trusted `User`, and marker comments are excluded, so no new principal can vouch.

## Rollout

Ships to this repo on merge (`internal-clarify.yml` calls `clarify.yml@main`). It reaches consumers on the next `@stable` release plus the wrapper sync (`update_workflows.yml`). No flag, and no new env var. Rollback is a revert.

## References

- #5243 (this issue), #5222 (`/reclarify` on closed issues), #4620 (intake authorization), `docs/plans/retire-master-session-plan.md` (phase 2 blocked-issue sweep).

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the job-level gate detect `/reclarify` on a later line? — Picked: A — `startsWith(…) || (contains(body, fromJson('"\n/reclarify"')) && !contains(body, '<!-- ai:'))`, line-anchored at job level. Alternatives: B — `contains(body, '/reclarify')` with the step check deciding (the issue's example); C — leave the job gate, require `/reclarify` first and document it. Why: A starts no runner for inline mentions, and C does not fix the incident. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Should the marker exclusion also apply when `/reclarify` is the first line? — Picked: A — no, a first-line `/reclarify` behaves exactly as today. Alternatives: B — exclude marker comments everywhere. Why: backward compatibility for stall recovery and any automation that starts a comment with `/reclarify` (§1 order, §5); a marker comment starts with its marker, so it cannot start with `/reclarify`. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How should the `plan.yml` implementation-plan comment (which ends with a bare `/reclarify` line) be kept from self-triggering? — Picked: A — append an invisible `<!-- ai:implementation-plan:v1 -->` marker as its last line. Alternatives: B — backtick the `/reclarify` line (a visible text change); C — special-case the `Implementation Plan` heading in the gate. Why: A leaves the visible text and the `implement.yml` heading lookup unchanged, and it follows the marker convention. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should the route step also gate (repeat the rule), or only report? — Picked: A — gate: a non-command comment skips with `reason=not_reclarify_command`. Alternatives: B — job gate only. Why: defence in depth, as the issue asks; a later wrapper change cannot turn an inline mention into a clarify run or an orchestrator auto-`/answer`. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Build the answered-but-unrouted detection (issue fix direction 2) here? — Picked: A — no: leave it to the retire-master plan's phase 2 `claude_blocked_sweep.py`, which also resumes answered blockers without `/reclarify`, and record it under Non-goals. Alternatives: B — add a read-only check to `claude-issue-queue-watchdog.yml` now; C — add it to the pickup command (a protected path). Why: B and C would compete with the planned sweep in the same files (§5, "extend, never compete"), and this plan's intake fix removes the cause. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] Should `has_trusted_reclarify` match `/reclarify` case-insensitively like the workflow expression? — Picked: A — keep it case-sensitive (unchanged for the first line) and add the line rule. Alternatives: B — case-insensitive, to mirror the expression exactly. Why: the authorization check stays at least as strict as today (§1). A differently-cased command already behaves this way. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`: the security pass runs.
