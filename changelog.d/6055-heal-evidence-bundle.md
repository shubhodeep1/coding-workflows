<!-- changelog: added -->
- **Workflow-heal issues now carry the failing runs' logs, artifacts and provenance to the agents that fix them.** Clarify, plan and implement add an evidence folder for every trusted `ai:workflow-heal` issue, and the heal intake reads review/autofix runs it used to skip.

The heal pipeline's agents cannot open GitHub Actions logs: the web UI needs a sign-in and the clarify sandbox has no token. A survey of the 51 heal issues opened between 2026-09-21 and 2026-10-04 found most diagnoses resting on guesses. Issues #4412, #4416, #4487 and #6055 blocked on "logs are sign-in only", and the intake had read no log at all for 8 review/autofix reports (`runs=0`), because those review runs conclude success. A trusted step in `clarify.yml`, `plan.yml` and `implement.yml` now runs `scripts/workflow_failure_heal_evidence.py collect` before the agent. It writes step-sliced job logs, the diagnostic files of the review artifacts, the source PR's state, the failing head against `main` and the target branch, the earlier heals of the same lineage with whether each fix reached `main`, the other runs on the failing head, and the GitHub rate limit and OpenRouter key balance. The prompt gets `=== WORKFLOW HEAL EVIDENCE (UNTRUSTED) ===`, and clarify's sandbox gets a read-only copy at `/evidence`. Review/autofix heals for pull requests in coding-workflows now target the branch the review's support scripts came from (`main` or `stable`) instead of the pull request's head branch, which had stranded the same fix twice (#4478, #4585) on branches that never reached `main`.

| The numbers that matter | Value |
| --- | --- |
| Heal issues surveyed | 51 (49 closed, 2 open) |
| Reports the intake read with no log (`runs=0`) | 8, now read through the review job |
| Evidence folder cap | 400 KB in total, 60 KB per file |
| GitHub REST calls | about 20 for the first stage, about 5 for later stages |
| Optional parts skipped below | 500 remaining core calls |
| Real #6055 review log | 2,046,723 bytes sliced to 48,140, keeping the env and working-tree checkpoints that show the cause |

What this means for operators: heal issues should stop blocking on missing logs, and a heal fix for a review/autofix failure in coding-workflows now lands where the failing code lives. The intake's fingerprints are unchanged, so open lineages continue. Nothing new needs configuring; every part of the evidence step fails open and records what it skipped in `manifest.json`.

### For contributors

`slice-log` replaces only what the diagnosis prompt reads; `filter_log` still feeds the fingerprint. Run links come from the issue's `runs=` marker, its `**Failed run:**` lines and occurrence comments by the issue's own author, limited to the issue's and the source repository. Finished runs are cached between stages with actions/cache (`heal-evidence-<issue>-<run>-<attempt>`); a run with a failed fetch is fetched again by the next stage. A `script_ref` equal to the pull request's head SHA still targets the pull request's branch. Log prefix: `WORKFLOW_HEAL_EVIDENCE`. Tests: `tests/test_workflow_failure_heal_evidence.py` and the new targeting and review-job cases in `tests/test_workflow_failure_heal.py`.
