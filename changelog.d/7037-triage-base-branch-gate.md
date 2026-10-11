<!-- changelog: changed -->
- **Check-failure triage no longer opens issues for failures that exist only on the failing PR's branch.**

Every triage issue is implemented as a new PR off the default branch, so it can only fix a failure the base branch shares. For a PR-specific failure the implementer found nothing to change and the issue ended `ai:blocked` with an ERROR alert (#7020 on PR #6674, #7013 on PR #6652). `scripts/check_failure_triage.sh` now reads the base branch's result for the same check before filing. When it is `success`, the run logs `CHECK_TRIAGE skip reason=pr_specific_failure` and the PR's own review/autofix loop keeps the failure. A failing, pending, missing or unreadable base result files the issue as before.

| The numbers that matter | Value |
| --- | --- |
| Triage issues filed since 2026-10-03 | 62, about 26 of them open and `ai:blocked` |
| Extra API reads per triaged failure | 2 for a CI workflow run, 1 for a non-Actions check |
| `CHECK_FAILURE_TRIAGE_BASE_GATE_ENABLED` | default `true`; `false` files every failure |

What this means for operators: fewer blocked triage issues and ERROR alerts for failures a base-branch PR could never fix, with base-branch breakage still filed.
