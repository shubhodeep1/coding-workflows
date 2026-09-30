<!-- changelog: security -->
- **Reviewer rejection votes can no longer clear a single-reviewer finding on a `claude/*` PR.** However many reviewers reject it, and however well their quotes verify, the finding is still handed to the Claude fixer.

Security audit finding #5582 showed that the Claude-fixer hand-off gate in `scripts/review_claude_fixer_nonblocking.py` treated a quote copied from the reviewed file as proof that a finding was false. A PR author could prompt-inject pass-2 reviewers into copying the run's finding ID and nearby source text, even text that shows the defect, into `REJECTED_FINDING` votes. A majority of such votes moved the last finding to the `NON-BLOCKING FINDINGS` block, and `review_autofix.yml` auto-merged the PR. Votes are now necessary but never sufficient. The gate demotes a finding only when an independent automated check also proves it false, and the hand-off step (`scripts/review_autofix_step_claude_fixer_handoff.sh`) has no such check. So every single-reviewer finding stays blocking and reaches the Claude fixer, which checks it against the code and answers an invalid one through the verdict bot and a fresh reviewer panel.

| The numbers that matter | Value |
| --- | --- |
| Findings demoted by the hand-off step | 0 (`CLAUDE_FIXER_NONBLOCKING demoted=0` on every run) |
| New keep reason | `CLAUDE_FIXER_NONBLOCKING_KEPT file=<file:line> flagged_by=<slug> reason=no_automated_proof` for an entry that met every vote condition |
| Unchanged | vote parsing, run IDs, `consensus_id` binding, evidence checks, and the `_VOTES` / `_EVIDENCE` / `_UNVERIFIED` log lines |

What this means for operators and Claude sessions: expect more review rounds on `claude/*` PRs. A single-reviewer false positive that the other reviewers rejected now reaches the Claude session, which rejects it with a reason instead of the gate auto-merging past it. The run log still shows how each reviewer voted.

### For contributors

`demote` and `demote_with_diagnostics` take an optional `disproof_check`. It is called with a copy of the candidate record (`entry`, `path`, `lines`, `flagger`, `consensus_id`, `rejecters`, `others`), and only an exact `True` demotes. It runs after every vote condition, so the earlier keep reasons are still reported first. No production caller passes one. A future automated disproof plugs in there. The pass-2 cross-pollination header in `scripts/review_run_reviewers.sh` no longer tells reviewers that a rejected finding skips the fixer.
