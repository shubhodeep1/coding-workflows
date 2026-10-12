<!-- changelog: security -->
- **The consumer security-hold sweep runs the pinned release and does its work with the job token.** Its support checkout no longer follows the moving `stable` branch, and `GH_PAT` is only used to name the account that posts findings markers.

The hourly `security-hold-sweep` job in `ai-review.yml` checked out `scripts/review_security_hold_sweep.py` from `stable` and ran it with `GH_PAT`. `scripts/workflow_wrapper_refs.py` now pins that checkout (`path: .security-hold-support`) to the same release SHA as the wrapper's reusable-workflow refs, so the updater, the drift audit and `/seed-repo` install `ref: <release-sha> # stable`; a checkout of that path with any other ref fails the render. The job now sets `GH_TOKEN` to `github.token` and passes `GH_PAT` as `SECURITY_HOLD_FINDING_AUTHOR_TOKEN` with `SECURITY_HOLD_SWEEP_TOKEN_MODE=actions_token`. The script reads that PAT once for a `gh api user` call and removes it from its environment before listing, dispatching or commenting. Findings markers count only from the PAT's account (from `github-actions[bot]` when no PAT is set), and stale-dispatch markers count from `github-actions[bot]` of author type Bot or, for older markers, the PAT's account. A user account named `github-actions` never matches.

| The numbers that matter | Value |
| --- | --- |
| Pinned support checkouts | `.security-hold-support` (the provider-outage checkout is unchanged) |
| Token mode default in the script | `legacy` (the source-repo sweep is unchanged) |
| Log lines | `SECURITY_HOLD_SWEEP outcome=skip reason=finding_identity_unavailable\|invalid_token_mode` |

What this means for operators: consumer `ai-review.yml` wrappers show a one-line drift on the sweep checkout until the next workflow update. If the PAT's identity cannot be read, the sweep dispatches nothing that tick and retries an hour later.
