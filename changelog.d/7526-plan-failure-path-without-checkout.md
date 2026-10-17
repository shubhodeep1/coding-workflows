<!-- changelog: fixed -->
- **A plan run stopped by the integration-ref resolver no longer adds misleading errors on its way out.** "Resolve integration ref" runs before "Checkout repository", so when it refuses an issue there is no `scripts/` tree and no `RUNTIME_DIR`. "Complete /answer command (failure/cancel)", "Record planning run failed", "Revert label on planning failure" and "Telegram failure notification" now skip with a `::warning::` naming the missing helper, and "Write run summary" writes its table without the token row instead of failing on `RUNTIME_DIR: unbound variable` (run 38989786896). The failure comment still posts and the run still fails.

Correction to #7513: its resolver skip covers only an `ai:orchestrator-managed` issue whose body has a `Tracking issue: #N` line. #7511 carries `ai:orchestrator-managed` but has neither that line nor `ai:check-triage`, so it is still refused with `label_missing`, as designed; #7513 did not unstick it.

What this means for operators: nothing to set. #7511 needs a person to restore `ai:check-triage` (and remove `ai:orchestrator-managed`) or to add a tracking line; #7516 tracks what swapped its labels.
