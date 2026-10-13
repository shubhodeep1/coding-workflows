<!-- agents: section="Workflow architecture" -->
Issue #7251 dependency wait (unblock fix-up #7257, temporary; registry entry
in `docs/scripts-pending-removal.md`): #7251 waits for PR #6667 to merge into
the default branch. The `issue_7251_*` helpers in `scripts/gh_helpers.sh`
apply only to #7251 in `shubhodeep1/coding-workflows` (no API call otherwise)
and read one aliased GraphQL snapshot of the PR, the default branch and the
issue's last 100 comments. While #6667 is open (or the read fails), and
after it merges or closes until the poller's trusted outcome marker below is
on #7251 (`detail=merged_pending|closed_pending`), `clarify.yml` "Decide
clarify route" and `plan.yml` "Validate planning phase label" skip with
`AI_PHASE_GATE_V1 ... gate=issue_7251_dependency_wait reason=pr_6667_unmerged`
and write no labels. The poller's `run_issue_7251_dependency_wait` runs once
per process from the top of `run_standalone_stall_recovery` and
`run_unblock_scan` (so the full tick and both the `UNBLOCK_SCAN_SWEEP_ONLY`
and `STANDALONE_RECOVERY_SWEEP_ONLY` ticks run it) and writes
`${RUNTIME_DIR}/issue_7251_dependency_wait_hold` so the standalone stall loop
and the unblock scan skip #7251. After the
merge it greps the default-branch `scripts/ai_engine.sh` for
`_AI_ENGINE_EXIT_REFUSED` and `ai_engine_fallback_class`: both present posts
`/reclarify` with `<!-- ai:issue-7251-dependency-released:6667 -->`; any
missing posts `<!-- ai:issue-7251-preflight-failed:6667 -->` and one
Telegram WARNING. A close without merge adds `ai:blocked` and posts
`<!-- ai:issue-7251-dependency-closed:6667 -->`. Markers count only from
OWNER/MEMBER/COLLABORATOR comments. Off switch
`ISSUE_7251_DEPENDENCY_WAIT_ENABLED` (unset/empty/`true` on, `false` off,
anything else off with a warning). Logs `STALL_SKIP issue=7251
reason=dependency_wait detail=open|merged|closed_unmerged|unverified|issue_closed`.
