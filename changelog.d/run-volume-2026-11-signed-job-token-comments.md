<!-- changelog: added -->
- **Opt-in signed job-token comments: pipeline comments that cannot start work stop creating four comment-wrapper runs each.** Set the repository secret `AI_COMMENT_SIGNING_SECRET` to turn it on; with it unset nothing changes.

Every comment posted with `GH_PAT` starts `internal-implement.yml`, `internal-clarify.yml`, `internal-plan.yml` and `internal-orchestrate-clarify-respond.yml` (the `ai-*` copies in consumers), and almost all of those runs skip within seconds. Readers trust most pipeline comments only when the `GH_PAT` login wrote them, so they could not simply move to the job token. With the secret set, `scripts/gh_helpers.sh` routes `gh` through a shim (`scripts/comment_signature.py`). The shim signs each comment that cannot start a wrapper, posts it as `github-actions[bot]` (which starts nothing), and on every comments read turns a validly signed comment back into exactly what the `GH_PAT` would have written. Readers need no change. Comments that start work (`/approved`, `/answer`, `/reclarify`, `Clarification required`) and plan comments stay on `GH_PAT`. An unsigned or forged bot comment stays untrusted.

| The numbers that matter | Value |
| --- | --- |
| Comments in coding-workflows, 2026-11-15 23:47 to 2026-11-16 23:47 UTC | 687, of which 622 posted with `GH_PAT` |
| `issue_comment` runs in that window | 2,500, of which 59 did work |
| `GH_PAT` comments that start work and stay on the PAT | about 55 a day (plus plan comments) |
| Runs this removes in coding-workflows at that volume | up to about 2,270 a day (91%) |
| Extra API calls | one `GET /user` per job; one `GET issues/comments/<id>` per edit of a signed comment |

What this means for operators: create `AI_COMMENT_SIGNING_SECRET` in coding-workflows. The weekly `Propagate consumer secrets` run (or a dispatch of it) copies the same value to every consumer, and the consumers pick up the new template lines on their next `@stable` sync. To roll back, set the repository variable `AI_COMMENT_SIGNING_POST=false`: new comments go out with `GH_PAT` again, and comments already signed keep verifying. Do not delete the secret to roll back, because every signed comment, orchestrator state included, would then read as untrusted.

### For contributors

The signer is embedded in `scripts/gh_helpers.sh` and written to `RUNNER_TEMP` on activation, so it travels wherever the helpers are staged. `tests/test_comment_signature.py` fails when the embedded copy and `scripts/comment_signature.py` differ. Inside Actions the first activation adds the shim to `GITHUB_PATH`, so later steps and Python scripts run from a step go through it. `tests/conftest.py` strips the signing variables for the test session. Comment calls made with `curl` and steps before the first `gh_helpers.sh` source in a job keep the PAT.
