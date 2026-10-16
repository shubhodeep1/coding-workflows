<!-- changelog: fixed -->
- **An orchestrator-managed issue that copies a CI-triage header now finds its project's branch.** Before this, the integration-ref resolver stopped such an issue with `label_missing` and the phase failed.

An `ai:orchestrator-managed` issue whose body carries a copied `check-failure-triage:` header, but no `ai:check-triage` label, is not a triage issue to route to a PR head. When its body also has a `Tracking issue: #N` line, `scripts/resolve_integration_ref.sh` now skips the CI-triage routing and resolves the integration branch declared on that tracking issue, like any other managed issue. #7511 was stuck this way (heal #7512).

What this means for operators: nothing to set. A managed issue without a tracking line, and any unlabelled non-managed issue with a triage header, is still refused with `label_missing`. Labelled `ai:check-triage` issues are routed exactly as before.
