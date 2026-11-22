<!-- changelog: changed -->
- **The review sweep stops re-dispatching an unapproved PR after 10 review rounds on its current head commit within 48 hours, and generated chore PRs no longer go through review or the security pass.** A PR at the limit gets `ai:needs-human` and one comment instead of another round; a new head commit starts the count again.

`.github/workflows/review_autofix_sweep.yml` now counts each PR's dispatched `Internal: AI Review & Autofix` runs from one listing per tick (`scripts/review_sweep_round_cap.sh`). A PR whose head is not approved and that has `SWEEP_REVIEW_MAX_ROUNDS` or more rounds inside `SWEEP_REVIEW_ROUND_WINDOW_HOURS` is skipped with `AUTOFIX_SWEEP_SKIP ... reason=review_round_cap`, labelled `ai:needs-human` and told why in a single comment. Only rounds created since the PR's current head commit (its committer date) count, so a new head commit starts the count again and a PR that keeps getting new commits is not capped. The autofix and judge-fix limits bound autofix and `[judge-fix]` commits, after which the rounds pile up on one head and the cap applies, but they do not bound `[ai-merge-resolve]` conflict commits or base-sync `update-branch` merges: a PR whose head keeps moving that way can stay uncapped. A new push is still reviewed, a manual dispatch still works, and the sweep resumes by itself when the window has room again. Separately, the review gate (`scripts/review_autofix_step_evaluate_gate.sh`) sends two kinds of generated PR straight to `deterministic-skip-merge`: the nightly validation self-test status and the workflow log analysis report. It decides by head branch, changed paths and author together, so a hand-written PR with the same title is reviewed as before. Forward-merge and wrapper-sync PRs are not exempt.

| The numbers that matter | Value |
| --- | --- |
| `Internal: AI Review & Autofix` runs, 2026-11-10 to 2026-11-17 | 3,778 over 398 PRs (2,430 dispatched) |
| Runs per PR | median 4, 90th percentile 26, maximum 81 (PR #9178) |
| `SWEEP_REVIEW_MAX_ROUNDS` / `SWEEP_REVIEW_ROUND_WINDOW_HOURS` | `10` / `48` |
| `SWEEP_REVIEW_ROUND_CAP_ENABLED`, `AUTOFIX_SKIP_GENERATED_CHORE` | `true`, `true` (`false` turns each off) |
| Extra API calls per sweep tick | 1 paginated listing, about 7 pages at the default window |
| Dispatched review rounds on status PR #9570 before it merged | 10 in 26 hours |

What this means for operators: a PR labelled `ai:needs-human` with a comment ending `<!-- ai:review-round-cap:v1 -->` has used its review rounds and needs a person to fix or close it. Removing the label alone does not resume the sweep while the PR is over the limit, and the label stays until a person removes it. An approved head waiting for CI or a merge is never capped. Status and report PRs now merge after their CI without reviewer or security-audit runs. Consumer repos receive the gate change on their next `@stable` sync; the sweep runs in coding-workflows only.

### For contributors

The cap counts dispatched runs from every dispatcher but only the sweep enforces it, so the poller, the security pass and the merge train can still dispatch a capped PR. `tests/test_review_sweep_round_cap.py` and `tests/test_review_gate_generated_chore_skip.py` cover both changes; the `tests-system-python` catch-all job runs them, so `ci.yml` is unchanged.
