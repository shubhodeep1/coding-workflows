<!-- changelog: fixed -->
- **The release gate's clarify waits name the Clarify run they waited on and stop early when clarify reports a failure.** Before this, a stalled smoke issue failed only after 30 idle minutes with no run link, so the heal intake could not tell why (release run 42930970244).

In `test-and-mark-stable.yml`, "Phase 1: Wait for clarify to complete" now reads up to 100 issue comments instead of 5. The old 5-comment read missed any comment after the fifth, because the list comes back oldest first.

A trusted comment (owner, member, collaborator or `github-actions[bot]`) can fail the step early. It must start with `AI clarification workflow failed for` and name a run in this repository. The step fails only when that run is the newest Clarify run titled with the smoke issue. It then fails with the new status `clarify_failed` and an error line linking the run. A failure comment for an older run, while a newer run is still going, keeps the wait going. A "cancelled/timed out" comment does not fail the step early, because a newer run usually replaced the cancelled one; its run is listed when the wait times out.

When the wait times out, the error still starts `Clarify phase stalled — no activity for N minutes`. It now adds `(Clarify run: <url>)` or `(no non-skipped Clarify run titled '<title>' found)`.

The alt-model job's "Create alt-model smoke issue" step now outputs `created_after` and `title`. When "Wait for clarify→plan→implement (alt-model)" hits its deadline, its error line names that issue's Clarify run the same way.

Comment text is never printed. Only a run ID made of digits, checked against this repository, goes into a URL. No GitHub API call is added while the gate is polling. Run lookups happen only on the failure-comment and timeout paths.

Refs #9223
