<!-- changelog: fixed -->
- **An orchestrator project's validation failure now gets its own heal issue instead of an immediate "lineage cap" escalation.** Heal reports link the project's failed validation runs, and run-less reports are no longer lumped together across repositories.

binance-blessings#249's `ai:harness-broken` report was escalated at "generation 4 > 3" on its first occurrence (intake run 38108920859). The reporter linked only failed runs titled like the issue, but validation runs are named `AI Validate [tracking:N]`, so the report carried no run. With no run, the intake fingerprinted it as `label:ai:harness-broken`, the same fingerprint as every other run-less harness report in any repository, and three unrelated earlier heals had used up that lineage. Reports now link failed runs whose title ends in `[tracking:<issue>]`, and a report that still has no run is fingerprinted as `label:<label>|source:<repo>#<n>`.

What this means for operators: a new harness failure on one project is diagnosed and filed as a heal issue, and the lineage cap still stops a failure that keeps coming back on the same item.
