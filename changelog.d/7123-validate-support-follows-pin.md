<!-- changelog: fixed -->
- **Runtime validation now stages its support scripts from the same commit as the pinned `validate.yml`.** A consumer wrapper that calls `validate.yml@<sha>` no longer runs that workflow against newer scripts from `stable`.

Consumer wrappers pin the reusable validation workflow to a commit, but its "Fetch workflow support files" step always cloned the support scripts from `stable`. When a pin fell behind, the old workflow ran against newer scripts and failed the same way on every re-dispatch. binance-blessings#249's integration branch pinned `de7fef2` and kept failing validation for this reason. The step now reads its own pinned commit from the job context, fetches the support scripts from that commit, and checks that it got exactly that commit. `@stable` callers and this repository behave as before.

What this means for consumer repos: validation runs on an orchestrator integration branch use the support scripts from the same commit as the pinned workflow. The GitHub API retry helper, the integration-ref resolver and the `setup-runtime` action still come from `stable`. A branch that already pins an older `validate.yml` gets this after its next sync from the default branch.

### For contributors

The identity source is `toJSON(job)`'s `workflow_ref`, as in `review_autofix.yml` and `security-audit.yml`. Only `shubhodeep1/coding-workflows/.github/workflows/validate.yml@<40-hex>` counts as a pin. `plan.yml`, `clarify.yml`, `implement.yml` and `orchestrate_clarify_respond.yml` still stage from `stable` in consumers.
