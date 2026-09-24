<!-- changelog: added -->
- **Review/autofix now checks the editor's preconditions before the reviewers run.** A run whose staged `scripts/review_apply_fixes.sh` would fail a required-variable guard now fails in the preflight step within seconds, instead of after a full reviewer and consolidator pass.

The "Preflight: Verify required files before reviewer invocation" step of `review_autofix.yml` now runs `review_apply_fixes.sh --preflight` after its file checks. The new `review_apply_fixes_preflight()` function checks every `: "${VAR:?…}"` guard the editor script declares, the OpenCode helpers and config writer, the `opencode` binary, and write access to `RUNTIME_DIR`, with no model or network call. It logs one `REVIEW_EDITOR_PREFLIGHT check=<name> result=<ok|fail>` line per check and a final `REVIEW_EDITOR_PREFLIGHT result=<ok|fail> checks=<n> failed=<m>`. A failure sets `EDITOR_PREFLIGHT_FAILED=true` and takes the ordinary failure path: failure comment with its fingerprint marker, `ai:review-blocked` on the linked issues, and a heal report with `failure_reason=editor_preflight_failed`. `scripts/stage_workflow_support.sh` also logs `::notice::STAGE_MAIN_PINNED_DIVERGENCE script=<name> script_ref=<ref>` when a main-pinned support script's branch copy differs from the `main` copy it stages instead.

| The numbers that matter | Value |
| --- | --- |
| Time to fail on a broken editor guard | seconds, before reviewers (PR #4259 spent about 75 minutes per run, seven runs) |
| New failure reason | `editor_preflight_failed` |
| New log prefixes | `REVIEW_EDITOR_PREFLIGHT`, `STAGE_MAIN_PINNED_DIVERGENCE` |
| New GitHub API calls | 0 |
| Kill switch | `REVIEW_EDITOR_PREFLIGHT_ENABLED=false` |

What this means for operators: a deterministic editor precondition failure, like the unset `OPENROUTER_API_KEY` on PR #4259, now costs one short run per head, and the identical-failure fingerprint cap stops the retries after three. The preflight is skipped (`skip reason=unsupported`) when the staged editor script predates `--preflight`, and (`skip reason=editor_not_scheduled`) in Claude-branch review mode or a terminal resume, where the editor does not run.

### For contributors

Adding a `: "${VAR:?…}"` guard to `scripts/review_apply_fixes.sh` now requires the same name in `preflight_required_vars` inside `review_apply_fixes_preflight()`; `tests/test_review_autofix_review_pipeline_contract.py` fails otherwise. The variable must be set when the preflight step runs, because that step sees the job env plus the editor step's explicit env entries (`GH_TOKEN`, `REPOSITORY`, `TOOL_CALL_BUDGET_JUDGE`). A second contract test compares each `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` entry with its `origin/main` copy and fails when the branch copy writes a `${RUNTIME_DIR}/…` or `${…_FILE}` output the main copy does not (the PR #4273 class). It fetches `origin/main` only when the ref is missing and skips with a warning when it cannot.
