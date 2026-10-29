<!-- changelog: fixed -->
- **The nightly validation self-test status commit now pushes with `GH_PAT`, and a refused push names the ruleset as the cause.**

The `commit-selftest-status` job of `nightly-validation-selftest.yml` pushed `analysis/validation-selftest-status.json` to `main` with `github.token`. Run 40439152665 was refused with `GH013: Repository rule violations found for refs/heads/main` ("2 of 2 required status checks are expected"), even though the self-test itself passed. The step now pushes with `secrets.GH_PAT || github.token`, the same identity as the other bot pushes to `main`. The PAT reaches only this job, which runs on a fresh runner and receives just the summary JSON (#6568). The self-test job keeps its read-only token.

When the push still fails, the step prints one `::error::VALIDATION_SELFTEST_STATUS outcome=failed` line: `reason=push_rejected_by_ruleset` for a repository-rule or protected-branch refusal, otherwise `reason=push_failed rc=<n>`. Both lines name the token source (`token=gh_pat` or `token=github_token`), and the step still fails. A successful push logs `VALIDATION_SELFTEST_STATUS outcome=pushed`.

What this means for operators: if the nightly run still fails with `reason=push_rejected_by_ruleset token=gh_pat`, the `GH_PAT` account is not a bypass actor on the `main` ruleset either, and the status has to be delivered through a pull request instead. Status pushes made with `GH_PAT` start push CI on `main`, as the workflow-log-analysis pushes already do.
