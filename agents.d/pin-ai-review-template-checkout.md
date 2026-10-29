<!-- agents: section="Immutable consumer wrapper pins" -->
- Third-party actions in `workflow-templates/*.yml` are pinned to a 40-hex
  commit SHA with the tag as a trailing comment (`actions/checkout@<sha>  # v5.1.0`).
  The renderer pins only coding-workflows refs and leaves action refs as
  written, so a movable tag in a template would ship to every consumer and fail
  their workflow-ref pinning gates (fun-token-multi-chain#640).
  `tests/test_update_workflows_guardrails.py`
  (`test_wrapper_template_third_party_actions_are_sha_pinned`) enforces it.
