# dispatch_workflow.py returns the run its own dispatch started

Source issue: shubhodeep1/coding-workflows#5016 (https://github.com/shubhodeep1/coding-workflows/issues/5016)
Base branch: main
Security pass: run

## Summary

`.claude/scripts/dispatch_workflow.py` picks "its" run by taking the newest new `workflow_dispatch` run, so two sessions that dispatch the same workflow within seconds can each record the other's run. This plan makes the helper take the run id GitHub returns for the dispatch itself, refuse to guess when it has to fall back to polling, and makes `/implement-plan-claude` steps 9–10 check a run's target ref before trusting its verdict.

## Context

- `find_new_run` (`.claude/scripts/dispatch_workflow.py:88-98`) returns `max(new_runs, key=id)` over every run that did not exist before the POST. With two parallel dispatches both sessions see both runs and each takes the newest.
- Observed 2026-09-29 (issue #5016): runs 36512923727 (`ref` = issue-4787's project branch, clean) and 36512928347 (`ref` = issue-4813's project branch, 3 follow-ups) were created 3 s apart; the issue-4787 project recorded 36512928347. The `security-pass 1/5 — read result` stage caught it only because it read `AUDIT_TARGET_REF_INPUT` from the run log.
- The worst case is a clean security or validation verdict applied to a project whose own audit opened follow-ups. `security-audit.yml` (`ref`) and `internal-validate.yml` / `ai-validate.yml` (`target_ref`) are exposed whenever projects run in parallel.
- GitHub's "Create a workflow dispatch event" endpoint accepts an optional boolean body field `return_run_details`; with it the response is `200 OK` with `workflow_run_id`, `run_url`, and `html_url` instead of `204 No Content` (GitHub changelog 2026-02-19, "Workflow dispatch API now returns run IDs"; REST docs for `POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`). Without the field the endpoint keeps returning 204.
- The helper and `/implement-plan-claude` have byte-identical twins under `workflow-templates/.claude/` (`tests/test_dispatch_workflow.py::test_template_parity`, and the `.claude/` sync to consumer repos).

## Goals

- G1. A dispatch whose POST response carries `workflow_run_id` returns exactly that run id, whatever other runs of the same workflow appear at the same time.
- G2. When the response carries no run id (a 204 or an unparseable body), the helper falls back to polling and never guesses: more than one new run is reported as `"ambiguous": true` with the candidate ids and exit 2, and a single new run is returned with `"matched_by": "new_run"` so the caller knows it is unverified.
- G3. Every exit-0 result says how the run was matched (`"matched_by": "dispatch_response" | "new_run"`); existing output fields (`dispatched`, `workflow`, `ref`, `run_id`, `html_url`, `status`, `created_at`, `error`) keep their names and meaning.
- G4. `/implement-plan-claude` steps 9 and 10 confirm, before reading a verdict, that the run audited / validated this project's branch (`AUDIT_TARGET_REF_INPUT` for the security audit, the validate run's `target_ref`), and never apply another project's verdict.

## Non-goals

- No change to any workflow file, `run-name`, or consumer wrapper (no correlation input): the dispatch response makes that unnecessary, and a new input would be rejected by consumer wrappers that predate it (`Unexpected inputs provided`).
- No change to `check_in_status.py`, the checker prompt, or the six-workflow allowlist.
- No change to how the review-convergence dispatch (step 7a) is used; it benefits from G1 automatically.

## Constraints

- §5 minimal change set: only the helper, its twin, its tests, the two command-file passages, and the docs that describe the helper.
- §6: no output field is renamed or removed; `matched_by`, `ambiguous`, and `candidate_run_ids` are new fields whose names do not clash with existing ones in the helper or `check_in_status.py`.
- §15: the success path issues no more calls than today (pre-dispatch list read, POST, one read of the returned run instead of ≥ 1 list polls); the fallback path issues the same calls as today.
- §23.I / §28.C: `.claude/**` is a protected path. Both files have `workflow-templates/.claude/**` twins, so the phase is marked `protected paths:` and runs only under a recorded `Protected-path approval:` line.
- §9 tabs in Python; §20 one changelog fragment; §7 README.md / agents.md describe the helper and must be updated.

## Approach

1. `_post_dispatch` adds `"return_run_details": true` to the JSON body and returns the parsed response when it is a JSON object (empty stdout on a 204 returns `None`). Failure classification (4xx → `dispatched: false`, anything else → `DispatchUnconfirmed`) is unchanged.
2. `dispatch` uses `workflow_run_id` when it is a positive int: one best-effort read of `repos/<repo>/actions/runs/<id>` fills `status` and `created_at` (a failed read leaves them `null`; the id from the response stays authoritative), `html_url` comes from the response (or the run read), and the result carries `"matched_by": "dispatch_response"`, exit 0.
3. Without a run id, the existing poll runs, but `find_new_run` is replaced by a function that returns all new runs: exactly one → exit 0 with `"matched_by": "new_run"`; more than one → exit 2, `"dispatched": true`, `"ambiguous": true`, `"candidate_run_ids"` (newest first) and an error telling the caller to verify each candidate's target ref and never dispatch again. `find_new_run` stays as a thin wrapper (§6: existing identifier kept, tests may call it).
4. `/implement-plan-claude` ([Dispatch helper](../../.claude/commands/implement-plan-claude.md#dispatch-helper), steps 9 and 10): document `matched_by` / `ambiguous`; a result that is not `dispatch_response` is recorded only after the run's target ref is confirmed; the read-result stage confirms the target ref before reading any verdict, and on a mismatch re-dispatches once in the same cycle (a second mismatch is a `Status: BLOCKED` failure escalation).

Alternatives considered: a correlation input echoed into `run-name` (needs every workflow plus every consumer wrapper, and old wrappers reject the input), and matching candidate runs by their inputs (REST exposes no dispatch inputs on a run, so it needs a log read per candidate). The dispatch response is exact and needs neither.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan; the helper change, its tests, and the command-file guidance that depends on the new fields are one reviewable unit.

1. **Phase 1 — exact run id from the dispatch response, no guessing on fallback, target-ref check in steps 9–10.**
   - protected paths: `.claude/scripts/dispatch_workflow.py`, `.claude/commands/implement-plan-claude.md`
   - Files: see [Files & Modules](#files--modules).
   - Done: `tests/test_dispatch_workflow.py` passes, including the new race test (two new runs, response id is the older one → that id is returned) and the ambiguous-fallback test; twin parity holds; the command file's steps 9–10 and Dispatch helper section describe the target-ref check; README.md / agents.md describe the new fields.
   - Rollback: revert the phase PR; the helper returns to newest-new-run matching with no other component depending on the new fields.

## Implementation Steps

Phase 1:
1. `.claude/scripts/dispatch_workflow.py` + twin: `_post_dispatch` sends `return_run_details: true`, parses stdout as JSON (empty → `None`, invalid → `None`), returns it. Update the module docstring (API calls, output fields).
2. Same files: add `new_runs(repo, workflow, known_ids)` returning new runs newest first; `find_new_run` returns its first element or `None`.
3. Same files: `dispatch` takes the response id path (step 2 of Approach) or the fallback path (step 3), adding `matched_by`, `ambiguous`, `candidate_run_ids`.
4. `tests/test_dispatch_workflow.py`: `FakeGitHub.post_dispatch` returns a configurable response; new tests for the response-id path (including the race where the newest new run belongs to another dispatch), the failed run read after a response id, the 204 fallback with one and with two new runs, the POST body carrying `return_run_details`, and stdout parsing.
5. `.claude/commands/implement-plan-claude.md` + twin: Dispatch helper section (new fields and what the caller does with each), step 9 and step 10 (confirm the target ref before recording an unverified match and before reading a verdict; re-dispatch once on a mismatch).
6. `README.md`, `agents.md`: the helper descriptions mention the dispatch-response id, `matched_by`, and `ambiguous`.
7. `changelog.d/5016-dispatch-exact-run-id.md` (`fixed`).

## Files & Modules

- `.claude/scripts/dispatch_workflow.py`
- `workflow-templates/.claude/scripts/dispatch_workflow.py` (twin)
- `.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin)
- `tests/test_dispatch_workflow.py`
- `README.md`
- `agents.md`
- `changelog.d/5016-dispatch-exact-run-id.md` [new]

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_dispatch_workflow.py tests/test_permission_prompts.py tests/test_update_workflows_guardrails.py -q` (the helper, its twin parity and allowlist, and the `.claude/` template guardrails).
- `ruff check` on the changed Python.
- End-to-end: the next security-audit / validation dispatch of any `/implement-plan-claude` project prints `"matched_by": "dispatch_response"`; this project's own security and validation stages exercise it.

## Risks & Mitigations

- GitHub ignores `return_run_details` (older API behaviour, 204) → the fallback path, which is today's behaviour plus the ambiguity refusal.
- The proxy or `gh` changes the response body → unparseable stdout is treated as "no run id" and falls back; covered by a test.
- Consumer repos with an older `.claude/` copy keep the old helper until their next sync → ACCEPTED — the `.claude/` sync ships the twin; the step 9–10 target-ref check in the synced command file covers the gap.
- Protected-path phase cannot be edited unattended → the phase runs only under a recorded `Protected-path approval:` line (§28.C).

## Rollout

Ships with the next `@stable` release through the existing `.claude/` sync; no flag, no repo variable. Rollback is a revert of the phase PR.

## References

- Issue #5016; observing project issue #4787 (Refs #4787), issue #4813's follow-ups #4955–#4957.
- GitHub REST: Create a workflow dispatch event (`return_run_details`).
- CLAUDE.md §23.I (helpers), §28 (issue mode), `docs/operations/master-session.md` Q40 (twin-first protected-path edits, until #4785).

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the helper tie a dispatch to its run? — Picked: A — send `return_run_details: true` and use the `workflow_run_id` GitHub returns. Alternatives: B — correlation input echoed into each workflow's `run-name` and matched on `display_title`; C — read each candidate run's inputs from its log. Why: exact, no workflow or consumer-wrapper change, no extra API calls (§5, §15). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What should the helper do when the response carries no run id and more than one new run appears? — Picked: A — exit 2 with `dispatched: true`, `ambiguous: true`, and `candidate_run_ids`; a single new run is returned with `matched_by: new_run`. Alternatives: B — keep returning the newest new run; C — exit 2 on any fallback match, even a single run. Why: the issue asks for no guessing; a single new run stays usable but flagged unverified, so callers that cannot verify are not stalled. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What should a read-result stage do when the recorded run's target ref is not this project's branch? — Picked: A — never read its verdict; re-dispatch once in the same cycle with the helper and wait on the new run; a second mismatch stops at `Status: BLOCKED`. Alternatives: B — stop at `Status: BLOCKED` on the first mismatch; C — search recent runs for one whose log shows this project's ref. Why: the fixed helper makes a repeat mismatch unlikely, so one bounded re-dispatch keeps the project unattended without looping (§28.C caps stay). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should a successful response id be followed by a read of the run? — Picked: A — one best-effort read of `actions/runs/<id>` to fill `status` and `created_at`, leaving them `null` when it fails. Alternatives: B — no read; return `status` and `created_at` as `null` always. Why: keeps the documented output fields populated (§6) at one call, fewer than today's poll. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5016` → `{"skip": false, "label": null, "reason": "no skip label"}`.
