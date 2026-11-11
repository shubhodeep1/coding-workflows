<!-- changelog: fixed -->
- **The release gate's clarify waits name the Clarify run they waited on and stop early when clarify reports a failure.** Before this, a stalled smoke issue failed only after 30 idle minutes with no run link, so the heal intake could not tell why (release run 42930970244).

In `test-and-mark-stable.yml`, "Phase 1: Wait for clarify to complete" now reads up to 100 issue comments instead of 5. The old 5-comment read missed any comment after the fifth, because the list comes back oldest first. The existing clarification-needed check now reads the newest `github-actions[bot]` comment in that list, as its name says.

A trusted comment (owner, member, collaborator or `github-actions[bot]`) can fail the step early. It must start with `AI clarification workflow failed for` or `AI clarification workflow was cancelled/timed out for` and name a run in this repository (the owner/repo match ignores case). The step fails only when that run is the newest Clarify run titled with the smoke issue. It then fails with the new status `clarify_failed` and an error line linking the run. A failure or cancellation comment for an older run, while a newer run is still going, keeps the wait going; the run a cancellation comment names is also listed when the wait times out. When the newest-run lookup finds nothing, the step logs that and compares again on a later poll, at most 3 times for each comment run ID. The newest run is looked up at most once per poll, shared by both comments. Comments from other authors are ignored on purpose, so an outside commenter cannot fail the release gate.

When the wait times out, the error still starts `Clarify phase stalled — no activity for N minutes`. It now adds `(Clarify run: <url>)` or `(no non-skipped Clarify run titled '<title>' found)`.

The alt-model job's "Create alt-model smoke issue" step now outputs `created_after` and `title`. When "Wait for clarify→plan→implement (alt-model)" hits its deadline, its error line names that issue's Clarify run the same way.

Comment text is never printed. Only a run ID made of digits, checked against this repository, goes into a URL. The comment read stays at one GitHub API call per poll. Run lookups happen only on the timeout path and, while the gate is polling, on polls that find a trusted failure or cancellation comment not yet compared with the newest run.

Refs #9223
