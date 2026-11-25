<!-- changelog: changed -->
- **The release gate's "Deep verify: inspect all workflow run steps" body now lives in a script.**

The body of that step in the `e2e-smoke-test` job of `.github/workflows/test-and-mark-stable.yml` moved verbatim to `scripts/stable_e2e_step_deep_verify.sh`, through the same github.sha-pinned step-script wrapper as Phase 4 and Phase 4b. The step's behaviour is unchanged. The move keeps `test-and-mark-stable.yml` under its 430,000-byte ceiling after the clarify negative-test output guards (#9797) were added; those guards are already in the v1.47.6 changelog.

What this means for operators: nothing changes in how the release gate runs or reports.
