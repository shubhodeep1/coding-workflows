<!-- changelog: security -->
- **The guard differential check now runs the base branch's verifier, not the PR's.** A pull request can no longer weaken a `.claude/hooks/*_guard.py` hook and, in the same change, edit `scripts/guard_differential.py` so the check passes without comparing anything.

The `Guard differential check (issue #5174)` step in `.github/workflows/ci.yml` copies `scripts/guard_differential.py` out of the fetched base commit into `$RUNNER_TEMP` and runs that copy against the PR's merge commit. The PR's hooks, corpora, and body are only the data it checks. The verifier also prints a `::warning::GUARD_DIFFERENTIAL verifier_change` line when a PR changes the verifier script or any `Guard differential …` step of `ci.yml`, so reviewers read those as security-boundary changes. A base branch that does not carry the verifier yet runs the PR's copy, which is the same trust as before, and says so in a `verifier=head reason=base-has-no-verifier` warning. The verifier now also runs every base hook before the first PR hook, so a PR hook can no longer rewrite the base hook copy mid-run and hide its own loosening. It also compares the settings wiring before the first PR hook, so a PR hook can no longer restore the checkout's settings files to the base version and hide a wiring change. And the step runs right after the job's dependency install, before any test or script from the PR's checkout, so PR code cannot plant a `.pth` file in the Python install the verifier runs under. The dependency install and the step's `python3 -c` call run with `-P`, so a `pip/` package or `json.py` in the PR's checkout is never imported before the verifier. Security audit finding `pr-controlled-verifier` (issue #5327).

| The numbers that matter | Value |
| --- | --- |
| Verifier that runs on a PR into `main` / `stable` | the base commit's `scripts/guard_differential.py` |
| New log lines | `verifier=base source=<sha>:…`, `verifier=head reason=base-has-no-verifier`, `verifier_change path=…` |
| Exit-code impact of a `verifier_change` | none (warning only) |
| Hook run order | settings wiring compared first, then every base run, in both hook trees, before the first PR hook run |
| Step position in `tests-hooks-and-orchestrator` | right after `Install Python CI dependencies`, before any step that runs code from the checkout |

What this means for contributors: on a base branch that carries the verifier, a change to `scripts/guard_differential.py` takes effect once it has merged, on every CI run that starts afterwards (including new runs on pull requests that were already open); a base branch that does not carry it yet runs each PR's own copy. The CI step may pass only flags the base copy already accepts. Land a new flag in the script first and use it in the step in a later PR.

### For contributors

Keep the check right after the dependency install: `tests/test_guard_differential.py` fails when a step is added before it, when the install differs from the package list pinned in the test, or when workflow, job, or step `env` gains a key outside the test's allow-list. A pull request that rewrites `ci.yml` (the step, or a step before it) still controls what its own run executes, as with any `pull_request` workflow. That edit shows in the diff the reviewer panel reads, and once it is on the base, the next PR's verifier reports any further change to the step. Details are in `agents.md` under "Guard differential check", bullet "Pinned verifier (issue #5327)".
