<!-- changelog: fixed -->
- **Planning no longer runs on a check-triage issue that is already fixed on the default branch, and a planned `_headers` file is no longer left out of the pipeline plan grant.**

When a check-triage issue's pull request merged with the check still red on its final head, plan.yml's "Close superseded CI-repair issue (merged PR)" step planned anyway, even if the default branch had since run the check green. The step now runs `scripts/check_triage_resolution.py gate` in that case, closes the issue with evidence once the fix is verified, and skips planning. Separately, the pipeline plan grant now keeps `_headers` and `_redirects` (Cloudflare Pages and Netlify config files) as exact files. Before this change they were dropped as possible directories, so implement's `files_touched` guard refused an approved plan that listed one.

| The numbers that matter | Value |
| --- | --- |
| Consumer issues behind this fix | fun-token-multi-chain #765, #771 |
| Kill switch for the new plan-time check | `CHECK_TRIAGE_RESOLVED_GATE_ENABLED` (default `true`) |

What this means for operators: CI failures caused by an outage that `main` has already recovered from close on their own instead of raising "Planning blocked", and Cloudflare Pages header changes no longer latch `ai:scope-blocked`.
