<!-- changelog: fixed -->
- **A check-failure triage issue that stall recovery re-issues can now be clarified again.** The replacement keeps `ai:check-triage` and gets `ai:orchestrator-managed` only when it belongs to an orchestrator project.

When standalone stall recovery asked the stall judge and the judge chose `close_and_reissue`, the replacement was created by the orchestrator's re-issue code in `scripts/orchestrate_poll_process.sh` (`execute_stall_recovery_action`). That code always added `ai:orchestrator-managed` and dropped `ai:check-triage`. A re-issued triage issue then kept its check-failure-triage header but had no triage label, no project and no tracking line, so `scripts/resolve_integration_ref.sh` refused every clarify run with `reason=label_missing`. `btc_sweeper#223` (re-issued from #209) sat blocked that way until the unblock judge spent its rounds and parked it as `ai:needs-human`. #7511 and #7515 had the same labels, and #7516 left this writer to a follow-up. The standalone re-issue path had the same gap for `ai:check-triage`.

| The numbers that matter | Value |
| --- | --- |
| Re-issue paths fixed | 2 (orchestrator and standalone `close_and_reissue`) |
| Extra GitHub API calls | 0 (labels come from the source-issue read each path already makes) |

What this means for consumer repos: after the next `@stable` sync, a stalled CI-triage issue that gets re-issued routes to the failing PR's head branch like the original did, and the triage filer's dedup still sees it, so no duplicate triage issue is filed for the same failure. A standalone issue re-issued through the stall judge stays standalone. Wave items and other project issues, such as security-pass fix issues, keep `ai:orchestrator-managed` as before. Issues already re-issued with the wrong labels, such as `btc_sweeper#223`, still need their labels fixed by hand or need to be closed.

### For contributors

The orchestrator re-issue adds `ai:orchestrator-managed` when the stall has a `local_id` or the source issue already carried the label, and adds `ai:check-triage` when the source carried it. The standalone re-issue adds `ai:check-triage` from the same read or from its `labels_json`. `tests/test_stall_reissue_lineage_labels.py` runs both case blocks with stubbed `gh` and passes the replacement's labels and body through the resolver's own `CI_TRIAGE_PARSE_PY`.
