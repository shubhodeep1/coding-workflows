# Keep automation text from resuming a blocked issue through a later-line /reclarify

Source issue: shubhodeep1/coding-workflows#5309 (https://github.com/shubhodeep1/coding-workflows/issues/5309)
Base branch: claude/implement-plan-issue-5243-reclarify-any-line
Security pass: skip (ai:security: automation-produced issue)

## Summary

Project #5243 lets a trusted comment resume a blocked issue when `/reclarify` starts any line of the comment. It excludes comments that carry an `<!-- ai:` marker, and it only applies on issues labelled `ai:claude-blocked`, `ai:claude-handoff-failed` or `ai:blocked`. The security audit (#5309, high) found a gap. Automation posts as the same trusted `User` as a human, and the orchestrator's clarify escalation adds `ai:blocked` and then posts a comment that embeds model text with no marker. If that text has a line starting with `/reclarify`, the gate treats it as a human command. The run then skips the pause for human input and resumes Codex clarification.

This plan closes the gate in layers. Escalation comments get markers, the later-line form fails closed on orchestrator issues and on any HTML-comment marker, the route step ignores `/reclarify` lines inside fenced code blocks, and a switch to Codex clears the stale Claude "waiting" labels.

## Context

- The gate lives in four copies: `.github/workflows/clarify.yml:21`, `.github/workflows/internal-clarify.yml:17`, `workflow-templates/ai-clarify.yml:17`, and the README example (`README.md:369`). `tests/test_phase_wrapper_predicate_contract.py` requires all four to match. The later-line clause is `contains(body, fromJson('"\n/reclarify"')) && !contains(body, '<!-- ai:') && <one of the three labels>`.
- The route step `Decide clarify route` (`clarify.yml:435–454`) repeats the rule in bash as `IS_RECLARIFY_COMMAND`. A non-command comment skips with `AI_PHASE_GATE_V1 … reason=not_reclarify_command`. `IS_FORCED_RECLARIFY` (`clarify.yml:431`) greps `^/reclarify` on any line. So on an `ai:orchestrator-managed` issue, a later-line command also counts as forced, and it resumes Codex clarify instead of taking the orchestrator fast path.
- **The reported path.** `scripts/orchestrate_parse_and_post_answer.sh:172–216` adds `ai:blocked`, then posts the loop-break comment. The `ESCALATE` variant copies `ESCALATION_SECTION` (model output) verbatim, and neither variant carries a marker.
- **Other comments that set a trigger label, with no marker.** `plan.yml` "Handle blocked planning output" (`plan.yml:1405–1440`, "Planning blocked: human input required.") and `implement.yml` (`implement.yml:5332–5354`, "Implementation blocked: human input required."). Both embed a one-line model reason. The Claude-side comments (`ai:claude-blocked:v1`, `ai:claude-handoff-failed:v1`) are already marked.
- **Tracking issues.** `orchestrate_poll_process.sh` sets `ai:blocked` on tracking issues (`set_tracking_phase_label`, lines 11970 and 22682). `post_tracking_comment` (line 2206) adds no marker, and tracking issues get judge, stall-judge and validation comments with model text. The comment route of the gate does not exclude `ai:orchestrator-tracking` (only the opened-issue route does).
- **Markers without the `ai:` prefix.** Heal intake comments carry `<!-- workflow-failure-heal:occurrence -->` / `…:outcome -->` (`scripts/workflow_failure_heal.py:38`, `MARKER_PREFIX`). Heal issues can be routed to Claude and blocked.
- **Fenced untrusted text.** `.claude/scripts/permission_prompts.py:361` posts "Seen again." comments on `ai:permission-prompt` issues (Claude-routed, so they can carry `ai:claude-blocked`). The comments have no marker, and the latest command example sits in a ```` ````text ```` fence. A multi-line command can put `/reclarify` at column 0.
- **Stale labels after a switch to Codex.** "Release Claude claim on switch to Codex" (`clarify.yml:545–556`) removes only `ai:claude`. `ai:claude-blocked` / `ai:claude-handoff-failed` stay on the issue while the Codex pipeline posts unmarked model text on it (the `plan.yml` "Clarification required" comment, `plan.yml:1836–1856`).
- `plan.yml:864` finds clarification comments by `<!-- ai:clarification-questions -->` or the `^Clarification required` prefix. A marker appended at the end of the comment leaves the prefix match unchanged. Nothing else parses the three escalation bodies (`grep`: only `tests/test_implement_blocked_verdict_terminalizes.py:109` checks a substring).

## Goals

- No automation comment can resume a blocked issue through a later-line `/reclarify`, in this repo or in consumer repos. This covers orchestrator escalations, tracking-issue comments, marked heal or Claude comments, and fenced model text.
- A human's answer that ends with a `/reclarify` line still resumes an `ai:claude-blocked`, `ai:claude-handoff-failed` or `ai:blocked` standalone issue (the #5243 goal).
- A comment that starts with `/reclarify` behaves exactly as today on every issue, including stall recovery (#5243 AD-2).
- Every comment that adds `ai:blocked` carries an `<!-- ai:…:v1 -->` marker on its own last line, outside the model text.

## Non-goals

- Adding markers to every comment site that embeds model text (#5243 AD-7 option C). The scope and fence rules cover the sites that can reach a trigger-labelled standalone issue.
- Editing `.claude/scripts/permission_prompts.py` (a protected path). The fence rule covers it (AD-3).
- The `has_trusted_reclarify` vouch check in `scripts/claude_issue_route.py`: it accepts only a first-line `/reclarify` (#5243 AD-8).
- Changes to `IS_FORCED_RECLARIFY` or the orchestrator fast path. Both are reached only after the new gate.

## Constraints

- §1: security first. Every new rule narrows the gate, and none widens it.
- §6: no identifier is renamed or removed. New names are `<!-- ai:clarify-escalation:v1 -->`, `<!-- ai:plan-blocked:v1 -->`, `<!-- ai:implement-blocked:v1 -->`, `<!-- ai:clarification-required:v1 -->`, and the bash variables `RECLARIFY_FENCE_OPEN_RE`, `RECLARIFY_FENCE_CLOSE_RE`, `RECLARIFY_OPEN_FENCE`, `RECLARIFY_SCAN_LINE`, `RECLARIFY_LINE_NO`. They are checked for collisions before use.
- §14: the consumer wrapper `workflow-templates/ai-clarify.yml` stays identical to the reusable predicate (parity test). A new wrapper with an old reusable workflow, or the reverse, is safe, because each only narrows.
- §15: no new GitHub API call. The label removals reuse the release step's existing DELETE pattern, one call per label.
- §20: one changelog fragment (`security`). The unreleased `changelog.d/5243-reclarify-any-line.md` is corrected where it describes the marker rule.
- §27: `clarify.yml`, `plan.yml` and `implement.yml` stay far under 480,000 bytes.
- §9: YAML 2-space, shell scripts in `scripts/` use tabs.

## Approach

1. **Job-level predicate, all four copies.** In the later-line clause, replace `!contains(body, '<!-- ai:')` with `!contains(body, '<!--')`, and add `!contains(toJson(github.event.issue.labels.*.name), '"ai:orchestrator-tracking"') && !contains(toJson(github.event.issue.labels.*.name), '"ai:orchestrator-managed"')`. The first-line `startsWith` clause is unchanged.
2. **Route step `Decide clarify route`.** The later-line branch uses the same `<!--` test and the same label exclusions (jq). It also scans the lowercased body line by line and accepts a `/reclarify` line only when it is outside a fenced code block. A fence opens with 0–3 spaces and then 3+ backticks or tildes, and it closes on a line of the same character, at least as long, with nothing after it. The expression language cannot parse fences, so this check lives only in the step: such a comment starts a runner that then skips with `not_reclarify_command`.
3. **Markers on escalation comments.** Append the marker as the final line, after the model text: `orchestrate_parse_and_post_answer.sh` (both loop-break variants) → `<!-- ai:clarify-escalation:v1 -->`; `plan.yml` blocked comment → `<!-- ai:plan-blocked:v1 -->`; `implement.yml` blocked comment → `<!-- ai:implement-blocked:v1 -->`; `plan.yml` non-orchestrated "Clarification required" comment → `<!-- ai:clarification-required:v1 -->` (the orchestrated variant already starts with `ai:clarification-questions`).
4. **Release step.** `RELEASE_CLAUDE_CLAIM` also turns true when the issue carries `ai:claude-blocked` or `ai:claude-handoff-failed`, and the release step deletes those two labels as well as `ai:claude`.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the four predicate copies must stay identical (parity test), and the markers, route-step rules and label release together make one fail-closed gate.

1. **Phase 1: fail-closed later-line `/reclarify` gate.**
   - Files: see Files & Modules.
   - Done when: the four predicates match and carry the new clause; the route step skips orchestrator-managed and tracking later-line commands, `<!--` comments and fenced `/reclarify` lines, and still passes the #5127 answer shape; the four comment sites carry their markers; the release step drops the stale labels; the named suites pass; docs and changelog are updated.
   - Rollback: revert the phase PR. The #5243 gate comes back as it was.

## Implementation Steps

1. `.github/workflows/clarify.yml:21`, `.github/workflows/internal-clarify.yml:17`, `workflow-templates/ai-clarify.yml:17`, `README.md:369`: new later-line clause (Approach 1).
2. `.github/workflows/clarify.yml` `Decide clarify route`: `<!--`, the label exclusions and the fence scan in the `IS_RECLARIFY_COMMAND` branch, and an updated comment block. `RELEASE_CLAUDE_CLAIM` condition.
3. `.github/workflows/clarify.yml` "Release Claude claim on switch to Codex": delete `ai:claude-blocked` and `ai:claude-handoff-failed` too.
4. `scripts/orchestrate_parse_and_post_answer.sh:190–212`, `.github/workflows/plan.yml` (blocked comment, clarification-required comment), `.github/workflows/implement.yml` (blocked comment): markers.
5. Tests (see Tests).
6. Docs: `README.md` trusted-comment paragraph, `agents.md` intake bullet (around line 227), `docs/how-it-works.md:85`, `changelog.d/5243-reclarify-any-line.md` (the marker sentences), and `changelog.d/5309-reclarify-fail-closed.md` (new, `security`).

## Files & Modules

- `.github/workflows/clarify.yml`
- `.github/workflows/internal-clarify.yml`
- `workflow-templates/ai-clarify.yml`
- `.github/workflows/plan.yml`
- `.github/workflows/implement.yml`
- `scripts/orchestrate_parse_and_post_answer.sh`
- `tests/test_phase_wrapper_predicate_contract.py`
- `tests/test_phase_skip_gate_telemetry_contract.py`
- `tests/test_orchestrate_clarify_loop_guard.py`, `tests/test_plan_clarify_blocked_output.py`, `tests/test_implement_blocked_verdict_terminalizes.py` (marker assertions, where they read the posted body)
- `README.md`, `agents.md`, `docs/how-it-works.md`
- `changelog.d/5243-reclarify-any-line.md`, `changelog.d/5309-reclarify-fail-closed.md` [new]

## Tests

- `tests/test_phase_wrapper_predicate_contract.py`: update `RECLARIFY_COMMAND_CLAUSE` and the evaluator. New cases:
  - `ai:blocked` + `ai:orchestrator-managed`, trailing line → skipped;
  - `ai:blocked` + `ai:orchestrator-tracking`, trailing line → skipped;
  - the orchestrator escalation comment with model text holding a `/reclarify` line and the new marker → skipped;
  - the same text without the marker on an `ai:orchestrator-managed` issue → skipped (scope);
  - a heal occurrence comment (`<!-- workflow-failure-heal:occurrence -->`) with a `/reclarify` line → skipped.
  All #5243 cases keep their results.
- `tests/test_phase_skip_gate_telemetry_contract.py`: run the real step body over the same cases, plus the fence cases:
  - a ```` ````text ```` fence containing a ```` ``` ```` line and then `/reclarify` → skipped;
  - a `~~~` fence → skipped;
  - `/reclarify` after a closed fence → passes;
  - an unclosed fence → skipped.
  Assert `skip_codex=true`, `orchestrator_fast_path=false` and the `not_reclarify_command` line for orchestrator-managed later-line comments. Assert `release_claude_claim=true` for a Codex route on an issue with only `ai:claude-blocked`.
- Static checks that each escalation site writes its marker after its model text (orchestrate script, both `plan.yml` comments, `implement.yml`), and that the release step deletes all three labels.
- Run: `python3 -m pytest tests/test_phase_wrapper_predicate_contract.py tests/test_phase_skip_gate_telemetry_contract.py tests/test_orchestrate_clarify_loop_guard.py tests/test_plan_clarify_blocked_output.py tests/test_implement_blocked_verdict_terminalizes.py tests/test_claude_issue_route.py tests/test_changelog_fragment_contract.py tests/test_workflow_file_size_limit.py`, `bash -n` / `shellcheck` on the script, and `actionlint` on the three workflows when available.

## Risks & Mitigations

- **A human answer with an HTML comment, a fenced `/reclarify`, or on an orchestrator issue** is ignored. ACCEPTED: the human can put `/reclarify` first, and on orchestrator issues the escalation text asks for `/answer`.
- **A runner starts for a fenced `/reclarify` comment** and then skips it. ACCEPTED: rare, costs one short run, and has no side effects.
- **Unmarked model text outside a fence**, posted on a standalone issue that carries a trigger label from a site not covered here. The known sites are all covered (see Context). ACCEPTED as residual: this needs a column-0 `/reclarify` line in unfenced model text on a blocked issue, and the effect is one extra clarify run.
- **Wrapper/reusable version skew** during the `@stable` sync: each copy only narrows the gate.

## Rollout

Ships to this repo when the #5243 project's final PR merges (`internal-clarify.yml` calls `clarify.yml@main`). It reaches consumers on the next `@stable` release and wrapper sync. No flag and no new env var. Rollback is a revert.

## References

- #5309 (this finding), #5243 (the project this hardens, final PR #5266), #3576 (security audit tracker), `docs/plans/issue-5243-reclarify-any-line-plan.md`.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should the reported path (an orchestrator escalation adds `ai:blocked`, then posts unmarked model text) be closed? — Picked: A — both layers: an `<!-- ai:…:v1 -->` marker on every comment that adds `ai:blocked`, and a later-line form that never applies on `ai:orchestrator-managed` or `ai:orchestrator-tracking` issues, in the job predicate and the route step. Alternatives: B — markers on the escalation comments only (the finding's literal recommendation); C — revert the later-line form to first-line only. Why: B leaves every unmarked poller, judge and stall-judge comment on tracking and child issues open. On those issues the human path is `/answer` or a first-line `/reclarify`. C would undo #5243. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Which comments count as automation for the later-line form? — Picked: A — any comment containing `<!--`. Alternatives: B — keep `<!-- ai:` and add the `workflow-failure-heal:` prefix. Why: fails closed for heal markers and any future marker without a gate change. A human answer rarely contains an HTML comment (§1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How should fenced model text (the permission-prompt "Seen again" example) be kept from counting? — Picked: A — the route step ignores `/reclarify` lines inside fenced code blocks. Alternatives: B — add a marker to `.claude/scripts/permission_prompts.py` twin-first (a protected path, so the project stops for a twin sync); C — accept it as residual risk. Why: A covers every fenced untrusted excerpt with no protected-path edit (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Should a switch to Codex clear `ai:claude-blocked` / `ai:claude-handoff-failed`? — Picked: A — yes, in the existing release step. Alternatives: B — leave them. Why: stale labels keep the later-line form open while the Codex pipeline posts unmarked model text on the issue. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the non-orchestrated `plan.yml` "Clarification required" comment get a marker? — Picked: A — yes, `<!-- ai:clarification-required:v1 -->` as its last line. Alternatives: B — no. Why: it copies raw model output, it costs one line in a file this phase already edits, and the `^Clarification required` lookup is unchanged. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] Should the unreleased `changelog.d/5243-reclarify-any-line.md` be corrected? — Picked: A — yes, reword its marker sentences in place and add a separate `5309` fragment. Alternatives: B — leave it and describe the change only in the new fragment. Why: the 5243 fragment ships in the same release and would otherwise describe a rule that no longer holds (§12.B stale docs). Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Parent project: #5243, project branch `claude/implement-plan-issue-5243-reclarify-any-line`, final PR #5266 (open, draft). Its security pass waits for this issue to close with `ai:merged`.
