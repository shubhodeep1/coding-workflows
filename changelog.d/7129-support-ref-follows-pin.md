<!-- changelog: fixed -->
- **Plan, clarify, implement and clarification-response runs now use support scripts from the same commit as the workflow they run.** A consumer wrapper pinned to `<file>@<sha>` no longer runs that workflow against newer scripts from `stable`.

The four reusable workflows `plan.yml`, `clarify.yml`, `implement.yml` and `orchestrate_clarify_respond.yml` staged their support scripts from `stable` in every consumer repo, whatever commit the wrapper pinned. When a pin falls behind `stable`, the old workflow can call scripts whose interface has changed, and every run fails the same way. Each workflow's "Resolve workflow support ref" step now reads its own pinned commit from the job context and uses that commit. `@stable` callers and this repository behave as before, and files an older commit lacks still come from the `main` snapshot.

What this means for consumer repos: a workflow run uses one consistent version of the workflow and its support scripts, so a lagging pin no longer breaks a phase.

### For contributors

This applies the `validate.yml` fix (#7123) to the other four reusable workflows. Only `shubhodeep1/coding-workflows/.github/workflows/<this file>@<40-hex>` counts as a pin. `tests/test_workflow_support_ref_follows_pin.py` runs each of the 8 steps for a pin, a tag and look-alike identities.
