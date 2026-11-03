<!-- changelog: fixed -->
- **An issue written by a person that only mentions `check-failure-triage` markers in prose is no longer held as `label_missing`.**

Refs #8671. `scripts/resolve_integration_ref.sh` searched the whole issue body for a `check-failure-triage` marker. Issue #8671 is a feature request with no `ai:check-triage` label that discusses the fingerprint and markers in its text, so the resolver refused it with `reason=label_missing` and clarify held it. Now a label-less issue whose body does not open with a `check-failure-triage` marker is treated as an ordinary issue. Issues filed by `scripts/check_failure_triage.sh` always open with the `fp=` header, so a real triage issue that lost its label keeps that header and is still refused as `label_missing`. Labelled issues are unchanged.

The new `CI_TRIAGE_PROSE_MARKER_SKIP_ENABLED` switch defaults to `true`. `false` restores the old refusal; any other value also turns the skip off and logs a `::warning::`. The resolver reads it from its own environment; no workflow forwards a repository variable for it yet. No log prefix, refusal reason or identifier changed.
