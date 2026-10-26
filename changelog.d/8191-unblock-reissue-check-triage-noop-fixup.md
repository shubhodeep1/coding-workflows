<!-- changelog: fixed -->
- **Two unblock dead ends are fixed: stall recovery now keeps `ai:check-triage` on a re-issued CI-triage issue, and a fix-up the implement phase closes as already implemented now ends the unblock judge's wait.** Together these sent btc_sweeper #223 to the needs-human digest even though its fix had already shipped.

When stall recovery re-issued the CI-triage issue btc_sweeper #209 as #223, it dropped `ai:check-triage` but copied the check-failure-triage markers, so `scripts/resolve_integration_ref.sh` refused every clarify and plan run with `reason=label_missing`. Both `close_and_reissue` paths in `scripts/orchestrate_poll_process.sh` now carry the label over, like `ai:workflow-heal`. Separately, a verification-only operator-step fix-up (btc_sweeper #229) closes through implement's success-no-op guard with `ai:closed`, and `scripts/unblock_judge.sh` read that as "did not merge in time" and spent a judge round. The success-no-op close comment in `.github/workflows/implement.yml` now ends with `<!-- ai:implement-success-noop:v1 issue=<n> -->`, and the fix-up wait treats a completed `ai:closed` fix-up carrying that marker from the pipeline login like a merged one.

| The numbers that matter | Value |
| --- | --- |
| Re-issue paths that keep `ai:check-triage` | 2 (managed and standalone `close_and_reissue`) |
| Extra API calls for the fix-up check | 1 paginated comments read, only for a fix-up closed `completed` with `ai:closed` and no `ai:merged` |
| New log lines | `op=fixup_done via=success_noop`, `outcome=skip reason=fixup_comments_unavailable` |

What this means for operators: a re-issued CI-triage issue routes to the triaged PR's branch the way its source did, instead of sitting on `ai:blocked` until someone fixes its labels. A fix-up that needed no change resumes or holds its item instead of using up a round and reaching `item_cap`. If the fix-up's comments cannot be read, the wait stays open and the next run checks again.
