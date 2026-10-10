<!-- agents: section="Workflow architecture" -->
Provider-outage activation gaps (issue #7099): the consumer `ai-review.yml`
`provider-outage-resume` job exposes `outputs.paused` (step id
`provider_outage`; both early skips write `paused=false`), and
`security-hold-sweep` runs with `needs: provider-outage-resume` and
`!cancelled() && ... && needs.provider-outage-resume.outputs.paused != 'true'`,
so it skips dispatch during an outage and still runs when the outage job is
disabled, skipped or failed. In `review_autofix.yml`, "Mark linked issues
review-blocked (autofix exhaustion)" (`id: mark_review_blocked_exhaustion`)
writes `labelled_issue_numbers` (digits only, from
`SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME=applied`, which
`scripts/label_helpers.sh` sets alongside its unchanged return code 0) and
`labelled_issue_numbers_resolved=true`; the outage marker step marks exactly
that set and falls back to the old derivation only when the output is missing
(`PROVIDER_OUTAGE op=mark_label outcome=fallback reason=labelled_set_unavailable`).
