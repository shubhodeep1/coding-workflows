<!-- agents: section="Isolated Codex agents" -->
- **Commit identity follows the engine.** A pipeline commit that a model
  wrote names the engine that wrote it: `claude-bot
  <claude-bot@users.noreply.invalid>` when the editing role ran on Claude and
  did not fall back, `codex-bot <codex-bot@users.noreply.invalid>` otherwise.
  The five AI writers decide this from what actually ran, not from the label
  alone:

  | Writer | Role | Claude identity when |
  |---|---|---|
  | `scripts/review_commit_changes.sh` (`[ai-autofix]`) | `REVIEW_EDITOR` | `AI_ENGINE_RESOLVED_REVIEW_EDITOR=claude` and `REVIEW_EDITOR_ENGINE_FELL_BACK` is not `true` (`scripts/review_apply_fixes.sh` writes `true` to `GITHUB_ENV` when a Claude attempt hands off to OpenCode and `false` when a later Claude attempt succeeds) |
  | `scripts/implement_commit_changes.sh` | `IMPLEMENT`, `IMPLEMENT_REPAIR` | both resolved to `claude` and neither `claude-implement.unavailable` nor `claude-implement-repair.unavailable` exists under `${CODEX_THREAD_REUSE_RUNTIME_DIR:-$RUNTIME_DIR}/codex-thread-reuse/states` (`codex_thread_reuse.sh` writes them, and on the heal route `scripts/heal_isolated_implement.sh` writes `claude-implement.unavailable` when a Codex-written snapshot is transferred to the host, and warns `outcome=marker_write_failed` when it cannot) |
  | `scripts/review_conflict_resolve.sh` (`[ai-merge-resolve]`) | `CONFLICT_RESOLVER` | the last model attempt ran on Claude (`resolver_commit_engine`) |
  | `scripts/review_rb_judge.sh` (`[judge-fix]`) and the poller's combined review-blocked judge (`scripts/orchestrate_poll_process.sh`) | `RB_JUDGE` | the fix pass did not return 75; the poller reads `${RUNTIME_DIR}/rb_judge_last_engine`, which `poller_judge_isolated` writes |

  Every other pipeline commit (merges, memory records, stall-recovery empty
  commits, integration-judge merges, heal reconciles) stays `codex-bot`. Both
  emails are on the reserved `.invalid` domain, so GitHub links these commits
  to no account and `.author.login` / `.committer.login` read empty. The old
  `codex@users.noreply.github.com` linked every pipeline commit to the
  unrelated GitHub account `codex`. Checks that recognise pipeline commits
  accept the legacy pair and both new pairs: the review-blocked external-head
  classifier (by name or email when the login is empty) and the noop
  force-merge gate (`NOOP_PRODUCTIVE_COMMIT_IDENTITIES_JSON`, exact name and
  email pair). The login-based checks keyed on `AUTOFIX_BOT_LOGIN` (the
  opt-in `AUTOFIX_SKIP_SELF_TRIGGERED` skip and
  `scripts/check_external_branch_advance.sh`) see no login on these commits
  and take their documented fail-open paths: review runs, and an advance is
  `ADVANCE=unknown`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `AI_COMMIT_IDENTITY` (`scripts/review_commit_changes.sh`: `role=REVIEW_EDITOR engine= fell_back= name=`; `scripts/implement_commit_changes.sh`: `role=IMPLEMENT engine= repair_engine= commit_engine= name=`; `scripts/review_conflict_resolve.sh`: `role=CONFLICT_RESOLVER engine= name=`; `scripts/review_rb_judge.sh`: `role=RB_JUDGE engine= name=`; `scripts/orchestrate_poll_process.sh`: `role=RB_JUDGE scope=poller engine= name=`. `name=` is `claude-bot` or `codex-bot`, the git author of the commit that follows)
LOG_PREFIX.name=AI_COMMIT_IDENTITY
