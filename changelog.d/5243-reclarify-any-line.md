<!-- changelog: fixed -->
- **An answer that ends with `/reclarify` now resumes a blocked issue.** On an issue waiting for a human answer, `clarify.yml` accepts `/reclarify` at the start of any line of a trusted comment, not only as its first text.

Until now the clarify job started only when the comment body began with `/reclarify`. On 2026-09-29, 13 blocked issue-mode projects were answered with the answer text first and `/reclarify` on the last line. Every clarify run was `skipped`, `ai:claude-blocked` stayed on the issues, and nothing reported it, so the projects stalled for 3 to 14 hours. The job-level gate in `clarify.yml`, `internal-clarify.yml` and `workflow-templates/ai-clarify.yml` now also accepts a later line that starts with `/reclarify`, on an issue labelled `ai:claude-blocked`, `ai:claude-handoff-failed` or `ai:blocked`, in a comment that carries no `<!--` HTML comment (every automation marker is one), on an issue that is not an orchestrator tracking or managed issue. The `Decide clarify route` step repeats the rule and skips any other comment with `AI_PHASE_GATE_V1 … reason=not_reclarify_command`. That skip applies no label, posts no automatic `/answer`, and starts no Codex run or Claude handoff.

| The numbers that matter | Value |
| --- | --- |
| Labels that allow a later-line `/reclarify` | `ai:claude-blocked`, `ai:claude-handoff-failed`, `ai:blocked` |
| Never counts | a mention inside a sentence, in backticks, or indented; a comment carrying `<!--` with `/reclarify` below its first line; a later line on an `ai:orchestrator-tracking` or `ai:orchestrator-managed` issue, or one whose body carries the `Managed by: AI Orchestrator` line |
| Comment starting with `/reclarify` | unchanged, on every issue |
| New automation marker | `<!-- ai:implementation-plan:v1 -->`, last line of the `plan.yml` implementation-plan comment |
| Extra GitHub API calls | 0 |

What this means for operators and maintainers: answer a blocked issue in any order, and put `/reclarify` on its own line anywhere in the comment. On other issues, keep `/reclarify` as the first text of the comment, as before. Consumer repos get the new `ai-clarify.yml` gate on the next `@stable` sync. Until then their old wrapper only lets through comments that start with `/reclarify`, as today.

### For contributors

Automation comments post as the same trusted `User` account as a human, so the later-line form needs two guards. The `<!--` exclusion covers marked comments such as `ai:claude-blocked:v1`. The label scope keeps model text in unmarked automation comments on orchestrator, tracking and validation issues from starting clarify. The implementation-plan comment ends with a bare `/reclarify` line, so it now carries a marker. The Claude intake's `has_trusted_reclarify` vouch check is unchanged: an outside author's issue enters the Claude pipeline only through a first-line trusted `/reclarify`, and that comment stays in the history. `tests/test_phase_wrapper_predicate_contract.py` pins the clause and evaluates it over sample comments. `tests/test_phase_skip_gate_telemetry_contract.py` runs the real route step over the same cases.
