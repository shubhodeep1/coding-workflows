<!-- changelog: added -->
- **An auto-heal remediation that changes no code no longer becomes a pull request.** The implement run stops before the commit and parks the issue for a person.

An auto-heal remediation issue asks for a code fix to a live alert, such as a slow route. Between 2026-11-10 and 2026-11-17 consumer tele-funtoken-msg-scoring merged remediation PRs that changed only README and tests (its PRs #5343, #5345, #5346 and #5347), which closed the alert without fixing it. `scripts/implement_commit_changes.sh` now checks the staged change set of an issue whose body has a line that opens with `<!-- auto-heal-remediation:v1` (outside a code fence). When every staged path is a doc, test, fragment or DB contract path, nothing is committed or pushed, no PR is opened and no review runs. The issue gets `ai:scope-blocked` and one comment that lists the staged paths and says what to do next. Other issues, and remediations that change at least one code file, are unaffected.

| The numbers that matter | Value |
| --- | --- |
| Paths that do not count as code | `*.md`, `tests/**`, `docs/**`, `changelog.d/**`, `agents.d/**`, `db/contracts/**` |
| New repository variable | `REMEDIATION_NO_CODE_GUARD_ENABLED`, default `true` |
| New log line | `IMPLEMENT_REMEDIATION_NO_CODE_GUARD outcome=allow\|block reason=<token> issue=<n>` |
| Extra GitHub API calls | 0 (the guard reads the issue body the run already fetched) |
| Workflow failure heal report for a parked remediation | skipped once the park comment is posted (`skip reason=remediation_no_code_park`) |

What this means for operators: a parked remediation needs a decision from you. If the alert needs a code fix, say in the issue which code to change, remove `ai:scope-blocked` and redispatch. If it needs no code change, close the issue by hand. Set `REMEDIATION_NO_CODE_GUARD_ENABLED=false` to turn the guard off for a repository.

### For contributors

The decision is `evaluate_remediation_no_code` in `scripts/files_touched_scope_guard.py` (CLI `--remediation-no-code-check`, exit 40 to block). The guard fails open: a missing body file, a missing or crashing helper, the heal route, an empty staged set and a run that started from a prior PR's branch (which may already hold the code change) all allow the commit. A parked remediation is still visible to the unblock judge like any other `ai:scope-blocked` issue. The rejection reuses the `scope_violation_blocked` step output with the new value `remediation-no-code`, so `implement.yml` gained one `env:` line and no step or job. The handler's marker is `guard=scope reason=remediation-no-code`, which the unblock ledger never offers `override_guard` for. The review gate is unchanged: it does not read the linked issue's body, and a PR that was already open before this change is still reviewed as before.
