# Implement-Plan Log — Usage-limit resumes: only resume sessions the pickup's workflows started

- Plan: docs/completed/issue-6101-authorize-usage-limit-resumes-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#6101 (progress comment 5956414439)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5660-resume-usage-limit-stops
- Project branch: claude/implement-plan-issue-6101-authorize-usage-limit-resumes   Final PR: #6106 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (this PR)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01DV4YiX8EogPZxbF41aN7P7 (reused); safety net and hand-back ids in the validation 1/3 — read result stage report
- Last updated: 2026-10-02
- Last note: validation cycle 1 (run 37067368025, project head d932a9c) passed 10/10; completion PR moves the plan to docs/completed/; next stage final-merge 1/1 marks final PR #6106 ready (session_01S7TqLGpAf3y1KBEUwm73uZ)

## Phases
1. [x] Phase 1 — authorize usage-limit resumes by session source, origin, and lineage   — protected paths: .claude/scripts/usage_limit_resumes.py (via workflow-templates/.claude/** twin); .claude/commands/claude-issue-pickup.md (no twin — diff in the sync blocker)   — PR #6108 merged 2026-10-02T20:44:07Z (merge d932a9c; twin sync 00ae9ca; round-1 fix e66e22c, twin sync 92714eb; round 2 on 92714eb: 0 defects); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-10-02: CONFORMANT — no fixes (pre-validation; security skipped). Implemented COMPLETE, Correctness PASS: every Goal maps to code (workflow-templates/.claude/scripts/usage_limit_resumes.py:158-163 constants, :296 load_allowed_repos, :316 session_repos, :340 _unauthorized_reason, :499 wired after `pickup`, :612-613 flags with defaults, :636 main builds the set); twin and .claude copy byte-identical; tests/test_usage_limit_resumes.py 162 passed, janitor/stale-routines/implement-issue/changelog suites 214 passed, implement-plan/session-titles 59 passed (1 skipped), ruff clean; real-data run on the live 100-session listing: all 66 claude_code_mcp_seed sessions authorized, 32 desktop_app unknown_origin, 2 desktop_app foreign_repo. Non-finding: an unreadable or non-GitHub git_repository URL is labelled foreign_repo, not no_repo (the plan's two descriptions overlap; the module docstring defines it, review round 1 chose it; the session is skipped either way)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified it)

## Validation
- Cycle 1 — run 37067368025 2026-10-02 (target_ref: claude/implement-plan-issue-6101-authorize-usage-limit-resumes, authorized head d932a9c): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 274s); no fix issues

## Completion
- Completion PR (claude/implement-plan-issue-6101-authorize-usage-limit-resumes-complete) open 2026-10-02 — doc moved to docs/completed/issue-6101-authorize-usage-limit-resumes-plan.md
- Final PR #6106 draft — marked ready at final-merge 1/1 (base claude/implement-plan-issue-5660-resume-usage-limit-stops)

## Activation
- Expected: n/a (base claude/implement-plan-issue-5660-resume-usage-limit-stops) — the change goes live with the #5660 project's final PR #5678; recorded at final-merge, which closes #6101 with `ai:merged`

## Auto-decisions
- AD-1 [plan, 2026-10-02] How are resumes authorized? — Picked: A — every GitHub source in coding-workflows or the consumer registry, `origin` = `claude_code_mcp_seed`, and a `parent_session_id`; fail closed. Alternatives: B — also require the parent chain to reach the pickup session; C — repository check only. Why: server-set fields, no API call; B would stop resuming chains rooted at an operator session or a previous pickup and ancestors older than the 72-hour listing; C still resumes a person's own Auto-mode session in the same repository. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Where does the allowed repository set come from? — Picked: A — optional `--registry` (default `.github/ai/consumer_repos.json`) plus `--self-repo` (default `shubhodeep1/coding-workflows`); an unreadable registry narrows to the self repo and adds an `errors` line. Alternatives: B — new required arguments; C — only the pickup's own `get_session` sources. Why: the pickup's command line keeps working; C would drop the consumer-repo sessions the pickup starts; failing narrow keeps §1. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Must the session's `environment_id` match the pickup's? — Picked: A — no. Alternatives: B — yes. Why: §5; repository, origin, and lineage close the finding. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-02] Does `.claude/commands/claude-issue-pickup.md` change? — Picked: A — one doc line in step 1a.2, as a diff in the twin-sync blocker. Alternatives: B — leave it. Why: §7 keeps the operator text accurate. Applied in: twin-sync blocker. Status: pending review
- AD-5 [plan, 2026-10-02] What does `select` do when called without the allowed set? — Picked: A — `None` authorizes nothing (fail closed). Alternatives: B — self repo only; C — no check. Why: a caller that forgets the argument must not widen access. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-10-02] A session a person opened in the app (`origin` `desktop_app`, no parent) sometimes runs a workflow itself, such as the invoking session of a manual `/implement-plan-claude` (2 of 47 such sessions on the live 100-session listing). Is it resumed? — Picked: A — no; it is skipped as `unknown_origin` and resumed by hand. Alternatives: B — resume it when its title matches a workflow title pattern. Why: §1; titles are written by sessions and cannot authorize anything, and the person who opened the session can resume it. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1 — resume after round-1 twin sync, 2026-10-02] Commit the progress-log record of the round-1 twin sync to PR #6108's branch while its review run on 92714eb is in flight? — Picked: A — no; carry it in the resume block and the issue progress comment, and the next stage that pushes commits it. Alternatives: B — push a log-only commit now. Why: a log-only push supersedes the reviewer panel running on 92714eb and costs a round (#5660's AD-20). Applied in: no code change. Status: pending review
- AD-8 [phase 1/1 — review round, 2026-10-02] Review round 2 on head 92714eb (workflow round 1, ledger 466adb43…, run 37051666777) handed off with `findings=2`, but both entries are "Review summary" bullets (minimax-m3: "No new defects were found"; glm-5.2: "No remaining issues were found"); consensus findings and task gaps are empty and no check failed. `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is unset, so no verdict can be posted. How does the round close? — Picked: A — reply on the PR and push this log update (AD-7's deferred record plus this round) as the next head, which starts a fresh review round. Alternatives: B — hold the PR and stop BLOCKED until a verdict bot exists; C — an empty commit to restart the review. Why: the log commit was due anyway (AD-7); B never converges without a bot; an empty commit is never allowed; issue-4886 AD-12 and issue-4887 AD-13 closed zero-defect hand-offs the same way. Applied in: PR #6108 (this log commit). Status: pending review

## Lessons
- [source:intervention] A parser behind a fail-closed authorization check must treat every entry it cannot read (a non-object, or a present but null field) as unreadable and refuse, not skip it; skipping lets one readable entry authorize the whole record. (files: workflow-templates/.claude/scripts/usage_limit_resumes.py)
- [source:intervention] The Claude-fixer hand-off counts every `- ` bullet inside a reviewer block as a ledger entry, so a reviewer that writes its clean verdict as a "- Review summary:" bullet turns a zero-defect review into a findings hand-off; read the entries before judging, and with no verdict bot close such a round with the next real push, never an empty commit. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)
- Twin sync: 00ae9ca on the phase branch (2026-10-02 17:09Z), relayed by trigger trig_011fh92GLpoQjSZwejg3jbwf because the Claude issue pickup is stopped, so no `/reclarify` was posted
- Review round 1 (2026-10-02, head e38f334, ledger fa993529…): fixed — `session_repos` returned the readable repositories and skipped a non-object source or a `git_repository: null` entry, so one allowed source authorized a session whose other sources could not be read, contrary to the documented "anything the script cannot read fails closed"; both now return None (`foreign_repo`, as the existing `not a url` case does), an object with no `git_repository` key is still another source kind and skipped. Rejected — the `SOURCE_URL_PATTERN` crash (the `?` makes the last `+` lazy, the group is not optional: `https://github.com/`, `https://github.com/.git`, and `https://github.com/owner` never match, so they fail closed as `foreign_repo`; tests now pin it), the `main` catch-all (premised on that crash), a distinct `unreadable_source` reason (the plan documents four reasons and `foreign_repo` for an unreadable URL), and binding the parent chain to the pickup (AD-1 B, a recorded non-goal)
- Real-data evidence (review round 1 task gap): this account's live 100-session listing (2026-10-02 18:20Z) carries 108 sources, all `git_repository` objects with an `https://github.com/<owner>/<repo>` URL: no bare-hostname URL, no `git_repository: null`, no non-object entry, and no other source kind, so the round-1 tightening changes no live session's verdict
- Twin sync round 1: 92714eb ([claude-twin-sync] issue-6101 review round 1, by the operator's driver session_01Db6EvqsPiaDHDTp8fUMeV8, answer A to blocker comment 5958673498, relayed by trigger trig_01SDBMPUgrkzHGDcfK3Y6ay5 at 2026-10-02 19:07Z) copies the twin to .claude/scripts/usage_limit_resumes.py; sha256 36de9839…520d0fb verified for both copies; 355 passed across the selector, implement-issue, stale-routine, and janitor suites on 92714eb; ai:claude-blocked removed from #6101 at 19:08Z
- Review round 2 (2026-10-02, head 92714eb, workflow round 1, run 37051666777, ledger 466adb43…): 6 of 6 reviewers succeeded; consensus findings and task gaps empty; the 2 ledger entries are minimax-m3's and glm-5.2's "Review summary" bullets, both stating no defects; no failed check. Nothing fixed or rejected; closed by this log commit (AD-8)
- Validation 1/3 — read result (2026-10-02, session_01S7TqLGpAf3y1KBEUwm73uZ): run 37067368025 conclusion success, `validation_status.json` status=pass raw_status=pass on authorized head d932a9c; no validation-fix PR, so no post-validation conformance re-run; issue base claude/implement-plan-issue-5660-resume-usage-limit-stops has not merged (no closed PR with that head); project branch already up to date with it
- Operator note: never archive session_01Db6EvqsPiaDHDTp8fUMeV8 (it holds the operator's hourly driver Routine for the #5660 project) and never delete that Routine
