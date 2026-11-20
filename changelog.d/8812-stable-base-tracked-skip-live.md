<!-- changelog: changed -->
- **On the `stable` line, check-failure triage now skips a per-PR issue when a heal issue already tracks the same default-branch failure.** The skip shipped in v1.47.5 (#9272) but could not fire, because heal issues did not carry the `<!-- workflow-failure-heal:workflow=<path> -->` header it matches on.

With #8812 backported, `scripts/workflow_failure_heal.py` writes that header on every new run-sourced heal issue. A PR whose failed jobs all also fail on the default branch's newest run of that workflow gets no per-PR triage issue (`CHECK_TRIAGE skip reason=base_failure_tracked`); its heal issue gets one comment naming the PR instead. Heal issues filed before this release have no header, so triage keeps filing per-PR issues for the failures they track.

What this means for operators: a red default branch produces one heal issue and comments on it, not one triage issue per open PR. Set the `CHECK_TRIAGE_BASE_TRACKED_SKIP_ENABLED` repository variable to `false` to keep filing per-PR issues.

Refs #8812, Refs #9272.
