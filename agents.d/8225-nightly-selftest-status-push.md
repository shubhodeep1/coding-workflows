<!-- agents: section="Validation harness isolation" -->
- Nightly self-test status push (#8225): the `commit-selftest-status` job of `nightly-validation-selftest.yml` pushes the status commit with `secrets.GH_PAT || github.token` (step env `STATUS_PUSH_TOKEN`, with the fixed word `STATUS_PUSH_TOKEN_SOURCE=gh_pat|github_token`), because `github.token` is not a bypass actor on the `main` ruleset (run 40439152665, GH013). The PAT reaches only that fresh-runner job; the self-test job keeps `contents: read` and never sees it. A failed push prints `::error::VALIDATION_SELFTEST_STATUS outcome=failed reason=push_rejected_by_ruleset|push_failed` (pattern `GH006|GH013|protected branch|push declined|repository rule`, as in `review_autofix.yml`) and still fails the step; push output is echoed with URL userinfo redacted. Tests: `tests/test_nightly_validation_selftest_workflow_contract.py` runs the shipped step against a fake `git`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `VALIDATION_SELFTEST_STATUS` (`nightly-validation-selftest.yml` "Commit validation self-test status": `outcome=pushed|failed reason=push_rejected_by_ruleset|push_failed branch= token=gh_pat|github_token [rc=]`)

LOG_PREFIX.name=VALIDATION_SELFTEST_STATUS
