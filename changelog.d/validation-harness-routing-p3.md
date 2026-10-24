<!-- changelog: fixed -->
- **The unblock judge now sends the harness heal report it plans, instead of dropping it.** Refs #7913.

When the unblock judge decided that a blocked item's fix belongs in the shared workflow harness, it filed no fix-up but also sent nothing, because the step that should send the report was missing. The item said a report was on its way, and nothing arrived.

The judge now sends one workflow failure heal report to coding-workflows for such an item, with the item's escalation label, the repository's recent failed runs and the run's identity. For a project's item it then asks the poller to hold the project until the harness fix ships. If the report cannot be sent, no hold is requested and a WARNING is sent.

What this means for operators: harness problems found by the unblock judge now reach coding-workflows as heal issues. Each judged verdict logs one `UNBLOCK_TARGET_REPO_GUARD` line saying where its work went. Set `UNBLOCK_JUDGE_TARGET_REPO_GUARD_ENABLED=false` to restore the old behaviour, or `WORKFLOW_HEAL_ENABLED=false` to skip the report; you then get a WARNING telling you to file the harness change yourself.
