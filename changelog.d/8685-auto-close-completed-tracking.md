<!-- changelog: changed -->
- **Finished orchestrator projects now close their tracking issue, and check-triage issues for PRs merged into a project branch can close too.**

Every successful project ended with "Issue kept open for manual review." and nothing ever closed it: on 2026-11-02, 7 of the 10 open `ai:orchestrator-tracking` issues were finished and were closed by hand. The poller now closes a `complete` project's tracking issue on a later tick, once its activation verdict is final, its release callback is handled, its plan-split source is settled and no `ai:integration-backpressure` label is left. The closing comment links the verdict and, for DORMANT, the repository's `ai:operator-step` issue. Separately, `scripts/check_triage_resolution.py` closed a triage issue only when the default branch contained the triaged PR's own merge commit, which never happens for a PR merged into `orchestrator/project-<N>` (#8592, #8570, #8565, #8562 and #8501 were stuck on `compare=diverged`). The gate now verifies such a fix on the project's integration PR head, or on the default branch after the integration PR merged.

| The numbers that matter | Value |
| --- | --- |
| Writes per closed project | 1 comment, 1 close, 1 state comment, once |
| New kill switch | `ORCHESTRATOR_CLOSE_COMPLETED_TRACKING` (default `true`; repository variable, forwarded by `orchestrate_poll.yml`) |
| Existing kill switch for the triage change | `CHECK_TRIAGE_RESOLVED_GATE_ENABLED` |

What this means for operators: consumer repositories get this on by default on the next `@stable` sync, and any already-open finished projects close on the first poller tick after that. Reopen an auto-closed tracking issue to keep it open for good (unless the poller logged `state_persist=failed` when it closed it), or set `ORCHESTRATOR_CLOSE_COMPLETED_TRACKING=false` to keep every finished project open. Failed and validation-failed projects stay open as before.

### For contributors

Logs: `TRACKING_AUTO_CLOSE tracking_issue=<N> outcome=closed|deferred reason=...` and a trailing `evidence_ref=default|integration_pr:<n>` on `CHECK_TRIAGE_RESOLVED`. The close reads only the comments, labels and state the tick already holds; its outcome is kept in `.tracking_auto_close` and its comment ends with `<!-- ai:tracking-auto-close:v1 issue=<N> -->`.
