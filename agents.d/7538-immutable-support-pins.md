<!-- agents: section="Immutable consumer wrapper pins" -->
- Support code follows the wrapper pin (tele-funtoken-msg-scoring#4566). A
  consumer run whose job `workflow_ref` (read from `toJSON(job)`) is
  `shubhodeep1/coding-workflows/.github/workflows/<this file>@<40-hex>` takes
  every nested support checkout from that commit. This covers `plan.yml`,
  `clarify.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`,
  `orchestrate.yml`, `orchestrate_poll.yml`, `check_failure_triage.yml`,
  `cancel_on_pr_close.yml`, `sync_ai_labels.yml`, `workflow_failure_heal.yml`
  and the integration-ref resolver in those files and `validate.yml`.
  - The "support source fallback" and "main snapshot" checkouts run only while
    `SCRIPT_REF` (or `DUPLICATE_NOTICE_SCRIPT_REF`) is `stable`, so a pinned run
    never stages code from `main`.
  - The "Ensure workflow support source checkout" steps, and a new "Verify
    pinned workflow support checkout" step in `check_failure_triage.yml` and
    `workflow_failure_heal.yml`, fail the job unless the checkout `HEAD` equals
    a 40-hex `SCRIPT_REF`.
  - The integration-ref resolver fetches the pinned commit, checks its `HEAD`,
    and fails closed on any staging failure for a pinned run, logging
    `INTEGRATION_REF_RESOLVE outcome=resolver_unavailable reason=<token>
    pinned_ref=<sha>` (with `ci_triage_routing=on` before `pinned_ref=` while
    routing is on). Unpinned runs keep the existing CI-triage-routing rule.
  - Every support checkout is checked against the pin before it is used. The
    orchestrate run-start staging step fails the job on a mismatch or a
    missing checkout; implement's duplicate-PR notice skips its Telegram
    helper; the heal-report jobs of `implement.yml`, `clarify.yml` and
    `plan.yml` skip with `WORKFLOW_HEAL_PHASE_REPORT skip
    reason=support_ref_mismatch`.
  - The "Bootstrap GitHub API retry helper" step (identical in
    `implement.yml`, `validate.yml`, `orchestrate_clarify_respond.yml`,
    `security-audit.yml` and `review_autofix.yml`) fetches the pinned commit
    when the job `workflow_ref` names any coding-workflows workflow file at a
    40-hex SHA, and reads the helper only when `FETCH_HEAD` equals it;
    otherwise the steps fall back to their plain API calls, never to `stable`.
  - Wrappers on `@stable`, a branch, or any other identity keep `stable`
    support with the `main` fallback; the wrapper itself is mutable there.
  - `install-codex` is referenced by commit
    (`install-codex@91b30f6fba400686c5dded34423f88f1ea10e163`), in the
    workflows and inside `setup-runtime`. Bump that pin in the same PR when the
    action changes. `validate.yml` calls `setup-runtime` by commit
    (`setup-runtime@18e876fa6b0f9f285c71789c937af17e31fd7411`, #7551); bump it
    in the same PR as any change to `setup-runtime/action.yml`.
  - The `ai-review.yml` wrapper's `.provider-outage-support` checkout joins
    `.security-hold-support` and `.non-retryable-backfill-support` in
    `scripts/workflow_wrapper_refs.py`'s `_PINNED_SUPPORT_CHECKOUT_PATHS`, so an
    installed wrapper pins it to the release SHA; no support checkout in a
    rendered wrapper is left on `stable`.
  - Contract tests: `tests/test_workflow_support_ref_follows_pin.py` and
    `tests/test_update_workflows_guardrails.py`.
