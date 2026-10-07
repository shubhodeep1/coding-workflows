<!-- changelog: fixed -->
- **A merge conflict the resolver sandbox cannot carry now stops a PR once, instead of failing review/autofix every 30 minutes.** The generated workspace manifest is merged deterministically again, and a host-only conflicted file stops the head after a single failure with a comment that names it.

PR #6438 failed the "Run Codex resolver, validate, stage, commit" step about 28 times on one head between 2026-10-06 16:55 and 2026-10-07 02:51 UTC. Two of its five conflicted paths could not enter the resolver sandbox: `.ai/.workspace_source_manifest.txt` and `.claude/hooks/pr_merge_status_guard.py`. The manifest should never have reached the resolver. The stage check in `scripts/review_conflict_prepare.sh` used the pattern `*' 2 '*' 3 '*`, which never matches the stage list `1 2 3`, so the deterministic union merge never ran. It now matches `' 2 3 '`. For host-only files such as the guard hook, `scripts/review_conflict_resolve.sh` now logs `Conflict resolver: host-only conflicted path(s) need a manual merge: <paths>` and fails closed with the new reason `sandbox_path_host_only` before any model call. Every resolver fail-closed reason now appears in the failure marker as `conflict_resolver_<reason>`. The gate's identical-failure cap stops a head on the first marker whose reason is non-retryable. It no longer waits for `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`. The cap comment is titled "non-retryable failure" and quotes the failed run's first error.

| The numbers that matter | Value |
| --- | --- |
| Failed runs on PR #6438's head `0c32cb6` | about 28 in 10 hours |
| Failures before a non-retryable reason is capped | 1 (other reasons: `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`, default 3) |
| Non-retryable reasons | `conflict_resolver_sandbox_path_host_only`, `conflict_resolver_sandbox_path_unsupported`, `conflict_resolver_sandbox_support_missing` |
| New gate output | `fingerprint_cap_non_retryable` |
| New `check-paths` argument | optional report file (`scripts/review_untrusted_workspace.py check-paths <host> <paths> [<report>]`) |

What this means for operators: a PR whose merge with the base touches a host-only file now gets one `ai:review-blocked` label and one cap comment naming the file. Resolve that merge by hand and push; the new head runs normally. The cap, including the new non-retryable stop, is still governed by the repository variable `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`. In coding-workflows it was `false` from 2026-09-30 to at least 2026-10-07. That is why PR #6438 was never capped, so check that the variable is unset or `true`.

### For contributors

The resolver sandbox boundary from #6187 is unchanged: host-only paths are never handed to the model, and no host fallback was added. `check-paths` echoes a rejected name only when it is a plain relative path (`[A-Za-z0-9_.][A-Za-z0-9._/-]*`) that is a regular file or is absent on the host. Every other rejection is reported as a nameless `unsafe` entry and keeps `sandbox_path_unsupported`, so no untrusted name reaches the log. Integration-sync PRs do not export the specific reason. Their resolver failures must keep counting toward the resolver retry-state escape threshold (`RESOLVER_ESCAPE_THRESHOLD_N`, default 5), whose escalation drives the orchestrator's automatic branch rebuild.
