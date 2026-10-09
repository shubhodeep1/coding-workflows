<!-- changelog: security -->
- **The release gate now runs its smoke tests only against this repository or a repository listed in `.github/ai/smoke_test_repos.json` on the default branch.** Security finding `smoke-target-cross-repo-write` (high) is closed. Refs #3576.

`test-and-mark-stable.yml` accepted any `test_repo` that looked like `owner/repo`, and seven of its jobs then used `GH_PAT` to create issues, labels, comments, file commits and workflow dispatches in that repository. A new `Validate smoke-test target repository` step in the `source` job, which every one of those jobs depends on, now stops the run first. An empty `test_repo` or this repository's own name (any letter case) passes without an API call. Any other value must be listed in `.github/ai/smoke_test_repos.json`, read from the default branch rather than the dispatched ref, because `gate_only` runs accept any ref. A missing, unreadable or malformed allowlist fails the run. The step reads the file with `github.token`, not `GH_PAT`. The value the orchestrator poller takes from a tracking issue's `test repo:` metadata line goes through the same check.

| The numbers that matter | Value |
| --- | --- |
| Repositories a dispatcher can make the gate write to | this one, plus the allowlist (was: any repository `GH_PAT` reaches) |
| Allowlist entries shipped | 0 |
| New GitHub API calls | 1 contents read, only for a `test_repo` that is not this repository |

What this means for operators: if you run the release gate against another repository, add it to `.github/ai/smoke_test_repos.json` on `main` through a pull request first. Otherwise the run fails in `source` with `SMOKE_TEST_REPO_ALLOWLIST outcome=rejected reason=not_listed`. Runs that leave `test_repo` empty are unchanged. The token itself is still `GH_PAT`: a short-lived token scoped to the target repository needs a GitHub App credential this repository does not have.
