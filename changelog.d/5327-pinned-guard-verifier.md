<!-- changelog: security -->
- **The guard differential check now runs the base branch's verifier, not the PR's.** A pull request can no longer weaken a `.claude/hooks/*_guard.py` hook and, in the same change, edit `scripts/guard_differential.py` so the check passes without comparing anything.

The `Guard differential check (issue #5174)` step in `.github/workflows/ci.yml` copies `scripts/guard_differential.py` out of the fetched base commit into `$RUNNER_TEMP` and runs that copy against the PR's merge commit. The PR's hooks, corpora, and body are only the data it checks. The verifier also prints a `::warning::GUARD_DIFFERENTIAL verifier_change` line when a PR changes the verifier script or any `Guard differential …` step of `ci.yml`, so reviewers read those as security-boundary changes. A base branch that does not carry the verifier yet runs the PR's copy, which is the same trust as before, and says so in a `verifier=head reason=base-has-no-verifier` warning. Security audit finding `pr-controlled-verifier` (issue #5327).

| The numbers that matter | Value |
| --- | --- |
| Verifier that runs on a PR into `main` / `stable` | the base commit's `scripts/guard_differential.py` |
| New log lines | `verifier=base source=<sha>:…`, `verifier=head reason=base-has-no-verifier`, `verifier_change path=…` |
| Exit-code impact of a `verifier_change` | none (warning only) |

What this means for contributors: a change to `scripts/guard_differential.py` takes effect once it has merged, on every CI run that starts afterwards (including new runs on pull requests that were already open), and the CI step may pass only flags the base copy already accepts. Land a new flag in the script first and use it in the step in a later PR.

### For contributors

A pull request that rewrites the CI step itself still controls what its own run executes, as with any `pull_request` workflow. That edit shows in the diff the reviewer panel reads, and once it is on the base, the next PR's verifier reports any further change to the step. Details are in `agents.md` under "Guard differential check", bullet "Pinned verifier (issue #5327)".
