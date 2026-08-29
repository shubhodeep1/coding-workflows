<!-- changelog: fixed -->
- **AI Plan no longer rejects plans that change a consumer repo's own `scripts/` files.** Only runtime-fetched script paths are blocked now, so validation fix-up projects can proceed instead of failing before the plan is posted.

The `Guard plans targeting template-owned paths` step in `plan.yml` rejected every path under `scripts/`, on the stale premise that `implement.yml` excludes that whole directory from Codex commits. `implement.yml` stopped doing that: it builds per-file exclusions for runtime-fetched helpers so consumer repos can commit their own `scripts/` changes. The mismatch deadlocked any project touching `scripts/run_validation_repo_checks.sh`, the validation entry that `validation_template_bootstrap.py` seeds into consumer repos and `.ai/validate.yml` points at, so a repo could never plan a fix for a file this library told it to own. The guard now derives the implementation script paths from the staged `implement.yml`, unions them with the plan job's fetched helpers, and falls back to the old blanket rejection if either source is unavailable or unparseable.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/plan.yml` |
| Still blanket-rejected | `prompts/`, `ai-memory/`, `.github/prompts/`, `.github/scripts/` |
| New test file | `tests/test_plan_template_owned_path_guard.py` |
| Guard tests added | 57 |

What this means for operators: a plan that lists a consumer-owned file under `scripts/` now proceeds to implementation instead of failing the AI Plan run with a template-owned-path error. Nothing the guard previously caught is let through, so plans targeting fetched helpers such as `scripts/render_prompt.sh` are still rejected with the same routing back to this repository.

### For contributors

The guard parses the staged implementation workflow's helper-staging block, including literal `scripts/...` entries written to `FETCHED_MANIFEST`, rather than carrying another copy of that list, then unions those names with the plan job's own `scripts/.gitignore` entries. It mirrors the implementation workflow's ownership exception for a consumer-tracked Serena template while still rejecting an untracked runtime copy, and preserves its blanket exclusions for `prompts/`, `ai-memory/`, `.github/prompts/`, and `.github/scripts/`. `EXCLUDED_RE` is kept as the pre-filter before path extraction; trailing sentence punctuation, redundant path separators, and lexical path segments are normalized before directory classification and exact helper matching. The guard had no test coverage before this change; the consumer-owned and implement-only-helper regression cases fail against the previous body.
