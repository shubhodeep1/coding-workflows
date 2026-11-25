<!-- changelog: fixed -->
- **A clean review now clears a stale `ai:review-blocked` label from a PR.** The review/autofix identical-failure cap labels a PR that has no linked issue, and no review path ever removed that label again, even after the cause (for example the Python 3.14 `render_prompt.py` crash) was fixed and the re-dispatched review came back clean. PR #9218 and PR #9229 stayed blocked until the label was removed by hand.

After the editor summary posts a `review-autofix-clean:v1` marker, `scripts/review_autofix_step_post_editor_summary.sh` removes `ai:review-blocked` from the PR when the newest own-line cap marker posted by the pipeline's own login is for another head, or for another review-support SHA, and says it labelled this pull request. A newer marker from another author does not hide that trusted marker. No cap marker, the same head and support, a cap that labelled linked issues instead, or an untrusted author keeps the label. Labels and comments come from the run's existing metadata snapshot. A failed removal logs `outcome=error` and the step still succeeds. Each run that writes the clean marker logs one `REVIEW_BLOCKED_STALE_CLEAR` line.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls on a normal clean run | 0 |
| Extra API calls when the label is stale | 2 (`GET /user`, one label DELETE) |
| Workflow YAML lines added | 0 |

What this means for consumer repos: after the next `@stable` sync, a PR left with `ai:review-blocked` by a harness-caused identical-failure cap gets the label back off on its next clean review. Labels on linked issues are unchanged and still follow the phase-label path.
