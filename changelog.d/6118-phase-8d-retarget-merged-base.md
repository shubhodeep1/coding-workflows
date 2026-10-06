<!-- changelog: added -->
- **Work stacked on a branch whose PR already merged now goes to that PR's base.** `implement.yml` and the review gate stop targeting a finished branch.

Port P6 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8d). GitHub retargets a stacked PR on its own only when the merged branch is deleted. When the branch is kept, new work and open PRs used to keep pointing at it. The new `scripts/retarget_merged_base.sh` treats an existing branch as finished only when a merged PR has it as its head and the branch tip is still that PR's head commit, so a branch name reused for new work is never touched. A confirmed deleted head ref follows its merged PR's base too. `implement.yml` maps the issue's integration branch through it before checkout, and the review gate retargets an open PR whose base is such a branch with one `PATCH`.

| The numbers that matter | Value |
| --- | --- |
| New repository variable | `RETARGET_MERGED_BASE_ENABLED`, default `true` |
| Hops followed | up to 3 (`RETARGET_MERGED_BASE_MAX_HOPS`) |
| API calls when the base is the default branch | 0 |
| API calls per hop otherwise | 2 reads, plus 1 `PATCH` when a PR is retargeted |
| New log prefix | `RETARGET_MERGED_BASE` |

What this means for operators: a fix-up issue or PR that would have landed on a branch nobody merges any more lands on the live base instead. Deleted merged branches also resolve to their former base; transient ref lookup failures keep the original branch. After a PR base changes, review runs against the new base rather than trusting earlier skip or resume state; legacy partial markers without a base reference cannot suppress review. A missing optional helper checkout skips retargeting with a warning, while an identity mismatch fails the gate. Set `RETARGET_MERGED_BASE_ENABLED=false` to turn both paths off.

### For contributors

The review gate gets the helper through a sparse checkout from the same protected commit as `codex-agent`, verified like the fingerprint-cap helper, and reads the base from the PR fetch it already makes. `implement.yml` runs the helper from the staged clone that already provides `resolve_integration_ref.sh`. `tests/test_retarget_merged_base.py` uses a fake `gh` and runs in its own `ci.yml` step.
