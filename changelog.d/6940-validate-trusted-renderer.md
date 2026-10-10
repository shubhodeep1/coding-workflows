<!-- changelog: fixed -->
- **Self-repo validation runs stage the template renderer from the same commit as the templates.** `scripts/stage_workflow_support.sh` no longer keeps the validation checkout's own `scripts/render_validation_templates.py` when the validation-harness templates come from the verified support commit.

Since issue #6578 a self-repo validation run takes its templates from the verified support commit, but the renderer stayed with the checkout being validated. An integration branch that had not synced `main` rendered `main`'s `10_family_marker.sh.j2`, which uses the `shell_quote` filter from #6569, with a renderer that did not define it. Run 37925800341 failed with `No filter named 'shell_quote'`, the poller recorded `harness_error`, and project #6664 was marked `ai:harness-broken`. The renderer is now copied from the trusted support checkout under the same condition the templates use, logged as `VALIDATE_TRUSTED_RENDERER` (and `VALIDATE_TRUSTED_RENDERER_OVERRIDE` when the checkout's copy differed), and a missing trusted copy fails closed. Consumer repositories and explicit-target runs keep the previous preserve rule.

What this means for operators: a project whose integration branch lags `main` no longer stops with a harness error because its renderer is older than the templates; the validation run uses one consistent harness version.

### For contributors

`tests/test_validate_workflow_validate_bootstrap.py` covers the override, the fail-closed path and the consumer and explicit-target behaviour.
