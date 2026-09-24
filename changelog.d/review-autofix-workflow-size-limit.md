<!-- changelog: fixed -->
- **`review_autofix.yml` is back under GitHub's 512,000-byte workflow file limit, so pushes stop creating phantom failed runs and the stable release gate can pass again.**

#4327 grew `.github/workflows/review_autofix.yml` to 540,537 bytes. GitHub does not start runs for a workflow file over 512,000 bytes, and it reports no error: every push to any branch created a zero-job run named `.github/workflows/review_autofix.yml` that concluded `failure` with "workflow file issue". Phase 4 of `test-and-mark-stable.yml` matched that run on the pinned head SHA, accepted it as a finished review before any editor had run, and Phase 4b then failed with `retry_timeout` (stable gate run 35903885958). The five largest step bodies now live in `scripts/review_autofix_step_*.sh` and the steps source them unchanged. Phase 4 also ignores runs named after their own file path. A new CI guard fails any workflow file that reaches 480,000 bytes.

| The numbers that matter | Value |
| --- | --- |
| GitHub workflow file limit (measured with padded probe workflows) | 512,000 bytes run, 512,001 bytes do not |
| `review_autofix.yml` before / after | 540,752 / 425,368 bytes |
| CI guard in `tests/test_workflow_file_size_limit.py` | fails at 480,000 bytes |
| Steps moved to `scripts/` | 5 (merge-topology gate, editor-uncommitted check, merge-conflict detection, partial finalize comment, iteration summary) |

What this means for operators and consumer repos: the phantom `review_autofix.yml` runs stop with this change, and the promote-cycle smoke gate on `main` no longer latches onto them. The moved steps keep their names, ids, conditions, env and log lines. Consumer runs pick up the five new scripts through the existing support staging on the next `@stable` sync, with no wrapper change.

### For contributors

A moved step's `run:` block resolves its script from `${SUPPORT_SCRIPTS_DIR}`, then `.codex-workflow-src/scripts`, then `.codex-workflow-src-main/scripts`, and sources it in the step shell. The main-snapshot fallback covers self-repo PR branches forked before the move, because those reviews run `main`'s YAML against the branch's own scripts. A missing script fails the step, except in the two `always()` steps, which skip with a warning. Contract tests read the moved bodies through `tests/review_autofix_step_scripts.py`, which inlines them again. The expanded text matches the pre-move workflow exactly. When a workflow reaches the 480,000-byte guard, follow the split rule in `agents.md` under "Workflow file size limit".
