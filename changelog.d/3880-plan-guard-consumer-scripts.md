<!-- changelog: fixed -->
- **AI Plan no longer rejects plans that change a consumer repo's own `scripts/` files.** Only runtime-fetched helper scripts are blocked now, so validation fix-up projects can proceed instead of failing before the plan is posted.

The `Guard plans targeting template-owned paths` step in `plan.yml` rejected every path under `scripts/`, on the stale premise that `implement.yml` excludes that whole directory from Codex commits. `implement.yml` stopped doing that: it builds per-file exclusions from the runtime-generated `scripts/.gitignore` so consumer repos can commit their own `scripts/` changes. The mismatch deadlocked any project touching `scripts/run_validation_repo_checks.sh`, the validation entry that `validation_template_bootstrap.py` seeds into consumer repos and `.ai/validate.yml` points at, so a repo could never plan a fix for a file this library told it to own. The guard now rejects a `scripts/` path only when it names a helper listed in `scripts/.gitignore`, and falls back to the old blanket rejection if that file is missing.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/plan.yml` |
| Still blanket-rejected | `.github/prompts/`, `.github/scripts/` |
| New test file | `tests/test_plan_template_owned_path_guard.py` |
| Guard tests added | 10 |

What this means for operators: a plan that lists a consumer-owned file under `scripts/` now proceeds to implementation instead of failing the AI Plan run with a template-owned-path error. Nothing the guard previously caught is let through, so plans targeting fetched helpers such as `scripts/render_prompt.sh` are still rejected with the same routing back to this repository.

### For contributors

The guard reads `scripts/.gitignore` rather than carrying its own copy of the fetched-helper list, because duplicating that list across `plan.yml` and `implement.yml` is how the two drifted apart. That file is already written by the `Stage workflow support files` step earlier in the same job, under the same non-self-repo condition the guard uses. `EXCLUDED_RE` is kept and now serves as the pre-filter before path extraction. The guard had no test coverage before this change; the two consumer-owned cases in the new test fail against the previous body.
