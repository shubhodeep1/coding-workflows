# agents.md — Repo Architecture Facts (coding-workflows)

This file contains **repo-specific architectural facts** for any AI agent
(interactive Claude session, codex-cli unattended pipeline, third-party
reviewer model). Global engineering rules live in `CLAUDE.md` (interactive)
or `unattended_system_instructions.md` (unattended) — do not duplicate them
here.

Consumer repos define their own `agents.md` with their own architectural
facts. The unattended pipeline loads this file as `agents_canonical.md` and
the consumer's `agents.md` separately; both are inlined into the prompt.

The interactive merged-PR hook (`.claude/hooks/pr_merge_status_guard.py` and
its consumer template) recognizes guarded git commands after nested control
words, shell negation (`!`), and simple `case` arms. Since their effective
directory is uncertain, pushes check the session checkout and request
confirmation unless blocked; commits following unresolved directory-changing
commands also ask, while control-flow-only commits remain warning-only when
not blocked. An unresolvable explicit directory override, including an
env-wrapped commit whose directory cannot be resolved, instead asks for
confirmation without querying PRs for the session checkout, which may be a
different repository. Pipeline elements run in subshells, so a pipe leaves
the directory known for later commands and a `cd` inside a pipeline is
ignored; a `||` branch makes it unknown only after a directory change in the
same `&&`/`||` list (or when the branch is itself a `cd`), and `&` still does.
Heredoc bodies that Bash passes on as data (to `cat`, `python3`,
`git commit -F -`, ...) are removed before parsing, so prose such as `it's`
cannot make the whole command unparseable; a body fed to a shell reader
(`bash`, `sh`, `eval`, `ssh`, `sudo`, ..., also through a pipe on the
operator's line such as `cat <<EOF | bash`) or an unquoted-delimiter body
holding `$(...)` or a backtick is still parsed and checked. Wrappers such
as `env` or `timeout` count only through the command they run, `|&` is a
pipe like `|`, and a delimiter the hook cannot read in full (`<<EOF-1`,
`<<\EOF`) leaves every line parsed.

---

## Workflow architecture

Phases of the unattended pipeline (each is a separate workflow file under
`.github/workflows/`):

1. **clarify** (`clarify.yml`, `internal-clarify.yml`) — read the issue,
   decide whether clarifying questions are needed, emit `STATUS: CLEAR` or
   a `Q1`/`Q2` batch.
2. **clarify-respond** (`orchestrate_clarify_respond.yml`) — answer the
   clarifier's questions on orchestrator-managed and standalone issues.
   Both clarification workflows run the selected Claude or Codex engine through
   `scripts/clarify_isolated_run.sh`
   in a read-only, network-isolated container with a host-side model broker.
   They stage the helper and Dockerfile from the support ref (main fallback);
   isolation failures never fall back to host Codex. GitHub-side fetching,
   memory, retry, and comment handling remain on the runner.
    Host Python uses isolated imports; `CLARIFY_SOURCE_ROOT` selects the snapshot
    input directory (default `$PWD`), which is read only as data.
    Clarify and clarify-respond retain agent instruction files in their snapshots
    by default; triage alone opts out with `CLARIFY_SNAPSHOT_OMIT_AGENT_INSTRUCTIONS=true`.
    The unblock judge also runs both engines through this isolation boundary
    (Claude by default since plan item 3c, `UNBLOCK_JUDGE`);
    Claude's OAuth token stays in the host-side relay.
    Standalone clarify-respond skips semantic-cache lookup and storage (including
    SQLite restore/save) because live referenced PR, branch and run state is
    absent from the cache key. Orchestrator mode keeps the existing cache path.
   `/reclarify` triggers clarify from any line of a trusted comment (issue
   #6630). On a later line it counts only without an `<!-- ai:` marker or the
   plan-comment trailer, so marker-bearing automation comments (the plan
   comment carries `<!-- ai:plan-proposal:v1 -->`) restart clarify only when
   the command is on their first line. The job predicate, the route and
   requeue steps, the poller's replay sweep (`RECLARIFY_COMMAND_JQ_DEF`) and
   `orchestrate_lib.is_reclarify_command_body` apply the same rule. The
   sweep-only poller tick also flags open `ai:blocked` issues whose newest
   trusted human reply was never routed (`detect_unrouted_blocked_comments`,
   `RECLARIFY_UNROUTED_*`, marker `<!-- ai:reclarify-unrouted:v1 comment=<id> -->`).
3. **plan** (`plan.yml`, `internal-plan.yml`) — read the clarified issue and
   emit a structured implementation plan with files-to-change and a
   per-issue ≤60-minute time budget. Codex runs in the isolated container
   (see "Isolated Codex agents").
4. **implement** (`implement.yml`, `internal-implement.yml`) — execute the
   plan with codex-cli; write the actual files. Codex runs in the isolated
   container and edits a copy of the workspace (see "Isolated Codex agents").
5. **implement-diagnose** (`scripts/implement_diagnose_post_codex_failure.sh`,
   driven by `MODEL_DIAGNOSE`) — analyse a post-Codex validation failure and
   emit JSON fix-up issue proposals.
6. **implement-repair** (`prompts/mode-implement-repair.txt`,
   `mode-implement-repair-syntax.txt`) — narrow post-Codex repair runs.
7. **review autofix** (`review_autofix.yml`, `internal-review.yml`) — multi-
   model reviewer + consolidator + editor loop on PR changes. Two pre-review
   gates run first: the merge train (`scripts/review_merge_train.sh gate`,
   `MERGE_TRAIN_ENABLED`) queues an `ai/issue-*` PR behind older open
   same-repository `ai/issue-*` PRs on the same base that edit the same non-ignored files
   (`MERGE_TRAIN_IGNORE_PATHS` defaults to the generated manifest); since #6570 a shared path blocks only
   when `git merge-tree --write-tree` of the two heads conflicts outside `MERGE_TRAIN_IGNORE_PATHS`
   (`MERGE_TRAIN_CONFLICT_CHECK_ENABLED`; a conflict only in ignored files logs `conflict=ignored` and does not block; git errors
   keep the path rule; outside a checkout, as in the poller and PR-close `release`, the probe fetches both heads
   into a private bare repository under `RUNNER_TEMP` and logs `MERGE_TRAIN_PROBE_REPO`), a blocker under review past `MERGE_TRAIN_HEAD_MAX_AGE_HOURS` (default 6) that is not
   itself queued stops blocking, and a PR whose verified `ai/issue-<M>` closing issue carries a
   `MERGE_TRAIN_PRIORITY_LABELS` label passes verified non-priority PRs; the last two share one aliased
   GraphQL read, and gate and release apply the same rules (the queued marker is verified against the `GH_PAT` account before it can authorize a bypass; label
   `ai:merge-queued`; released by `cancel_on_pr_close.yml` on close and by
   `orchestrate_poll.yml` every tick; managed/standalone conflict and stall
   recovery treat the label as an intentional wait). The one-shot bypass
   requires an automation-authored queued marker and a later authorized label
   removal. The merge-topology gate hands a
   content conflict to the resolver tail *before* the reviewer/editor spend
   (`PRE_REVIEW_CONFLICT_RESOLVE_ENABLED`, sets `AUTOFIX_PRE_REVIEW_RESOLVE`).
   The `gate` job also runs an identical-failure fingerprint cap: every
   failure comment ends with a `review-autofix-failure:v1` marker (head,
   reason, fingerprint of the failing stage's stderr), and once
   `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL` (default 3) trailing markers on
   the current head share one fingerprint the gate skips the run
   (`skip_reason=fingerprint_cap`) and the `fingerprint-cap-block` job labels
   the linked issues (or the PR) `ai:review-blocked`, posts one
   `review-autofix-failure-cap:v1` comment and sends an
   `identical_failure_cap` heal report (`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`;
   force_rb_judge dispatches bypass it). A reason listed in
   `NON_RETRYABLE_FAILURE_REASONS` (`scripts/workflow_failure_heal.py`:
   `conflict_resolver_sandbox_path_host_only`,
   `conflict_resolver_sandbox_path_unsupported`,
   `conflict_resolver_sandbox_support_missing`) trips the cap on its first
   marker (`non_retryable=true` in `AUTOFIX_FINGERPRINT_CAP_TRIPPED`); the cap
   comment is then titled "non-retryable failure" and quotes the failed run's
   **First error** line (PR #6438: ~28 identical resolver runs on one head). The poller's noop-suspicious
   recovery sweep (`scripts/orchestrate_poll_process.sh`) does not
   re-dispatch a PR whose current head already has a
   `review-autofix-failure-cap:v1` comment by the `GH_PAT` account, because
   the gate would end that run before a new warning could be posted; it logs
   `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP` instead of sending the "retry N/3"
   Telegram WARNING. A push clears the skip, and an unresolvable head SHA or
   token identity keeps the old re-dispatch.
   **Model-provider outages (issue #6633).** A review/autofix failure whose
   pipeline logs show a provider error (OpenRouter 402 / "Insufficient
   credits", 401, 429, 5xx, or the provider's JSON `error.code`) *and* whose
   live probe (`scripts/provider_outage.py`: key check plus a 1-token
   completion with `PROVIDER_OUTAGE_PROBE_MODEL`) also fails gets reason
   `provider_unavailable`. Evidence alone never classifies, because PR text
   can quote such errors. Those markers count toward neither the
   identical-failure cap nor the heal streak; no `ai:review-blocked` label,
   force tick, per-PR Telegram alert or heal report follows, and the PR gets
   a "paused — model provider unavailable" comment instead. The first such
   failure opens one `ai:provider-outage` tracker issue (`AI Provider Outage`,
   marker `<!-- ai:provider-outage:v1 kind=outage … -->`, trusted only when
   authored by the `GH_PAT` login; a create race keeps the lowest number) and
   sends one CRITICAL alert naming the provider, the status and the secret
   name `OPENROUTER_API_KEY`. While it is open, `review_autofix_sweep.yml`
   (`*/30`) probes before enumerating: down or inconclusive skips review
   dispatches (`AUTOFIX_SWEEP_SKIP reason=provider_outage`); up re-dispatches
   every open PR whose newest current-head marker is `provider_unavailable`,
   removes `ai:review-blocked` only where a trusted
   `ai:provider-outage-label:v1` marker (written when a failed judge applied
   the label during the outage) is not superseded by a later real failure or
   cap marker, reports release runs the heal intake recorded on the tracker
   (re-runs them only with `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true`, this
   repo only), records progress in the tracker's resume marker, closes it and
   sends one "recovered" alert. Consumers get the same pass from the
   `provider-outage-resume` job of `ai-review.yml` (hourly schedule,
   `GH_PAT` only). A Claude pool run with every account gated
   (`claude-pool-token` reason `all_gated`) opens a separate `kind=capacity`
   tracker with one WARNING; with the capacity wait on (default since #7370)
   the review pauses as `claude_capacity` and the tracker closes only after the
   hourly capacity-resume pass resumed every waiting item. Log prefix `PROVIDER_OUTAGE`.
   Failure and cap markers carry `support=<sha>`, the gate's verified review
   support SHA (#6625); the gate, the cap job and the sweep ignore markers of
   another or no support version, so a support fix lets the scheduled sweep
   retry a capped head (`NOOP_RECOVERY_FINGERPRINT_CAP_STALE_SUPPORT`). The
   sweep takes the support SHA from its engine SHA here and from the
   `ai-review.yml` pin in consumers; unresolved, any cap marker still skips.
   The gate runs `scripts/review_head_gate.sh` from its verified support SHA:
   `pull_request.synchronize` withdraws stale auto-merge (failure fails the
   gate), and opened/synchronize events mark their SHA `pending` immediately
   after the first PR read, before the rest of the gate evaluation, in the fixed
   commit-status context `ai-review/head-gate`. Deterministic skips and clean
   review/security tails post `success` on the evaluated head before their
   head-bound merge; the review-blocked judge does likewise after its security
   gate, including when auto-merge is disabled and an approved merge needs a
   manual action. `REVIEW_HEAD_GATE_STATUS_ENABLED` and
   `REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED` default to `true`. GitHub does not
   enforce this status until the default branch's required checks include it;
   this workflow does not edit branch protection. Logs: `REVIEW_HEAD_GATE`.
   The review editor's disposable Docker workspace admits `.cjs`, `.mjs`,
   `.cts`, and `.mts` alongside other source extensions for snapshot and
    validated transfer. For Claude engine fixes it also admits
   `.github/ai/claude_engine.json`, `.claude/hooks/gh_api_write_guard.py`,
    and `scripts/claude_settings.json.tmpl`; the merged-PR safety hook stays
    excluded from snapshot and transfer. That exclusion is deliberate: #6208
    reversed #6187's admission of `.claude/hooks/pr_merge_status_guard.py`
    because the hook runs on the host with credentials, so PR-derived editor
    output must never rewrite it. Consequence: review autofix cannot repair a
    CI failure in that hook (as it did in #6481, `fc1c150`); route such a
    failure to an interactive session, which must change the hook and its
    `workflow-templates/.claude/hooks/` twin together (byte-identical,
    `tests/test_claude_template_live_parity.py`). It also admits each
    `.claude/commands/<name>.md` whose `workflow-templates/.claude/commands/<name>.md`
   twin exists both in the host checkout and the verified workflow-support
   checkout (`GITHUB_WORKSPACE/.codex-workflow-src`) when the snapshot is
   taken. A PR-added twin absent from trusted support cannot authorize a new
   command; missing support admits none. The admitted command set is fixed
   for the whole run; later transfers and retries cannot widen it. The editor
   can still repair parity for existing supported commands; other `.github/ai/`
     and `.claude/` files remain excluded from snapshot and transfer. An editor
     write to an excluded file in an admitted directory is dropped; a new
     directory outside the admitted ones fails the transfer and the editor step
     with it, reporting a fixed `reason=unsafe_directory category=<class>` and
     bucketed `depth=1|2|3+` without printing the sandbox-controlled path.
    The command contract test `tests/test_audit_plans_command.py` is omitted
    from the sandbox; host CI still runs it. The isolation helpers must already
    exist in the verified workflow support commit; a PR's own copies are review data,
   not executable support, so review fails closed until that commit lands.
   A rejected transfer names its cause without printing untrusted paths:
   known rejections carry fixed `reason=<r>` tokens, and unsafe directories
   also carry `category=<c> depth=<d>`. Transfer diagnostics are written to
   an attempt-scoped `review_sandbox_transfer_reason_<output>` file and
   archived as `review_sandbox_transfer_reason_<attempt>.txt` (#6413).
   The smoke-only canary pre-write (`IS_SMOKE_TEST=true`) is seeded into the
   sandbox source with `scripts/review_untrusted_sandbox.sh seed`, not written
   to the host, so validated transfer publishes it; a host write after the
   snapshot is refused as `host_baseline_changed` (run 37669315093).
   PR-backed `claude/*` heads take the normal review path like every other
   PR: the GPT editor, conflict resolver, review-blocked judge and auto-merge
   all run on them. The former Claude-fixer hand-off (the reviewer panel
   handing findings to a claude.ai session) was retired together with
   CLAUDE.md §26 / §28; `claude_fixer_converged_head` is still accepted by
   `review_autofix.yml` for caller compatibility but ignored. Since Phase 5c
   `CLAUDE_FIXER_ENABLED` (default `true`) is the switch for the Claude
   engine of the review write roles: `false` keeps the editor, consolidator,
   conflict resolver and review-blocked judge on OpenCode. The
   three PR-derived Claude review roles run through the editor's network-isolated
   sandbox and credential-free relay: the consolidator uses its prepared
   read-only snapshot, and the judge and resolver prepare fresh snapshots
   without installing dependencies. The relay suppresses peer disconnects and
   timeouts on rejection, but other write errors reach the server error handler.
   The consolidator's OpenCode path (including Claude-unavailable fallback)
   uses a fresh credential-free, read-only ephemeral sandbox with the reviewer
   agent; isolation failure skips consolidation with empty output.
    Host `claude_run` refuses all four review roles. The resolver checks its
    conflicted paths against sandbox admission before either engine runs; an
    unsupported path is refused and, for integration-sync PRs, counted toward
    the existing resolver retry-state escalation, logging
    `REVIEW_RESOLVER_PATH_REJECTED reason=<token> path=<path|redacted>`
    (paths outside a fixed safe character set, and symlinks or odd names,
    print as `redacted`). When the
    live `.claude/hooks/pr_merge_status_guard.py` and its `workflow-templates/`
    twin conflict with identical index stages, the model resolves only the
    template; the runner copies it byte-for-byte over the live hook, which stays
    excluded from the sandbox, and requires identical staged blobs before the
    commit (`REVIEW_RESOLVER_PAIRED_LIVE`). `check-paths` writes a
    per-path report: when every rejected path is a plainly named file the
    sandbox policy keeps on the host, the resolver logs one
    `::error::Conflict resolver: host-only conflicted path(s) need a manual
    merge: <paths>` line and fails closed with `sandbox_path_host_only`;
    symlinks and odd names keep the nameless `sandbox_path_unsupported`. No
    model runs in either case, because a merge commit needs every path
    resolved. Outside integration-sync PRs every fail-closed reason is
    exported as `AUTOFIX_FAILURE_REASON=conflict_resolver_<reason>`, so the
    failure marker names it; integration-sync PRs keep the generic reason so
    their failures still reach the retry-state escape threshold that drives
    the orchestrator's branch rebuild. Its OpenCode runs (including
    Claude fallback)
    use fresh isolated snapshots and validated transfer, never the host writer.
    A Claude-selected resolver retries OpenCode in a fresh sandbox only when
    Claude is unavailable; isolation or transfer failure fails closed.
   The judge verdict uses read access; its fix and the resolver use write access
   with validated transfer back to the workspace. The poller's review-blocked
   judge uses the same sandbox from the verified workflow support checkout,
   with read access for a verdict and validated transfer for a combined fix;
   transfer checks destination parents and stages all changes before writing,
   rolling host changes back on failure. After a failed Claude fix or transfer
   the judge neither commits/pushes nor treats it as merged
   (`judge_skip_reason=fix_transfer_failed|fix_failed`).
   The in-workflow judge's OpenCode verdict and fix passes also use fresh
   credential-free sandboxes; missing isolation defers with
   `judge_skip_reason=isolation_unavailable`, never a host writer. For the poller's judge,
    missing isolation defers and escalates after three failures on the same
    head (configurable with `RB_JUDGE_ISOLATION_MAX_FAILURES`); unavailable
    Claude retries OpenCode in a fresh sandbox, never host Codex. Failed transfer discards the
    verdict and removes only newly untracked files. Ephemeral OpenCode attempts
    build the OpenCode-only image independently of Claude support files or CLI
    installation; a failed OpenCode image build still defers without host fallback.
    Cleanup/inventory failures
   stop the tick so another issue cannot stage a partial transfer. Lost comment
   responses are reconciled from trusted history; uncleared human latches
   block the judge even on a new head. The
   `claude-fixer-auto-merge` job id is kept but never runs.
    The other four poller judge roles use that sandbox with read-only access
    on both engines; a missing sandbox never starts a host judge.
    The review-blocked judge selects its engine from the already fetched
    linked PR labels (falling back to OpenCode if labels are unavailable);
    a standalone stall judge selects from its target issue labels.
   `[claude-intervention]` and `[claude-merge-resolve]` commits on older PR
   heads still end the counted run, like `[judge-fix]` and
   `[ai-merge-resolve]`.
8. **conflict resolver** (`prompts/conflict-resolver.txt`,
   `integration-sync-conflict-resolver.txt`) — merge-conflict resolution
   inside autofix. In consumer repos the resolver, the review-blocked judge
   and the poller remove workflow-generated root files before committing but
   keep any path HEAD tracks (a consumer-owned `agents.md`), logging
   `Preserving repo-tracked path during artifact cleanup: <path>`.
9. **orchestrate** (`orchestrate.yml`, `orchestrate_poll.yml`) — issue
   decomposition + judge polling, including the default-on, current-head
   project security-pass gate before validation/finalization. The poller's
   wave, stall, integration and security-pass judges use read-only,
   credential-free review sandboxes on both engines. Isolation failure
   defers (never host fallback) and escalates per role after
   `JUDGE_ISOLATION_MAX_FAILURES`; only deterministic poller code writes to
   GitHub. All five poller judges run on `scripts/review_untrusted_sandbox.sh`,
   not `codex_isolated_exec.sh` (see "Isolated Codex agents" → "Poller
   judges"). The standalone stall judge uses verified issue labels; managed
   stall and review-blocked judges combine issue and tracking labels
   (`ai:codex` wins), falling back to codex when the issue's GraphQL label
   page is missing, incomplete (more than 100 labels), or unverifiable.
   Project-level judges keep tracking labels. Integration verdicts
   re-dispatch the isolated conflict resolver;
   the resolver reads the existing tracking-comment snapshot for guidance
   bound to its PR head and default-branch tip, fenced as untrusted advisory context.
   Unconfirmed guidance publication defers dispatch without charging budget.
   An active resolver is checked before the judge call and again at dispatch.
10. **judge** (`mode-judge.txt`, `mode-orchestrate-poll-judge.txt`,
    `mode-judge-review-blocked.txt`, `mode-judge-stall-recovery.txt`) —
    JSON-emitting evaluation of wave state.
11. **validate** (`validate.yml`, `mode-validate-*.txt`) — generate / fix /
    self-heal a validation harness for the implemented change.
12. **workflow log analysis** (`workflow-log-analysis.yml`,
    `mode-workflow-*.txt`) — periodic audit of workflow runs.
13. **check failure triage** (`check_failure_triage.yml`,
    `internal-check-failure-triage.yml`, `scripts/check_failure_triage.sh`,
    `prompts/mode-check-failure-triage.txt`) — triggers on `check_run:
    completed` for non-Actions checks or failed `workflow_run: completed`
    pull-request runs of Actions CI; the diagnosis model analyses the failing
    check's logs and opens a GitHub issue (label `ai:check-triage`) describing
    the root cause + suggested fix, which the clarify→…→review pipeline then
    picks up. On by default; disable per repo via
    `CHECK_FAILURE_TRIAGE_ENABLED=false`; never pushes code itself. De-dupes one
    in-flight triage per repo+PR+check and caps the
    auto-fix lineage at `CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH` generations
    (escalates with `ai:check-triage-escalated` + Telegram at the cap).
    Files nothing when the base branch passes the same check
    (`CHECK_FAILURE_TRIAGE_BASE_GATE_ENABLED`, default `true`; log
    `CHECK_TRIAGE skip reason=pr_specific_failure`): a base-branch fix cannot
    repair a PR-specific failure, which the PR's autofix loop owns.
    The PR-head checkout does not persist credentials; collection uses a
    GitHub token before Codex runs from a trusted support directory in a
    separate, GitHub-token-free step. Missing trusted support fails closed.
    Both wrappers pass only the four declared secrets, and diagnosis runs in
    the credential-free, read-only clarify container with a host-side broker
    (on the Claude engine by default, `CHECK_TRIAGE`; isolated codex when Claude
    is unavailable); missing isolation falls back to a raw-context issue, never host Codex.
    The helper runs from trusted support with `CLARIFY_SOURCE_ROOT` set to the
    PR checkout, so host Python and the broker never execute PR-head modules.
    Triage omits agent instruction files at any depth from the sandbox snapshot;
    the opt-in filter leaves other clarify callers' snapshots unchanged.
    PR-head `agents.md` / `AGENTS.md` enters the prompt through a bounded,
    credential-free regular-file read that never follows symlinks.
    Untrusted check/workflow names are single-lined, length-capped, and have
    backticks and markers escaped before display, including a direct Telegram
    failure alert when trusted support staging fails (never sourcing PR-head
    helpers; suppressed without credentials or at `ALERT_MSG_LEVEL=SILENT`);
    logs and model diagnosis
    text are neutralised before issue posting
    so they cannot spoof downstream routing metadata or triage markers.
    Check metadata is flattened for display while raw names remain in dedup keys;
    the complete redacted issue body is checked for routing keys and forged markers
    before either the posting step or a direct caller may create the issue.
    Issue posting is separate and requires the `CHECK_TRIAGE_ISSUES_TOKEN`
    fine-grained PAT so `issues: opened` still fires without exposing `GH_PAT`.
    Fix PRs linked to triage issues run the single-issue security pass at their
    current head; the triage label and fingerprint do not exempt them.
14. **workflow failure heal** (`workflow_failure_heal.yml`,
    `internal-workflow-failure-heal.yml`, `workflow-failure-heal-intake.yml`,
    `scripts/workflow_failure_heal_report.sh`,
    `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`,
    `prompts/mode-workflow-failure-heal.txt`) — triggers on `issues: labeled` /
    `pull_request: labeled` with a human-needed escalation label
    (`ai:needs-human`, `ai:check-triage-escalated`, `ai:destructive-blocked`,
    `ai:scope-blocked`, `ai:harness-broken`, `ai:resolver-escalated`,
    `ai:security-pass-failed`) in a consumer or in this repo, and on
    `workflow_run: completed` failures of the five release / promotion
    workflows and of `CI` on pushes to the default branch. The reporter links the failed runs and the wrapper release pin
    and sends a `repository_dispatch` (`workflow-failure-heal`) to this repo;
    the intake fetches the failed job logs, diagnoses (`WORKFLOW_HEAL` role:
    Claude by default through `claude_run_selected`, isolated codex when Claude
    is unavailable) against the source at
    that SHA, classifies (`workflow-defect` / `inconclusive` → issue here with
    `Target branch: stable`; a review/autofix failure from this repo
    targets its PR head only when `script_ref` equals the head SHA,
    the verified support branch for `stable` or a main-ancestor ref,
    and otherwise has no Target branch and targets the default branch
    (`target_branch_source=shared_workflow`, issue #6680);
    `consumer-app-defect` → issue in the consumer;
    `consumer-config` / `transient` → Telegram + comment only;
    `already-fixed` → Telegram + comment only, honoured only when its
    `## Fixed by` section cites a commit that landed after the failing SHA,
    otherwise filed as `inconclusive`; for a
    review/autofix failure from this repo whose crash file the intake can
    attribute, `pr-self-inflicted` → diagnosis comment on the PR, no issue,
    and `base-self-inflicted` → issue targeting the PR's base branch with
    orchestrator lineage lines and `ai:orchestrator-managed` for an
    `orchestrator/project-<N>` base; ownership comes from the report's
    `changed_files` / `crash_file` and the intake's own
    `git diff origin/main origin/<base>`; with no crash file, a run that
    staged the PR head's scripts (`script_ref` = head SHA) of a PR changing
    `scripts/`, `.github/actions/` or `review_autofix.yml` is still `pr`
    ownership (`basis=pipeline_files`), and a token it does not back, or
    `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED=false`, routes as
    `workflow-defect`). The prompt carries the branch progress since the
    failing SHA (one REST compare call + a branch-tip worktree) and the earlier
    heal issues of the same fingerprint / lineage. Failure evidence and model
    diagnosis are neutralised before issue posting, and the finished heal body
    is checked for forged routing metadata and markers before creation. It
    de-dupes by fingerprint
    (label `ai:workflow-heal`; the promote cycle's `[cycle:<id>]` run-name
    suffix is ignored, and the error signature comes from the steps'
    `##[error]` output, not the echoed step script; for an `autofix_failure`
    report it comes from the evidence minus the reporter's header lines,
    `error-signature --strip-autofix-header`, led by the
    `AUTOFIX_FAILURE_FIRST_ERROR` line the reporter adds; an
    `identical_failure_cap` report uses its payload `failure_fingerprint`), de-dupes an
    `autofix_failure` report also by its pull request's `source=` marker (that
    PR's closed heal issues, and the heal issue its `ai/issue-<N>` head branch
    fixes, continue the lineage), caps the lineage at
    `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH` (escalates with
    `ai:workflow-heal-escalated` + Telegram), and bounds the volume with
     `WORKFLOW_HEAL_MAX_OPEN_ISSUES` / `WORKFLOW_HEAL_MAX_ISSUES_PER_DAY`.
     Comment run links only prioritize failed runs verified in the recent
     run listing with a matching issue title; source generation markers are
     inherited only from heal-labeled issues with a canonical marker header
     and a bot or owner/member/collaborator author. The
     composed issue neutralizes untrusted routing keys in evidence and diagnosis
     and rejects any remaining routing directives or extra markers before filing,
     so only intake-owned branch metadata can select the fix target.
     Fix PRs linked to `ai:workflow-heal` issues run the single-issue security
    pass at their current head; the heal label and `fp` marker do not exempt
    them (finding `ci-heal-skips-security-pass`). A third reporter lives
    in the failure path of `review_autofix.yml`
    (`scripts/workflow_failure_heal_autofix_report.sh`, payload kind
    `autofix_failure`): it reports a failed review/autofix run on a pull
    request once `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` (default 1, so every
    failed run) runs in a row failed on that PR, counted from the workflow's
    own failure comments. A fourth reporter is the `heal-report` job of
    `clarify.yml`, `plan.yml` and `implement.yml`
    (`scripts/workflow_failure_heal_phase_report.sh`, payload kind
    `phase_failure`): after the phase job ends in `failure` it reports the run
    once `WORKFLOW_HEAL_PHASE_FAILURE_STREAK` (default 1) runs of that phase in
    a row failed on the issue; duplicate comments for one run count once. The
    intake fingerprints only the current run's log, uses earlier streak logs
    for diagnosis context, and keys the report on the issue's `source=`
    marker, and a heal issue whose own run fails with its own fingerprint is
    escalated (`reason=heal_issue_failed_itself`). Implement skips guard
    blocks, diagnosed fix-up failures and `BLOCKED` verdicts through the job
    output `heal_report`. Before fingerprinting or mutation, the intake verifies
    phase reports against the GitHub-read run, failed phase job and a linking
    comment from a GitHub-reported trusted source-issue author (OWNER, MEMBER,
    COLLABORATOR or `github-actions[bot]`); self-repo reports also require the
    intake's authenticated account. Mismatches, self-repo identity failures and
    read failures skip
    with `WORKFLOW_HEAL skip reason=phase_report_unverified` and a WARNING.
    Payload `source_gen` / `source_root` lineage is honoured only when the
    listed `ai:workflow-heal` source issue was authored by the intake's current
    authenticated account and its markers match; an account rotation makes
    older heal issues' payload markers unverified (fingerprint lineage remains).
    Its comment streak trusts only the authenticated
    workflow account; cancellations and successful implementation break the
    streak, and unavailable identity or comment history reports the current
    failure without applying a higher threshold. A failed
    `Run reviewer models` step (the editor never ran) is reported as
    `reviewers_failed` with per-slot / summariser exit codes
    (`reviewers_failure_evidence.txt`, `AUTOFIX_REVIEWERS_FAILED=true`) rather
    than `editor_empty_noop`, and a failed `Install project dependencies`
    sandbox prepare (`deps_prepare`) is reported the same way as
    `sandbox_prepare_failed` (`sandbox_prepare_failure_evidence.txt`,
    `AUTOFIX_SANDBOX_PREPARE_FAILED=true`; the sandbox image build retries
    Docker Hub and network failures, `REVIEW_SANDBOX_BUILD_ATTEMPTS`,
    `REVIEW_SANDBOX_BUILD_RETRY_SLEEP_1` / `_2`); the identical-failure cap's report lists the
    failed runs from the head's `review-autofix-failure:v1` markers
    (`AUTOFIX_FAILURE_MARKER_AUTHOR`) so the intake reads their logs; and a
    support script's self-named error line (`untrusted_process_sandbox: …`)
    counts as the crash file when that script exists. When a PR in this repo
    closes, the `heal-pr-reconcile` job (`internal-cancel-on-pr-close.yml`,
    `scripts/workflow_failure_heal_pr_reconcile.sh`,
    `WORKFLOW_HEAL_PR_RECONCILE_ENABLED`) closes the heal PRs stacked on its
    head branch and their heal issues (source unmerged), or merges its final
    head and its base into their heal branches (fast-forward push, no force)
    and re-points them at its base (source merged). Reporters wrap
    the report as `client_payload: {schema_version, report}` (GitHub caps
    `client_payload` at 10 top-level properties); the intake unwraps it and
    accepts the flat shape too, and a rejected dispatch logs `detail=` with
    the first 300 characters of the API error. Reporters also attach a
    GitHub Actions OIDC token (`client_payload.report_identity`, audience
    `coding-workflows-heal-report`, wrappers grant `id-token: write`); the
    intake verifies it when present (JWKS signature, issuer, audience, `iat`
    within `WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS`, `repository` = source repo,
    `job_workflow_ref` = this repo's reporter workflow, reporter run), then
    binds the claimed issue / PR, label and runs to the source repository via
    the API (`workflow_failure_heal.py verify-report-identity` /
    `bind-report`). A report without a token passes on the binding checks
    while `WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=false` (default) and is skipped
    when `true` (phase reports included: their `heal-report` job is granted
    `id-token: write` and attaches a token). Only label-escalation reports have their
    runs bound here (claimed runs that all fail binding reject the report as
    `no_bound_runs`; title, URL and body excerpt come from the fetched issue,
    and an unauthenticated report's comments excerpt is dropped; autofix reports take theirs from the fetched PR); phase, autofix and `workflow_run` runs stay with the
    provenance gate; rejections fail closed (`report_auth=rejected reason=…`,
    WARNING, nothing filed). On by
    default; disable per repo via `WORKFLOW_HEAL_ENABLED=false`; never pushes
    code itself. Stable log prefixes: `WORKFLOW_HEAL_REPORT`,
    `WORKFLOW_HEAL_AUTOFIX_REPORT`, `WORKFLOW_HEAL_PHASE_REPORT`,
    `WORKFLOW_HEAL_PR_RECONCILE`, `WORKFLOW_HEAL`.
    Before reading logs for phase, autofix or release reports, the intake
    checks referenced runs' repository, failure status and workflow path
    (a `ci.yml` run only as a `push` run on the default branch, which comes
    from `WORKFLOW_HEAL_DEFAULT_BRANCH` or the intake event's
    `repository.default_branch`, else `ci_not_default_branch_push`; a
    `nightly-validation-selftest.yml` run only on the default branch, else
    `scheduled_check_not_default_branch`);
    phase and autofix runs must also be linked to the issue or PR. Self-repo
    phase failure comments must come from the intake token's account; consumer
    phase comments require a trusted GitHub-reported author association or
    `github-actions[bot]`, and phase run events are checked before job reads.
    Rejections log `WORKFLOW_HEAL provenance_rejected` and fail closed, so a
    missing phase failure comment prevents intake. A holder of the shared
    `GH_PAT` can still read registered repositories' logs directly or report a genuinely
    linked failed run. Label-escalation `issue` and `pull_request` reports are
    outside this gate even when their issue/comment-derived `run_refs` are
    present; those reports can still fetch unverified job logs with the shared
    `GH_PAT`. The intake redacts logs before disk, then writes a scope marker
    derived from verified paths (or leaves it absent when unresolved). Heal
    planning uses the read-only container; implementation verifies the PAT
    author's unedited marker and runs an editor in a disposable container,
    transferring only scoped paths. An unverifiable marker latches
    `ai:needs-human`; the reporter skips that pipeline-authored refusal.
    Ordinary issue editors remain unchanged.
    A report whose failure reason is `identical_failure_cap`, or a generation
    > 1 of its lineage, is deterministic (`is_deterministic_failure`): the
    intake never files it as `transient` (remaps to `inconclusive`,
    `classification_remapped … reason=deterministic_failure`), and its issue
    body forbids retry / backoff / re-run fixes and requires a regression test
    that reproduces the failure. The review/autofix failure comment names the
    failed step (one jobs-API call matched on `RUNNER_NAME`) and the first
    specific `::error::` line of the captured stage stderr (including the
    resolver's, `resolver_stage_stderr.txt`), redacted
    (`failure-headline`, log prefix `AUTOFIX_FAILURE_HEADLINE`).
15. **Claude issue implementer (retired)** — standalone issues always run the
    Codex pipeline (clarify → plan → implement); `clarify.yml` no longer
    routes to Claude, and `AI_ISSUE_IMPLEMENTER` is no longer read. The
    Claude issue intake / queue / pickup / dispatcher machinery and its
    `ai:claude*` labels were removed (see the `retired_labels` bullet under
    "Implement scope-lock label").
16. **consumer secrets propagation** (`propagate-consumer-secrets.yml`,
    `scripts/propagate_consumer_secrets.sh`) — on every push to `main` that
    changes `.github/ai/consumer_repos.json` (the `/seed-repo` registration
    PR, CLAUDE.md §14) it copies `CHECK_TRIAGE_ISSUES_TOKEN`, `GH_PAT`,
    `OPENROUTER_API_KEY` and `TG_BOT_SECRET` from this repository's secrets
    into the newly registered consumers via `gh secret set` (value on
    stdin, never logged); `workflow_dispatch` with an empty `targets` input
    backfills every registry entry. Unregistered targets are refused; a
    failed or unverified write leaves the run red and sends a Telegram
    CRITICAL. Tests: `tests/test_propagate_consumer_secrets.py`.

Planner scope note: the Boil the Lake rule is a planner-side instruction for
choosing the right scope mode up front, while CLAUDE.md §5 / the unattended
minimal-change rules still bind implementers and reviewers after that choice is
made. There is no conflict: planners may explicitly choose Expansion or
Selective Expansion when the marginal completeness cost is small, and editors /
reviewers must then stay surgical inside that approved scope rather than
shrinking it ad hoc.

CI guard contract: `.github/workflows/ci.yml` runs the `Shared shell-block
anti-regression checks` step immediately after checkout in the `static-checks`
job, before Python setup, dependency installation, and lint; the test jobs run
in parallel with it (see "CI job layout" below). Rejections emit the secret-safe
`CI_GUARD_FAILURE` diagnostic with `guard`, `check`, `file`, `line`,
`expected`, and `scanned_files` fields, and the guard deliberately uses
ubiquitous `grep` instead of `rg` so runner images without ripgrep still fail
only on real policy drift.

The `gh api` permission guard in `.claude/hooks/gh_api_write_guard.py` and
its `workflow-templates/` twin exempts an unquoted literal-ID loop counter
only when the entire loop passes the read-only body validator. Unvetted
loops with unquoted `gh api` arguments still prompt; the two hooks must stay
byte-identical (`tests/test_gh_api_write_guard.py`). Shell-rewrite hazards
inside a loop prompt even when the loop counter is not expanded. A `gh api`
call whose only argument is `--help` or `-h` is a read (it prints usage and
sends no API request); `--help` / `-h` next to any endpoint, flag or `--`
stays unreadable and prompts (#6668).

The merged-PR guard in `.claude/hooks/pr_merge_status_guard.py` and its
`workflow-templates/` twin distinguish numeric branch refspecs from adjacent
unquoted file descriptors before checking push destinations. `2 > out` retains
the branch `2`; `2>out` does not. The copies stay byte-identical.

Security finding #6755 (`merged-pr-guard-misses-wrapped-git`) is fixed in
both `pr_merge_status_guard.py` copies, which stay byte-identical. The guard
parses `bash`/`sh`/`zsh`/`dash` `-c` scripts, `eval` text, `$(...)`, backtick
and `<(...)` bodies up to three levels deep (`_MAX_SHELL_WRAPPER_DEPTH`) and
runs the normal merged-PR check on each Git commit or push found there;
wrapped text it cannot read safely (a dynamic command word, an unterminated
body, deeper nesting) asks for confirmation when it could run a Git write.
Since #6038 the `time`, `command`, `nice`, `ionice`, `exec`, `nohup`,
`setsid` and `timeout` prefixes are skipped and the Git command behind them
is checked; other runners with a Git write in their words (`sudo git push`,
`xargs git push`) ask, and pipes into `bash` stay unparsed. The live
hook runs on every Bash call through its `PreToolUse` settings entry; no
variable or operator step enables it. `CLAUDE_PR_MERGE_GUARD=off` disables
only the merged-PR check, not the API-write confirmation safeguard. Tests run
against both copies in `tests/test_pr_merge_status_guard.py`. The interactive
session applied the change because the pipeline's editors cannot write
`.claude/hooks/**`; the former `WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP`
placeholder is retired and nothing reads it. The full rules are in the
"Merged-PR guard" bullet under
"Isolated Codex agents".

Integration-ref trust boundary: `scripts/resolve_integration_ref.sh` can return
any existing valid Git branch name declared by issue metadata. Workflows may
pass that output to action inputs or through step-local environment variables,
but must never interpolate it directly into `run:` script source.
`tests/test_workflow_checkout_integration_ref_audit.py` pins the env-bound log
contract for every resolver-consuming workflow.
In `implement.yml`, issue title and body text must never use a fixed `GITHUB_ENV` delimiter; the body travels to later steps only through `ISSUE_BODY_FILE`.

Plan prompt note: `PLAN_DIAGRAMS_OPTIONAL` defaults to `true`, so plan outputs
may include `Data flow:`, `State machines:`, and `Failure modes:` only when
they materially help. Trivial plans should omit them, and `State machines:` is
required only for changes touching the orchestrator phase machine
(`ai:clarification` → `ai:planning` → `ai:awaiting-approval` →
`ai:implementing` → `ai:done` → `ai:ready-to-merge` → `ai:merged`).

Prompt assembly note: the migrated shared-prelude prompt family keeps the
legacy runtime bodies in `prompts/mode-*.txt` and stores the include-based
sources in `prompts/_templates/*.txt`. `scripts/assemble_prompt.sh` is a thin
wrapper over `render_prompt.py --assemble-only`, and `scripts/render_prompt.sh`
uses it only when `PROMPT_PRELUDE_REFACTOR_ENABLED=true`. Default persona
prefixes come from the checked-in JSON map at `prompts/_prelude_role_persona.txt`
unless `PROMPT_PERSONA_PREFIX_ENABLED` is disabled.

The poller counts the validation runs it dispatches per validation cycle
(state `validation_cycle_dispatch_count`, reset when
`validation_last_dispatch_cycle` changes). A run that ends without a verdict
label is re-dispatched once it is an hour old and no validation run is
active. When `MAX_VALIDATION_DISPATCHES_PER_CYCLE` (default `3`) runs have
ended that way, `dispatch_validation_if_needed` stops and calls
`mark_validation_failed` with `LAST_VAL_RAW_STATUS=harness_error`, so the
project gets `ai:validation-failed` + `ai:harness-broken` and goes to the
heal and unblock paths without spending `MAX_VALIDATION_RECOVERY_ATTEMPTS`.
State written before the counter existed reads as one dispatch.

Model-provider outage alert dedupe (issue #6633): `opencode_emit_failure_alert`
(`scripts/opencode_helpers.sh`) runs `scripts/provider_outage.py probe` before a
per-call Telegram ERROR for a runtime failure (setup classes such as `config_*`
are not probed). It caches the result in `RUNTIME_DIR/provider_outage_alert_probe.json`
(re-probed after `PROBE_CACHE_TTL_SECS`, 300 s), separate from the classifier's
cache. When the probe reports `down`, it opens or reuses the `ai:provider-outage`
tracker itself (`provider_outage.py open`; fail-open callers can let the job
succeed without reaching the failure-path tracker step), sends that tracker's
CRITICAL alert only when this call opened it, and logs
`PROVIDER_OUTAGE op=alert outcome=suppressed reason=provider_down`. If the
tracker cannot be confirmed it logs `outcome=sent reason=tracker_unconfirmed`
and sends the per-call ERROR. A missing `RUNTIME_DIR` or helper,
`PROVIDER_OUTAGE_CLASSIFY_ENABLED=false`, or an `up`/`unknown` probe keeps the
existing alert.

Retired host-only conflict re-issue (issue #6680): before stall recovery
dispatches the conflict resolver or the review-blocked judge for an open PR
(managed `resolve_merge_conflict`, the managed open-PR guard, the standalone
conflict guard, and the managed and standalone `dispatch_rb_judge` rungs), it
runs `_stall_retired_host_only_conflict_check` (`scripts/orchestrate_stall_retired_conflict.sh`,
sourced by the poller, which falls back to fail-closed stubs when the file is
not staged; classifier `orchestrate_lib.py retired-conflict-check`). When every host-only
conflicted path of the PR (one `review_untrusted_workspace.allowed()` refuses)
is listed in the verified support checkout's `workflow-templates/retired_files.txt`
and absent from the base, it runs `close_and_reissue` instead of re-dispatching
a resolver that can only fail closed. The replacement issue names the retired
paths and the closed PR's changed files, and the PR branch is kept. The managed
and standalone replacement bodies discard a stale `Target branch` header for
the default base, or replace it with the PR's base for an integration branch.
The ancestor no-op cap does not discard a verified retired-conflict re-issue.
Managed recovery closes the source issue and PR only after confirming the
replacement issue was created; a failed create leaves both open for retry
and spends one recovery attempt.
The managed review-blocked pre-judge dispatch skips the resolver and the judge for such a PR
and leaves the re-issue to the stall-recovery rung. Conflicts are read locally
with `git merge-tree`, the head tip must equal the PR head SHA, and the PR must
be the issue's own implementation PR; any other case, including probe or trust
failures, keeps the existing dispatch, and the resolver's own
`sandbox_path_host_only` check is unchanged. Kill switch
`STALL_RETIRED_CONFLICT_REISSUE_ENABLED` (default `true`).

For a self-repo review/autofix failure, workflow heal targets the PR head only
when `script_ref` equals the head SHA; otherwise it targets verified stable/main
support or omits Target branch to ship the shared-workflow fix on the default branch.

Weekly consumer backfill (issue #6712): `propagate-consumer-secrets.yml` also
runs on a weekly schedule (`23 5 * * 1`) against every registry entry with
`PROPAGATE_ONLY_MISSING=true`. `scripts/propagate_consumer_secrets.sh` then
lists each consumer's secret names once before any write and sets only the
missing ones (`status=skipped_present` for the rest); a failed listing writes
nothing to that consumer and turns the run red
(`status=failed reason=presence_list_failed`). Push and dispatch runs keep
overwriting. `PROPAGATE_ONLY_MISSING` defaults to `false` in the script and
accepts only `true` or `false`.

- **Check-failure triage integration branch (#6726).** With
  `TRIAGE_PR_HEAD_BRANCH_METADATA_ENABLED` on (default `true`; `false` turns it off) the only
  routing key the triage body validator accepts is one header line
  `- **Integration branch:** \`orchestrator/project-<N>\``, written by
  `scripts/check_failure_triage.sh` when the GitHub API reports a
  same-repository PR head of that shape and the branch exists; it is never
  inferred from logs or issue prose. Such a failure skips the base-branch gate
  (`CHECK_TRIAGE base_gate outcome=file reason=integration_branch`), because
  the fix lands on the failing PR's own head branch.

Review-blocked judge after a host-only resolver refusal (issue #6743, heal
#6733): on a `force_rb_judge` dispatch of `review_autofix.yml`, the `gate` job
reads the newest `review-autofix-failure:v1` marker that the `GH_PAT` account
wrote for the current head under the current review-support version. When its
reason is exactly `conflict_resolver_sandbox_path_host_only`, the gate output
`force_rb_judge_conflict_skip=true` makes `codex-agent` skip Detect merge
conflicts and both resolver steps, so the review-blocked judge runs instead of
the resolver refusing again. Markers from another author, head or support
version, a newer marker with another reason (including `provider_unavailable`,
which the cap's count passes over; the helper reports it as `newest_reason=`), or a failed identity or comment
read keep the conflict steps. The check runs even with
`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false`; its own kill switch is
`REVIEW_FORCE_RB_JUDGE_HOST_ONLY_SKIP_ENABLED` (default `true`). The resolver's
host-only refusal and the review-blocked handoff are unchanged.

Workflow failure heal and the resolver's host-only stop (#6745): the conflict
resolver's host-only conflict stop (`conflict_resolver_sandbox_path_host_only`)
needs a manual merge, not a heal. Its per-run report, an
`identical_failure_cap` report repeating it (`repeated_failure_reason`, from
the gate's `FINGERPRINT_CAP_REASON` or the trusted head markers) and an
`ai:needs-human` label on an item whose newest autofix marker by the `GH_PAT`
account names it (on a PR, for its current head; provider-outage markers and
comments are skipped as in `count_identical_failures`) are skipped by the
reporters and the intake (`WORKFLOW_HEAL skip reason=host_only_conflict_manual_merge`).
The resolver's manual-merge error and the identical-failure cap are unchanged.

Host-only conflicted paths resolved on the host (#6748): outside
integration-sync PRs, `review_conflict_prepare.sh` resolves host-only
conflicted paths before the sandboxed resolver runs. Paired live/template
copies (#6595) are split out first exactly as the resolver does and keep the
pairing path; an unpaired live copy whose template also conflicts leaves the
whole set on the manual-merge path (`reason=unpaired_live_copy`). When none
of the remaining host-only paths is covered by the linked issues'
`files_touched:` allowlist, each takes the base version
(`CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE`, dropped PR-side diff posted with
`<!-- ai:host-only-take-base:v1 -->`; with no other conflict the merge is
committed without a model). When any is covered, the resolver re-issues the
PR through `unblock_actions.py`'s `reissue` ops and `unblock_run_ops`
(a non-default PR base is passed as `pr_base_branch` and becomes the
replacement's `Integration branch` when the linked issue declares none)
(create, PR comment with `<!-- ai:host-only-conflict-reissue:v1 -->`, which
counts for deduplication only from an OWNER/MEMBER/COLLABORATOR author, then
close; `CONFLICT_RESOLVER_HOST_ONLY_REISSUE`) before failing with
`sandbox_path_host_only`. Unknown scope (linked issues from
`LINKED_ISSUES_RAW_FILE` unavailable, empty, or a full 50-reference page;
`CONFLICT_RESOLVER_HOST_ONLY_SCOPE outcome=unknown`) or
`HOST_ONLY_CONFLICT_TAKE_BASE_ENABLED=false` (forwarded by the
`review_autofix.yml` prepare step) keeps the manual-merge failure.

Workflow failure heal and non-retryable review/autofix failures (#6756):
right after its provenance gate, the intake routes a verified
`autofix_failure` report with a `NON_RETRYABLE_FAILURE_REASONS` reason
(directly, or for an `identical_failure_cap` report from the trusted
account's own failure markers on the head) away from heal issues. It posts
one PR comment, ending in
`<!-- ai:workflow-heal-non-retryable:v1 pr= head= reason= -->`, whose
remediation text depends on the reason; for the host-only stop it names the
paths from the trusted failure comment's **First error** line (#6750 / PR
#6535: `.claude/hooks/pr_merge_status_guard.py`). It then adds
`ai:needs-human`, sends a WARNING (which says when the label write failed),
and logs `WORKFLOW_HEAL skip reason=non_retryable_<suffix> outcome=diagnosed
label=applied|failed`. It reuses the provenance comment snapshot. A repeat
for the same pr/head/reason logs `outcome=duplicate label=present|reapplied|failed`
and re-adds the label only when the PR read by claim binding lacks it. A
failed comment post adds no label. The host-only stop (#6745) is no longer
dropped by the review/autofix reporter (`skip-reason --defer-host-only-autofix`)
or by the intake's early skip while routing is on; every intake path that
does not diagnose it still ends in `skip reason=host_only_conflict_manual_merge
stage=after_provenance`, so it never becomes a heal issue. The label reporter
skips the label event the diagnosis causes (`skip reason=non_retryable_diagnosed`)
while the account's newest failure, editor-summary or diagnosis comment is
that marker. Kill
switch: `WORKFLOW_HEAL_NON_RETRYABLE_ROUTING_ENABLED` (default `true`, wired in
`workflow-failure-heal-intake.yml`); `false` restores #6745's early host-only
skip and heal issues for the other two reasons.

Workflow failure heal issue creation is gated by
`HEAL_INTAKE_SCOPE_GUARD_ENABLED` (default `true`, unset or empty also on, #6871). While it is off (any other value),
the intake composes the heal issue, logs
`WORKFLOW_HEAL skip reason=scope_guard_disabled` and files no issue or outcome
comment (the `ai:workflow-heal` / `ai:workflow-heal-escalated` labels in this
repository are still ensured, as for every report). When it is on, the composed body's
`ai:workflow-heal-scope:v1` marker must pass `heal-scope verify` before
`gh issue create`; a missing or invalid marker logs
`reason=scope_marker_unverified`, sends a CRITICAL alert and fails the run.

The conflict resolver's host-only refusal (`sandbox_path_host_only`) has one
deterministic exception: the merged-PR guard hook. Before resolver dispatch,
`scripts/review_conflict_prepare.sh` stages
`.claude/hooks/pr_merge_status_guard.py` from the clean merge of its
`workflow-templates/` twin when each side's live copy equals that side's
template (blob and mode) and the twin itself did not conflict
(`CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED`, default `true`, #6882). A
hook-only conflict is committed as `[ai-merge-resolve]`; on
`orchestrator/project-*` the commit waits for the integration fingerprint
check. Any other shape logs `Guard-hook template merge: skipped reason=<...>`
and is left for the refusal.

CI enforces the agents.md fragment contract (CLAUDE.md §30) on pull requests.
The `static-checks` step `agents.md direct-edit check (CLAUDE.md §30)` runs
`scripts/check_agents_md_direct_edits.py --base <merge^1> --head <merge>` and
fails when an `agents.md` / `AGENTS.md` hunk outside the generated TREE-marker
blocks of `## Repo-tree (auto-generated)` only inserts lines, or when its hunks
outside those blocks add more than `AGENTS_MD_DIRECT_NET_ADD_MAX` lines (default `5`)
beyond the lines they replace. Corrections and removals pass. The step skips
a project's final PR from `orchestrator/project-*` (its sub-PRs were checked
when they merged into the integration branch) and an
`auto/forward-merge-stable-*` fallback PR (release-fold text), only for a
same-repository head targeting the default branch; the release fold itself
pushes without a PR. The
inline implement prompt (`mode-implement-inline.txt` in `implement.yml`), the
review editor prompt (`scripts/review_apply_fixes.sh`, `AGENTS.MD
DISCIPLINE`) and `unattended_system_instructions.md` §25 tell the model to
write an `agents.d/` fragment instead.

An integration-conflict judge answer that is not a valid diagnosis (non-zero
exit, unparseable output, wrong schema, or a `diagnosis` /
`resolution_guidance` over 2000 characters) no longer fails the project.
`invoke_judge_for_integration_conflict` returns 5, logs
`INTEGRATION_JUDGE_INVALID` with the reason and prints the last 20 lines of
the judge log in a `::group::`, each line prefixed with `| ` so the
model-influenced text cannot act as a workflow command. The judge prompt
now states the 2000-character limit. The caller spends one lifetime slot
(`integration_conflict_total_dispatches`), keeps `integration_sync_status`
at `healing`, and the judge runs again on a later tick. A judge that never
answers validly still ends at `INTEGRATION_CONFLICT_LIFETIME_MAX`.

Non-retryable cap re-arm on a new support commit (#6911): a non-retryable
review/autofix cap (`NON_RETRYABLE_FAILURE_REASONS`) gets one more run on the
same head when the gate's verified review-support SHA is on no trusted
`review-autofix-failure:v1` marker for that head yet, while fewer than 3
distinct support SHAs are recorded on the head's markers, the original stop's
included (`DEFAULT_FAILURE_CAP_MAX_REARMS`; `provider_unavailable` markers
are not recorded); the gate logs
`AUTOFIX_FINGERPRINT_CAP_REARMED pr= head= fp= reason= prior_support= support=`.
`count_identical_failures` still filters markers by support version (#6625),
but when that leaves no counted marker it reads the head's trusted markers of
every support version for the re-arm decision, so an older non-retryable stop
past the bound keeps the head capped. The re-armed run's own marker carries
`support=` and restores the cap if it fails again. The 3-strikes cap, the
poller's `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP` skip and the API calls are
unchanged; `autofix-identical-failure-count` adds `rearm=` and
`prior_support=` output lines.

Check-failure triage can route its fix to the failing PR's own head branch:
`CHECK_TRIAGE_PR_HEAD_ROUTING_ENABLED` (default `false`) adds one verified
`- **Integration branch:** `<head ref>`` line to the triage issue. The collect
step checks open state, that both `head.repo` and `base.repo` name this
repository, a matching nonempty check/PR head SHA and a valid ref; a verified
route bypasses the base-branch gate (`base_gate outcome=file
reason=pr_head_route`), because the fix lands on the PR's branch. An unverified
route logs `CHECK_TRIAGE skip reason=routing_unverified detail=<reason>`, sends
a WARNING and files nothing. The diagnose step re-checks the hand-off and, on a
mismatch, logs `error reason=routing_unverified` and fails closed; that step
has no Telegram secret, so the alert is the workflow's failure notification. The body validator admits only that one script-generated line.

Pending operator step for #6831 (activation gaps after PR #6647), recorded by
#7052: the fix needs a change to both `pr_merge_status_guard.py` copies
(`.claude/hooks/` and `workflow-templates/.claude/hooks/`) and possibly to
`CLAUDE.md`. Pipeline editors cannot write `.claude/**` and the isolated
workspace hides `CLAUDE.md`, so the unblock judge chose `operator_step` and
only a trusted operator session lands the change; this note does not
authorize any editor to touch the hooks. Until then the change stays off
behind the placeholder `PR_MERGE_GUARD_ACTIVATION_UNSET_OPERATOR_STEP`.
Nothing reads it, and the guard keeps its current behaviour.
`tests/test_claude_template_live_parity.py` and
`tests/test_pr_merge_status_guard.py` stay enforced; nothing is bypassed, and
nothing about #6831 is reported as validated. When a person lands the change,
both copies are edited together and kept byte-identical, and the same change
replaces this note with a description of the shipped behaviour and retires
the placeholder, as #6755 was.

`MERGE_GUARD_LIVE_PARITY_UNSET_OPERATOR_STEP` is an unset placeholder for a
pending operator step: no workflow, script or test reads it, and setting it
changes nothing. It comes from unblock round 1 on #6832 ("CI failure: CI on
PR #6830"), where `tests/test_claude_template_live_parity.py` reported
`.claude/hooks/pr_merge_status_guard.py` out of step with its
`workflow-templates/.claude/hooks/` twin on PR #6830's head. The two copies
were byte-identical on `main` when this note was written, so the drift may
exist only on that branch. The step: in a trusted checkout, an interactive
operator copies `workflow-templates/.claude/hooks/pr_merge_status_guard.py`
over `.claude/hooks/pr_merge_status_guard.py` byte for byte (template to
live, never the reverse, so the template's fresh PR lookup is kept) and
commits it on the branch that carries the drift: PR #6830's head, or `main`
if the drift has landed there. Do not revert the template, do not add the
hook to `.github/ai/claude_template_divergence.json`, and do not bypass or
relax the parity test, which stays the signal. Pipeline editors cannot write
`.claude/hooks/**` and CI's dry-run sync never prepares it (see "Live
`.claude/` copies and their templates"). Once both copies are byte-identical
on the affected branch, or PR #6830 merges or closes with the two copies
byte-identical on `main`, remove this paragraph and retire the placeholder as
`WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP` was retired.

agents.md fragments (CLAUDE.md §30): PRs document behaviour in
`agents.d/<issue-or-pr>-<slug>.md` instead of editing this file.
`scripts/assemble_agents.py` folds them into the named sections at release
time (release job, same commit as the changelog fold) and in consumers on the
`update_workflows.yml` sync; CI's `check` fails a fragment that would not fold,
and readers (`build_static_context.sh`, the materiality checks,
`tests/agents_doc.py`) see pending fragments through `render`.

Provider-outage activation gaps (issue #7099): the consumer `ai-review.yml`
`provider-outage-resume` job exposes `outputs.paused` (step id
`provider_outage`; both early skips write `paused=false`), and
`security-hold-sweep` runs with `needs: provider-outage-resume` and
`!cancelled() && ... && needs.provider-outage-resume.outputs.paused != 'true'`,
so it skips dispatch during an outage and still runs when the outage job is
disabled, skipped or failed. In `review_autofix.yml`, "Mark linked issues
review-blocked (autofix exhaustion)" (`id: mark_review_blocked_exhaustion`)
writes `labelled_issue_numbers` (digits only, from
`SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME=applied`, which
`scripts/label_helpers.sh` sets alongside its unchanged return code 0) and
`labelled_issue_numbers_resolved=true`; the outage marker step marks exactly
that set and falls back to the old derivation only when the output is missing
(`PROVIDER_OUTAGE op=mark_label outcome=fallback reason=labelled_set_unavailable`).

Pre-review conflict preparation keeps a folded fragment the PR changed.
`scripts/review_conflict_prepare.sh` stages the PR's copy of a top-level
`changelog.d/*.md` or `agents.d/*.md` path in a modify/delete conflict with
index stages 1 2 (the base branch's release fold deleted it, the PR changed
it), before the manifest union block and the model resolver
(`CONFLICT_FOLDED_FRAGMENT_KEEP_ENABLED`, default `true`). A fragment-only
conflict is committed as `[ai-merge-resolve]` without a model run; on
`orchestrator/project-*` the commit waits for the integration fingerprint
check (`_ff_defer_commit`, shared with the manifest-only deferred commit).
`scripts/check_resolver_diff.sh` now also fails when a conflicted path the
resolver did not touch is still unmerged, naming it with its index stages.

Check-failure triage defers instead of filing when the base branch's newest run
of the failing check is still in progress (`CHECK_TRIAGE skip reason=base_pending
base=<ref> pr=<n> check=<name>`). The base-gate reads return `pending` for that
case: the workflow-run read when the newest push run on the base branch has not
completed, the check-run read when every returned base check run fits on the page
and at least one has not completed. #7140 was filed for PR #7135's own test
failure while main's CI run was in progress; a PR-specific failure belongs to
the PR's review/autofix loop, and a real base breakage is filed by the next
triage after the base run completes.

Workflow failure heal reports for an orchestrator project carry its validation
runs: `select_failed_runs` (used by `build_issue_payload`) also picks failed runs
whose display title ends in `[tracking:<issue number>]`, the validate wrappers'
run name. A label-escalation report that still links no run, or whose linked
runs' job logs were all unreadable, is fingerprinted on
`label:<label>|source:<repo>#<n>` instead of `label:<label>`, so run-less
reports from different issues or repositories no longer share one heal lineage
(binance-blessings#249's first `ai:harness-broken` report was capped at
generation 4 by three unrelated heals).

Telegram alert levels (human-only paging): `scripts/tg_helpers.sh` documents the
level convention. `DEBUG` means the pipeline is already handling the event
(dispatch, retry, re-issue, stall-recovery step, auto-answer, success ping).
`WARNING` and above mean a person has to act. The default `ALERT_MSG_LEVEL` is
`WARNING` in `tg_helpers.sh`, in every workflow fallback
(`env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'WARNING'`) and in
`workflow-templates/ai-review.yml`. A new alert must pick its level by who acts
next. An auto-handled path that can repeat forever needs a once-per-condition
stuck alert, not a page per attempt. The existing once-only helpers in
`scripts/orchestrate_poll_process.sh` are:
- `_standalone_conflict_count_attempt`: one `WARNING` after
  `STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS` (default 3) resolution attempts on
  one PR head, counted in an `<!-- ai:standalone-conflict-attempts:v1 -->`
  PR comment read from the sweep's GraphQL batch; the marker records
  `alerted=1` only after Telegram returns a message ID, so failed sends retry
  (a failed confirmation write can cause a duplicate page);
- `_noop_gate_failure_alert`: one `ERROR` per PR head and force-merge gate,
  deduplicated by an `<!-- ai:noop-force-merge-gate-alert:v1 -->` PR comment;
- `_merge_deferral_cap_alert`: one notice when `MAX_MERGE_DEFERRALS` is
  reached;
- `_judge_output_failure_alert`: `CRITICAL` only at `JUDGE_OUTPUT_FAILURE_MAX`.

Final integration merge and project-complete messages stay `CRITICAL` on every
completion path so they remain visible. `tests/test_alert_levels_human_only.py`
pins the default, the helpers, and the ban on invalid level strings
(`WARN`, `INFO`, …).

Workflow failure heal, `ai:scope-blocked` reports (Q81, hylifegroup.com#75): `scripts/workflow_failure_heal_report.sh` reads `gh api user` for `ai:scope-blocked` as well as `ai:needs-human` (still one read per report) and passes it as `build-issue-payload --pipeline-login`. `build_issue_payload` adds `issue_author_login`, `issue_author_association` and `reporter_pipeline_login` for that label only; `validate_payload` keeps a login only when it matches the GitHub login pattern and an association only when it is a GitHub value, and drops anything else. `scripts/workflow_failure_heal_intake.sh` prints `Issue author: <login> (association: <value>)` and `Pipeline account (the reporter's identity): <login>`, with `unknown` for an older reporter. `prompts/mode-workflow-failure-heal.txt` (and its `_templates/` copy) tells the diagnosis to compare the guard's grant rules between `HEAL_SOURCE_DIR` and `HEAL_BRANCH_TIP_DIR`, to classify a rejection the newer guard grants as `already-fixed`, and never to suggest a `files_touched` block listing only the rejected automation paths on an issue that has none, unless the issue shows the change needs no other file.

Unmerged PR close binding (#7160): in `issue_pr_status.yml`'s "Update linked
issue labels when PR closes" step, a PR closed without merging labels
`ai:closed`, closes and finalizes the lineage of an issue only when the PR head
repository (`PR_HEAD_REPO`, from the event payload) is this repository, the
head branch is `ai/issue-<n>` for that issue, and the PR author
(`PR_AUTHOR_LOGIN`) equals the `GH_PAT` login. The login is read with one
`GET /user` at most once per run, and only after an issue passes the first two
checks; a failed or empty read fails closed. Any other referenced issue is left
unchanged and kept out of `LINEAGE_FINALIZE_ISSUE_NUMBERS`, with the line
`Unmerged close of PR #<pr> left issue #<n> unchanged (reason=foreign_head|unbound_issue|identity_unavailable|author_mismatch)`.
Merged-PR rules (#5776, #6631) and the tracking-issue skip are unchanged.

`INTEGRATION_CONFLICT_LIFETIME_MAX` counts conflicts that are still open, not every dispatch a project ever made. Each resolver or judge dispatch records the default-branch commit it was asked to merge in (`integration_conflict_episode_base_sha`). When `heal_integration_branch_conflict` next runs and the integration branch contains that commit, `integration_conflict_reset_on_progress` sets the total, per-episode dispatch and unresolved-tick counters back to `0` and logs `INTEGRATION_CONFLICT_EPISODE_RESOLVED`. A conflict the resolver never lands keeps counting and still ends the project at the cap.

The two terminal paths (lifetime cap, failed judge escalation) save the status the project failed from as `integration_failed_from_status`. Each tick, `recover_failed_integration_if_mergeable` checks a failed project whose `integration_sync_status` is `failed`: when the final PR targets the repository default branch (`base.repo.default_branch` of the same PR read) and has been merged, or is open, reported mergeable by GitHub and a local `git merge-tree` of the default branch into the integration branch is clean, the saved status is restored (or, for a failure from before the field existed, the status of the newest trusted state snapshot that is not `failed`, found with `scripts/orchestrate_state_v2.py extract --skip-status failed`, then the legacy V1 comments). Only `in_progress`, `merge_conflict`, `validating`, `validation-fixing`, `security-pass` and `security-pass-fixing` are restored. A merged final PR always restores `in_progress`, whatever status the project failed from (no prior status is needed), whose external-finalize path records the merged PR (completeness and security-pass gates included) and completes the project; the validation and security-pass arms would try to validate an already-merged branch. A failed state write, local or to the state comment chain, puts the failed state back and announces nothing (`reason=state_write_failed`). The counters start from `0`, the completion-status comment returns to `in-progress`, and the project's tick ends there; the next tick continues from the restored status. One REST read of the final PR per tick, only for a project in that failed state.

The §19 check `.github/workflows/lint-pr-body-auto-close.yml` lints the PR title and body on every PR into `main` or `stable`. For PRs into the default branch it also reads the PR's commit messages (`GET pulls/<n>/commits`, paginated, at most 250 commits) and passes them to `scripts/lint_pr_body_auto_close.py --commit-messages-file`. Squash merges here copy commit messages into the merge commit, so a closing keyword in any commit reaches `main`. A failed commit read fails the check. The script treats colon forms (`Closes: #N`, `Resolved:#N`) as closing keywords. GitHub closes issues on them: the squash merge of #7190 closed tracking issue #6664 that way.

The plan workflow decides whether the checkout matches the issue's integration branch before the model runs ("Capture planning ref context", `PLANNING_REF_MISMATCH=true|false|unknown`) and shows the result in PLANNING REF CONTEXT. The planner prompt (`scripts/run_plan_codex.sh`, `prompts/mode-plan.txt`) asks for `BLOCKED: integration branch mismatch` only when that line reads `true`. The model's own git is the isolated snapshot from `scripts/codex_isolated_workspace.py` (branch `isolated`, a fresh commit ID), so it never matches the planning ref. When an attempt still answers that block while `PLANNING_REF_MISMATCH=false`, `run_plan_codex.sh` retries within its 3 attempts and appends one `=== RETRY NOTE ===` to the prompt. On the last attempt it keeps the answer, so `plan.yml` blocks as before. The check reads the first `BLOCKED:` line after removing closed code fences, the same way `plan.yml`'s parser does.

- **Unblock `operator_step` fixes ship on.** The judge's `placeholder` decides who finishes an `operator_step`. An `_ENABLED` flag means the fix-up alone clears the block. The fix-up body (`scripts/unblock_actions.py` `_fixup_body`) tells the implementer to ship the new code on with the flag as an off switch: unset, empty or `true` keep it on, `false` turns it off, and anything else turns it off with a warning. `plan()` then files only the fix-up, with no `ai:operator-step` entry and no Telegram WARNING, and the verdict comment reads "Ships on by default; `X=false` turns it off." An `_UNSET_OPERATOR_STEP` placeholder still records the operator step, pages, and keeps the new code skipping until a person sets the value (`_placeholder_needs_person`). The prompt (`prompts/mode-judge-unblock.txt`) forbids defaulting a fix off only to page a person to switch it on.

A poll tick that moves a project forward queues one more poll run (Q99). GitHub runs the poller's `*/5` cron every 25 to 60 minutes, so without this a project that could take its next step at once waited for the next cron run. `scripts/orchestrate_poll_process.sh` takes a progress fingerprint (`poll_progress_fingerprint`) of each tracking issue's state when the tick loads it. The fingerprint is a sha256 over project `status`, `current_wave`, `integration_sync_status`, `final_merge_status`, `security_pass_status`, `security_pass_cycle`, and each wave issue's `id`, `github_issue` and `last_seen_phase`. After every successful `post_state_comment`, `record_poll_progress_if_advanced` compares the persisted state against it and appends the tracking issue to `${RUNTIME_DIR}/poll_progress_tracking_issues` when they differ. Timestamp and counter updates leave the fingerprint unchanged. The `orchestrate_poll.yml` step "Queue a follow-up poll after progress" runs after a successful tick with work. It passes the first recorded tracking issue to `scripts/orchestrate_force_tick.sh` (reason `poll-progress`, `FORCE_TICK_COOLDOWN_SECONDS=300`), which dispatches the caller workflow named by `github.workflow_ref` (`internal-orchestrate-poll.yml` when that cannot be parsed). The poll job's concurrency group keeps at most one pending run, so the dispatch queues behind the current run, and the cooldown keeps a chain at or below the cron's nominal cadence. The cooldown is kept in the AI-memory ledger, so with `AI_MEMORY_ENABLED=false` the step queues nothing (`reason=cooldown_unavailable`); a failed ledger read or claim still dispatches, bounded by the concurrency group. `POLL_FOLLOW_UP_ON_PROGRESS_ENABLED=false` turns it off. No GitHub API calls besides the dispatch.

`PUSH_DESTINATION_GUARD_UNSET_OPERATOR_STEP` is an unset placeholder for a
pending operator step: no workflow, script or test reads it, and setting it
changes nothing. It comes from unblock round 1 on #7220 (stop `blocked`). The
`claude/*` push allow rules in `.claude/settings.json` still let a branch
deletion or a refspec that writes the default branch through without a
prompt (`git push origin claude/x +main`, `git push origin claude/x :release`),
and a repository whose default branch is not `main` / `master`, or whose
default branch cannot be resolved, gets no ask at all. The fix is an edit to
the live `.claude/hooks/pr_merge_status_guard.py`, which pipeline editors
cannot write, and CI requires that file to stay byte-identical to
`workflow-templates/.claude/hooks/pr_merge_status_guard.py`
(`tests/test_claude_template_live_parity.py`). The step: in a trusted
interactive checkout, an operator edits both hook copies identically so that
`git push` asks for confirmation for every branch deletion (`--delete`, `-d`,
a combined short flag containing `d`, and a colon deletion such as
`:branch`), on any remote including the session's own `claude/*` branches;
for any refspec whose destination is the resolved default branch, including
`+`-prefixed refspecs, `refs/heads/<default>` destinations, a `HEAD` refspec
that resolves to it, and pushes with several refspecs; and, when the default
branch resolves neither locally (`symbolic-ref origin/HEAD`) nor remotely
(`ls-remote --symref`), for any destination outside `claude/*`. Targets:
`_push_targets` (deletions currently return no target and `+` is stripped
silently) and the default-branch skip in `_evaluate_bash` (commits on the
default branch stay skipped). Reuse the existing confirmation-reason path;
`CLAUDE_PR_MERGE_GUARD=off` keeps disabling only the merged-PR check, and the
new asks ship on with no extra switch. Add focused cases to
`tests/test_pr_merge_status_guard.py` (which already covers both copies),
invert `test_deletion_on_origin_does_not_check_merged_pr` to expect `ask`
while still making no PR query, and update the §21.E limitation note in
`CLAUDE.md` and `workflow-templates/CLAUDE.md` in the same commit. Do not
change the `.claude/settings.json` allow/ask rules, do not add the hook to
`.github/ai/claude_template_divergence.json`, and do not gate or disable the
existing hook behaviour. Once both hook copies with the new checks are on
`main`, remove this paragraph and retire the placeholder as
`WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP` was retired.

CI-triage PR-branch routing (issue #7042): with
`CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` on (the default; `false` turns it off),
`scripts/resolve_integration_ref.sh` routes a verified `ai:check-triage` issue
to its failing PR's head branch for clarify, plan, implement, clarify-respond
and validate. It verifies the label, the canonical marker header, an
OWNER/MEMBER/COLLABORATOR author, the body's PR line, the live PR (open, head
and base in this repository, matching head ref) and the branch. Any failure,
including an unreadable or unparseable issue while the flag is on, exits 3,
and the phase fails closed instead of using the default branch. Other resolver
exits keep their existing handling (75 and 2 fail closed in implement and
validate). An explicit `Integration branch:` line still wins. An issue with
no `ai:check-triage` label whose only triage marker is a
`check-failure-triage:pr=<n>` line (the unblock judge's CI-failure reissue,
#7032) is not routed and keeps the default branch.
Clarify alone keeps the default branch on a `reason=pr_not_open` refusal
(the PR merged or closed), so the check-triage resolution gate (#7095) can
close the issue with evidence; the refusal is logged as a `::notice::`. While
routing is on, clarify, plan and clarify-respond also fail closed on a
rate-limited resolver (exit 75, `INTEGRATION_REF_RESOLVE outcome=rate_limited`)
instead of using the default branch.

Check-failure triage can also route its fix with a `Target branch:` line:
with `CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED=true` (default `false`) a new
triage issue carries one `Target branch: <head ref>` line, written only for a
verified open same-repository PR head that is not the default branch. A
verified route bypasses the base-branch gate (`base_gate outcome=file
reason=target_branch_route`), because the fix lands on the PR's branch. The
diagnose step re-checks the flag, the ref shape and that the route equals the
recorded head ref (`routing outcome=skip reason=handoff_mismatch` otherwise);
the final body check allows exactly that line, once, above the first `---`.

Heal intake scope-marker guard and resolver path categories (issue #7059):
before filing any heal issue (`workflow-defect`, `inconclusive`,
`base-self-inflicted` or `consumer-app-defect`, all labelled
`ai:workflow-heal` and refused by implement preflight without a verified
marker), `scripts/workflow_failure_heal_intake.sh` checks its own rendered
scope marker with `workflow_failure_heal.py heal-scope check-marker` (the
implement-side parser and shape rules, 8 KiB read cap, no symlinks). A
missing or invalid marker files nothing: it logs
`WORKFLOW_HEAL skip reason=scope_marker_unresolved ... status=<status>`, sends
a CRITICAL alert and fails the run (a run-less report skips quietly, #7424), so
the next report of the fingerprint retries. Only `WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED=false` (script
environment) restores filing. The conflict resolver's nameless
`sandbox_path_unsupported` stop is now preceded by one
`::error::Conflict resolver: conflicted path(s) cannot enter the sandbox:
unsafe=<n> host_only=<m> categories=<c,...> depths=<1|2|3+|none>` line, which
becomes the failure's First error. Categories come from a closed set
(`symlink`, `symlink_in_path`, `special_file`, `unsafe_name`, `unreadable`,
`other`) and no path name is printed; the per-path report writes unsafe
entries as `unsafe<TAB><category><TAB><depth>`. Admission rules and failure
reasons are unchanged.

Check-triage target-branch guard (issue #7064): with
`CHECK_TRIAGE_BRANCH_GUARD_ENABLED=true` (default `false`), `plan.yml`'s
`Check-triage target-branch guard` step blocks planning of an `ai:check-triage`
issue through the existing `BLOCKED:` handler instead of planning it on the
default branch. It reads only the labels and `steps.refctx.outputs.resolver_status`
(`resolved`, `routed`, `none`, `failed`, `unavailable`). `resolved` (an
explicit `Target branch:` / `Integration branch:` line, or the tracking issue's
line) passes with `reason=metadata_present`; `routed` (a verified PR head from
`CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED`, matched from the resolver's own
`CI_TRIAGE_PR_BRANCH_ROUTING ... outcome=routed` line) passes with
`reason=pr_head_routed`; anything else, or unreadable labels, blocks. It never
infers a branch from issue prose.

Check-failure triage historical mode (#7093): `check_failure_triage.yml` takes
optional `historical_run_id` / `historical_job_id` /
`historical_repro_artifact_prefix` inputs (empty keeps the normal path). With
both ids set, `scripts/check_failure_triage.sh` triages a closed same-repo PR
(fork guard kept; missing head repo fails closed). Its fingerprint adds
`|run=<id>|job=<id>`, the dedup listing uses `state=all`, and the base gate is
bypassed. It binds the run and job (repository, `ci.yml` path,
`pull_request` event, head SHA and branch, failed conclusion) before
fetching the job log on the host. The log is redacted with
`workflow_failure_heal.py redact-stream` (now staged into trusted support),
and only the first failure window reaches the isolated diagnosis. On a 404/410
log, it parses same-run reproduction artifacts host-side and recounts them
(`reproduced` / `complete_non_reproduction` / `incomplete`; complete needs
the failing head SHA itself tested, every group, shard and step and zero jq/PyYAML skips). The single-use
`check-failure-triage-historical.yml` (push to `main` + dispatch, PR #6611
run 37922279826 / job 113792812346) drives it. Its guard dedups on trusted
issues and caps failed runs at 3, and its `repro` matrix runs without
secrets. Log keys: `CHECK_TRIAGE historical_mode`,
`historical_evidence source=log|repro outcome=…`, `error
historical_binding_mismatch|historical_evidence_unavailable|historical_inputs_incomplete`,
and `HISTORICAL_TRIAGE` in the workflow.

Check-triage already-resolved gate (#7095): clarify's `Decide clarify route`
step runs `scripts/check_triage_resolution.py gate` (support-ref copy in
`CLARIFY_HOST_PYTHON_DIR` with `workflow_failure_heal.py`, `python3 -I -B`) for a trusted
`ai:check-triage` issue (after the security-dependency hold). It closes the
issue (evidence comment with `<!-- ai:check-triage-resolved:v1 issue= pr= sha= -->`,
`ai:closed`, closed as completed; step output `check_triage_resolved=true`,
`gate=check_triage_resolved`) only when GitHub shows the linked PR merged and
the same check, or for workflow-run triage every failed job name, succeeded on
a default-branch commit that contains the merge commit. Unverified evidence
keeps the normal path and posts the failing job's traceback once
(`<!-- ai:check-triage-traceback:v1 issue= job= -->`); both markers count only
from the GH_PAT account. A write failure after verification exits 3 and fails
the step. Kill switch `CHECK_TRIAGE_RESOLVED_GATE_ENABLED` (default `true`).

Security-pass audit tree (#7126, security-pass fix cycle 2). The Q113 checkout that the security pass prepares with `keep_trusted_support=true` is trusted for execution only; it is no longer what gets audited. After the model config is written, `run_security_pass_inline` records the refreshed checkout as `security_pass_support_root` (`pwd -P`) and checks that its support assets exist. It then calls `_security_pass_prepare_audit_tree <head>`, which runs `git -c core.hooksPath=/dev/null worktree add --detach` of the integration head into `${RUNTIME_DIR}/security-audit-tree.XXXXXX/tree`. `RUNTIME_DIR` must be absolute, and the tree must be at the head and clean. The audit runs in a subshell with that tree as its cwd, with `SECURITY_AUDIT_SUPPORT_DIR=<support root>` and an absolute `SECURITY_AUDIT_FP_EXCLUSIONS=<support root>/scripts/security_audit_fp_exclusions.json`, and executes `<support root>/scripts/codex_heartbeat.sh` and `<support root>/scripts/security_audit.sh`. Its snapshot, diff scope and oversized-file chunks therefore come from the integration head's committed content, while every executable, the prompt and the exclusion catalog come from the verified support copy. `_security_pass_remove_audit_tree` runs before the exit code is checked; a removal failure only warns. A failed preparation fails the pass closed as `engine_unavailable`. Later poller steps still see the overlaid checkout.

Consumer sync verification (#7128): the `release` jobs of `test-and-mark-stable.yml` and `mark-stable.yml` run `scripts/build_sync_manifest.py build` on the final release commit, attest the manifest with `actions/attest-build-provenance@v3` before the tag step (`id-token: write`, `attestations: write`), and upload `sync-manifest.json` to the release (`--clobber`; a rerun rebuilds it byte for byte). The attest and upload steps run only when the release is published (`!inputs.dry_run` plus the `release_switch` and `release_tagging` outputs), like the release-manifest steps. The manifest schema is `coding-workflows.sync-manifest.v1`. It carries `files` (every root-level entry plus `workflow-templates/` and `scripts/`: a sha256 per regular file, `symlink:<target>` per symlink) and `allowed_consumer_paths`. In an allowlist entry `*` does not match `/`.

In `update_workflows.yml`, the step "Verify signed sync manifest" (`verify_manifest`) runs directly after the fetch and before any upstream script. It performs four checks:
- the `vX.Y.Z` tag whose peeled commit equals the fetched `stable` SHA;
- `gh release download` of that tag's `sync-manifest.json`;
- `gh attestation verify` with the identity regex for the two release workflows on `refs/heads/stable` and `--deny-self-hosted-runners`;
- the manifest's commit, version and exact file set, compared by an inline verifier that imports nothing from the tree it checks.

"Commit and push updates" (`commit_push`) rejects any staged path outside the allowlist. With `UPDATER_PR_DELIVERY_ENABLED=true` (the default) it then uses the `auto/update-workflows-*` pull request and ignores `update_via_pull_request`; with that variable `false` it delivers through `ai-sync/stable`:
- an open PR is reused only when its tree already matches; otherwise it is closed and a new PR is opened, because a push to an open PR fires `synchronize` and the head gate would withdraw auto-merge. The branch push is leased to the tip read just before it (`reason=lease_mismatch_or_push_rejected` on a race);
- a PR left `awaiting_merge` sends the Telegram notification at `WARNING`;
- the updater enables auto-merge, falls back to a direct merge, or else leaves the PR `awaiting_merge`;
- `update_via_pull_request: false` restores the direct push (only while `UPDATER_PR_DELIVERY_ENABLED=false`).

The consumer template gates the job on `vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false'` and grants `pull-requests: write`. The review gate (`scripts/review_autofix_step_evaluate_gate.sh`, before the deterministic skip) runs `scripts/review_workflow_sync_skip.sh` from the verified head-gate checkout. It skips the panel (`SKIP_REASON=workflow_sync_managed`) only for an `ai-sync/stable` head in this repository with a complete file list (rename sources included) inside the allowlist and no `force-review`, and it never sets `DETERMINISTIC_SKIP`, labels, comments, posts a status or merges. An `ai-sync/stable` head that fails that check is reviewed normally: the gate drops any deterministic skip it would otherwise get (`AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=workflow_sync_unmanaged`), so it never reaches `deterministic-skip-merge`. Both checks share one paginated `/pulls/{n}/files` read (`gate_fetch_pr_files`). The skip posts no `ai-review/head-gate` success, so a consumer that requires that check merges the sync PR by hand. Failure codes:
- `ERR_SYNC_MANIFEST_NO_VERSION_TAG`
- `ERR_SYNC_MANIFEST_MISSING`
- `ERR_SYNC_MANIFEST_SIGNATURE`
- `ERR_SYNC_MANIFEST_COMMIT_MISMATCH`
- `ERR_SYNC_MANIFEST_FILE_MISMATCH`
- `ERR_SYNC_PATH_NOT_ALLOWED`

Standalone PR sweeps without a project (#7177): the poller's standalone PR
conflict sweep and noop-suspicious recovery sweep (with their
`STANDALONE_CONFLICT_STUCK` and `NOOP_FORCE_MERGE_GATE_ALERT` alerts) run
inline at the end of "Process each tracking issue" when a tracking issue is
open. On ticks without one, the `orchestrate_poll.yml` step "Run standalone PR
sweeps without active projects" runs `scripts/orchestrate_poll_process.sh` with
`STANDALONE_PR_SWEEP_ONLY=true` (default `false`). That mode skips the tracking
loop, `prime_phase_concurrency_snapshot`, standalone stall recovery, the
staged-support latch release, the unblock scan and `close_merged_issues_sweep`.
The complementary `has_work` gates keep the two paths exclusive. The step skips
with a warning when the staged poller lacks the mode, is `continue-on-error`,
and forwards `ALLOW_WORKFLOW_EDITS`. "Stage workflow support files" also stages
`validate_editor_audit.sh` (optional), which the no-op sweep's force-merge
gate B needs; without it gate B fails closed. Both sweeps send Telegram
alerts at `STANDALONE_SWEEP_ALERT_MSG_LEVEL` (from `vars.ALERT_MSG_LEVEL`, default
`WARNING`) instead of inheriting `find_tracking`'s all-smoke-fixture `SILENT`,
then restore `ALERT_MSG_LEVEL`. An empty value keeps the poller level, the value is
matched case-insensitively, and a value outside
`DEBUG|WARNING|ERROR|CRITICAL|SILENT` is ignored with a warning.

Standalone stall recovery on ticks with no active project (issue #7183):
when `orchestrate_poll.yml` finds no `ai:orchestrator-tracking` issue,
`find_tracking` sets `has_work=false` and `standalone_recovery=true` (unless
`ENABLE_STANDALONE_STALL_RECOVERY` is off). The poller toolchain steps then
still run, and "Run standalone stall recovery without active projects" runs
the poller with `STANDALONE_RECOVERY_SWEEP_ONLY=true`, which primes the phase
concurrency caps, runs `run_standalone_stall_recovery` and exits before the
per-project loop and the post-loop sweeps. That step runs only when
"Validate required environment variables" (`validate_env`) and
"Stage workflow support files" (`stage_support`) have outcome `success`; both
are `continue-on-error` on no-project ticks, so a missing key or a staging
failure skips recovery without failing the tick, and `validate_env` is
skipped when the kill switch is off. A staged poller without the mode logs a
warning and runs nothing. The latch release and unblock scan steps that
follow are gated on `!cancelled()` plus a successful `find_tracking`, so
they still run when recovery or a toolchain step fails, but not when the
tracking-issue lookup itself failed. `close_merged_issues_sweep` runs only
when the full poller runs: on ticks with an active project, or when the #7284
standalone probe sets `has_work=true`.

Heal-scope recovery (#7191, #7323, the operator step for #7082) is gated by
`HEAL_SCOPE_RECOVERY_ENABLED`, on by default: unset, empty or `true` (any
letter case) keeps it on, `false` turns off recovery only, and any other
value also turns it off with
`::warning::WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=invalid_switch`.
It does not need `HEAL_INTAKE_SCOPE_GUARD_ENABLED`. A report whose OIDC
identity verified (`report_auth=verified`) and whose fingerprint matches an
open heal issue with no `ai:workflow-heal-scope:v1` marker at all (heal-scope
status `missing`), authored by the intake's own account, opens a replacement
instead of a duplicate occurrence. `_heal_scope_recovery_evaluate` in
`scripts/workflow_failure_heal_intake.sh` re-runs the budget without the
original (its body never feeds the lineage) and turns the decision into an
open, so the replacement goes through the normal scope freeze, diagnosis and
scope render. `_heal_intake_scope_guard_permits_create` then runs its
unchanged `heal-scope verify` check for this path even while the creation
gate is off (`WORKFLOW_HEAL scope_guard outcome=recovery_only`; only
`HEAL_SCOPE_RECOVERY_ACTIVE`, which the script resets before use, selects
it), so a missing or forged scope refuses creation with
`reason=scope_marker_unverified`, a CRITICAL alert and a nonzero exit, and
the original stays open. Every other report with the gate off still ends with
`skip reason=scope_guard_disabled`.
After `gh issue create` succeeds, the original gets one comment ending in
`<!-- ai:heal-scope-recovery:v1 replaced_by=<n> repo=<owner/repo> -->` and is
closed as "not planned"; its body is never edited and its editor never runs.
Only a marker posted by the intake's account counts
(`workflow_failure_heal.py heal-scope-recovery check`): later reports on an
original that carries it go to the replacement as occurrences
(`outcome=redirect`, the close is retried). A marker present in any form,
another author, an unverified report, a failed `/user` or comment read, or a
second budget that does not open keep today's duplicate handling. A
classification that opens no issue posts the usual occurrence on the
original (`outcome=no_replacement`). Logs:
`WORKFLOW_HEAL heal_scope_recovery outcome=eligible|skip|redirect|replaced|no_replacement reason=`.

Clarify sandbox base image pre-seed (#6985, #7215): `scripts/clarify_base_image_preseed.sh [DOCKERFILE]`
pulls the `FROM` image of `scripts/clarify_sandbox/Dockerfile` from a Docker Official Images
mirror and tags it under its Docker Hub name, so the build without `--pull` does not hit Docker
Hub's anonymous-pull 429. `clarify_image_build` in `scripts/clarify_isolated_run.sh` first tries
the prebuilt GHCR image (`sandbox_image.sh ref`, then one pull bounded by
`SANDBOX_IMAGE_PULL_TIMEOUT_SECS`, default 60) and runs the helper (output to stderr, credentials
unset) only before a local build, so slow mirrors never delay a pullable prebuilt image. A failed
prebuilt pull is retried once by `sandbox_image.sh build`; when that hand-off fails (its unseeded
local build after a label mismatch or failed re-pull), the helper runs and the image is built
again. A failed `sandbox_image.sh ref` logs `SANDBOX_IMAGE family=clarify outcome=ref_failed` (not when `SANDBOX_IMAGE_REGISTRY` is `off` or invalid).
This covers clarify, plan,
clarify-respond, check-failure triage, the unblock judge and the Claude engine smoke; clarify's
"Pre-seed clarify sandbox base image" step also calls it with the repository-variable overrides.
It always exits 0. Defaults live in the helper: `CLARIFY_BASE_IMAGE_MIRRORS` unset means
`public.ecr.aws/docker/library mirror.gcr.io/library` (set but empty means no mirrors) and
`CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS` is `300` (30-1800). A support tree without the helper logs
`reason=helper_missing` and builds as before.

Superseded CI-repair issues (#7216, follow-up of #7185): `plan.yml`'s "Close
superseded CI-repair issue (merged PR)" step runs after "Skip stale /answer
comments" and before the `/answer` claim. It runs
`scripts/plan_superseded_merged_pr.py classify` from the verified support
checkout only (`python3 -I -B`, never the branch checkout). An issue qualifies
when it carries `ai:check-triage` with a trusted author (pipeline login or
OWNER/MEMBER/COLLABORATOR) and its header (before the first `---`) has a Pull
request line for this repository matching the `check-failure-triage:pr=`
marker, or when the pipeline login wrote it and its line-start re-issue
references ("Re-issued from #N", "Re-issued by the unblock judge from #N",
"- Replaces: #N"; never follow-up references such as "Accepted with this
follow-up") lead, within `ORIGIN_MAX_DEPTH` / `ORIGIN_MAX_ISSUES`, only to
trusted triage issues naming one PR and check. `ai:security` issues are not
walked. A merged, same-repository PR whose named check succeeded on its final
head gets one `<!-- ai:plan-superseded:v1 pr=<N> head=<sha> -->` comment
(deduplicated on the pipeline login's marker), `ai:closed`, phase labels
removed, a "not planned" close and `SKIP_PLAN=true`. Merged with the check
failed or pending: planning continues with a "MERGED PULL REQUEST FACTS"
block, and when the resolved ref is the PR's former head the step checks out
its base branch (`PLAN_REROUTED_REF`, mismatch forced `false`); if that
checkout fails, the run skips planning (`SKIP_PLAN=true`, WARNING) rather than
planning on the former head. Every other
case, including read failures, plans as usual. Kill switch
`PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED` (default `true`). Implement's own
branch resolution handles a `reason=pr_not_open` refusal separately (#7540).

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

Routed fix PRs get CI, and resolver staging fails closed (issue #7283):
`ci.yml` runs pull-request CI for every base (`pull_request: branches:
["**"]`; push CI stays on `main`, `stable` and `orchestrator/project-*`). A
fix PR routed to a failing PR's head branch therefore gets its own
`CI / lint`, and `_pr_auto_merge_required_ci_check_name` in
`scripts/pr_checks_lib.sh` defaults to `lint` for every base in this
repository (consumers stay `none`). The "Resolve integration ref" step of
`plan.yml`, `clarify.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`
and `validate.yml` fails with `::error::INTEGRATION_REF_RESOLVE
outcome=resolver_unavailable reason=stage_failed|pin_failed|script_missing
ci_triage_routing=on` and writes no `ref` when the resolver cannot be staged,
pinned (the unpinned main-fallback clone counts as `pin_failed`) or found
while `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` is on (`true`, `1`, `yes` or
`on`, any case, as the resolver parses it). With the flag off, the
default-branch fallback is unchanged.
The `tests-hooks-and-orchestrator` job's "Prepare template-only PR live copies
for parity tests" step now prepares every pull request except one into
`stable` (`github.base_ref != 'stable'`), so a routed fix PR into an
`ai/issue-*` head gets the same dry-run preparation as a PR into `main`.
`scripts/check_failure_triage.sh`'s base gate skips a failed CI workflow run
whose base is `ai/issue-<n>` and has no completed push run
(`CHECK_TRIAGE skip reason=routed_head_base`), because push CI never runs on
that branch and the routed fix PR's own review/autofix loop owns the failure.

Standalone stall recovery without a tracking project (issue #7284, activation
gap from PR #7044): `run_standalone_stall_recovery` runs only inside the full
poller, which `orchestrate_poll.yml` started only when an open
`ai:orchestrator-tracking` issue existed. When there is none, the
`Find active tracking issues` step (`id: find_tracking`) now makes one aliased
GraphQL count probe while `ENABLE_STANDALONE_STALL_RECOVERY` is on (default
`true`, same truthy rule as the poller). It counts open issues for each of the
poller's seven pipeline labels, plus open issues whose comments carry
`AI_STANDALONE_STALL_STATE_V1` or `ai:clarification-questions`. These are the
same searches as `_fetch_standalone_marker_issues_graphql`. Any count above
`0` sets `has_work=true`, so the full poller runs with an empty
`tracking_issues.json`; its post-loop runs standalone recovery, the
staged-support latch release, the unblock scan, the close-merged sweep and the
standalone conflict sweep. A failed or malformed probe also sets
`has_work=true` (fail open, with a warning). All counts `0`, or the switch
off, keep `has_work=false`, and the no-project sweep steps run as before. The
#7183 `standalone_recovery` output is unchanged (`true` while the switch is on),
so on an all-zero probe "Run standalone stall recovery without active
projects" still runs recovery. `has_tracking` still means an open tracking
issue exists. Only counts are read and logged.

Non-retryable failure backfill (issue #7291, activation gap of PR #7065):
`scripts/workflow_failure_heal_non_retryable_backfill.sh` runs as the
"Backfill non-retryable autofix failure reports" step of
`review_autofix_sweep.yml` (from the trusted `.ci-rerun-support` checkout) and
as the `non-retryable-backfill` job of the consumer `ai-review.yml` schedule
(`contents: read`, no `id-token`). It trusts only comments by the `GH_PAT`
login (`gh api user`, once per tick) and lists open PRs, least recently
updated first, with their last 100 comments in one paginated GraphQL query
(10 PRs per call, `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS`, default 50;
a cut-off listing logs `reason=listing_truncated`). With more than `MAX_PRS`
open PRs each tick starts at a rotated window (`GITHUB_RUN_NUMBER` modulo the
window count, logged as `reason=window_rotated`; one `totalCount` call plus one
pageInfo-only call per 100 skipped PRs), so idle ineligible PRs cannot hide
later ones. The sweep's `head_ref_filter` input reaches it as `HEAD_REF_FILTER`
(head-branch substring, as in the enumerate step).
`workflow_failure_heal.py non-retryable-backfill-candidates`
(`select_non_retryable_backfill_candidate`) picks a same-repository PR whose
newest trusted current-head `review-autofix-failure:v1` marker names a
`NON_RETRYABLE_FAILURE_REASONS` reason, a 64-hex `fp` and a numeric `run`, is
at least `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES` (60) old, and
has no trusted `ai:workflow-heal-non-retryable:v1` diagnosis for the same
pr/head/reason and no trusted
`<!-- ai:workflow-heal-non-retryable-backfill:v1 pr= head= reason= run= -->`
for the same run; provider-outage markers, other heads and cap markers are
skipped, and an editor summary first means `superseded`. Each candidate (at
most `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK`, default 3) goes
through the unchanged `workflow_failure_heal_autofix_report.sh` with the
marker's run as `GITHUB_RUN_ID` and a PR snapshot read just before and
checked against the selected head (`reason=head_moved` / `head_unreadable` /
`pr_read_failed` otherwise) as `PR_PAYLOAD_FILE`, so the intake's provenance gate checks that
run and its non-retryable routing posts the diagnosis; no heal issue is filed.
After a confirmed dispatch the backfill marker is posted. No OIDC report
identity is attached, so an intake with `WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=true`
skips these reports. Kill switch `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED`
(default `true`; `false` off, any other value off with a warning); every path
exits 0.

A freshness-update head (only GitHub's merge of an approved head with its base, #7084) skips the reviewers and runs `deterministic-skip-merge`, which waits for CI and merges. When that CI fails, the job runs `scripts/review_freshness_ci_failed.sh` (Q103). It posts a PR comment ending in `<!-- ai:freshness-ci-failed:v1 pr=<n> head=<sha> -->` and dispatches the PR's review workflow (`ai-review.yml` / `internal-review.yml` first, then the caller from `github.workflow_ref`, then `review_autofix.yml`). `scripts/review_head_gate.sh freshness-sync` reviews a head carrying that marker by the workflow's login normally (`reason=ci_failed_on_head`), so autofix sees the failing checks. The gate's existing marker-comments read (cached per gate run) also loads this marker; the doc-only / small-diff skip check reads it only when no earlier gate check did, so at most one `user` read plus one paginated comments read is added, and only for a skip candidate. The marker POST is tried up to 3 times (`FRESHNESS_CI_FAILED_RETRY_SLEEP_SECS` apart, default 5). If the comment cannot be posted, nothing is dispatched, because the redispatched run would take the fast path again. `FRESHNESS_CI_FAILED_REDISPATCH_ENABLED=false` turns it off. Before this, PR #7153 kept red CI from 2026-10-11 with nothing to re-run review, and its orchestrator project #6664 waited on it.

Judge context checkout keeps the target's tracked files (Q113). `prepare_tracking_judge_checkout` in `scripts/orchestrate_poll_process.sh` falls back to a forced checkout when the plain one fails. The poll workflow's "Stage workflow support files" step installs every support script `0755`, so a tracked `0644` script that differs on the target (`scripts/pr_checks_lib.sh`) makes that happen on every tick in this repo. The fallback used to copy the poller's backed-up `scripts/`, `prompts/` and `.github/ai/` over the whole checkout, so the wave judge reviewed `main`'s code beside the integration branch's tests and fragments. Project #7126's judge filed restore issues #7296, #7306 and #7307 for code the branch already had. After restoring the backup, the fallback now checks the target's tracked files under those three directories back out of `HEAD` and removes restored paths the previous checkout tracked and the target does not (skipping any path under a symlinked directory, and any path the verified support checkout also has, because later poller steps run those staged files). Only support files the target does not track stay restored, which is the same state the plain checkout path leaves. The security pass passes a fifth argument `true` and keeps the poller's trusted support files, because it executes `scripts/security_audit.sh` and its helpers from that checkout with the poller's credentials. In that mode, after a plain or a forced checkout alike (`_judge_refresh_trusted_support`), it re-copies every `scripts/` and `prompts/` file in the checkout that also exists in the verified `.codex-workflow-src` checkout (fallback `.codex-workflow-src-main`; a root the checked-out ref tracks is not used) from there, because an earlier judge checkout in the same tick may have left another project's tracked scripts in the tree. It fails closed (the security pass reports `engine_unavailable`) when such a path cannot be replaced: a failed copy, a symlinked parent directory, or a symlink where the trusted checkout has a directory. It also fails closed when no usable support checkout exists (`reason=no_trusted_root`), and after a plain checkout it copies back trusted `scripts/` and `prompts/` files that checkout deleted because the target does not track them.

Heal-scope recovery scan (#7333, activation gaps of PR #7325 for #7082):
`workflow-failure-heal-intake.yml` also runs on `schedule` (`41 * * * *`).
Its `recovery-candidates` job (no shared lock, `issues: read`) runs
`scripts/workflow_failure_heal_recovery_scan.sh --list-only`, which lists, in
up to four GraphQL pages of 50, open `ai:workflow-heal` issues in this
repository written by the token's own `/user` login whose body has no
`ai:workflow-heal-scope` marker, dropping those whose last 30 comments already
hold `HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` scan markers. Only when that count
is above zero, and no other run of this workflow is active (one runs read;
`reason=intake_busy` / `reason=runs_unavailable` write `count=0`), does the
`recovery-scan` job run, holding the job-level `workflow-failure-heal-intake`
concurrency group. A newly queued member of a group replaces its pending one,
so the busy check keeps a scan from displacing a waiting report (a report
queued in the seconds between the check and the job start can still be
displaced). Each candidate's body and `lastEditedAt` are re-read (GraphQL)
right before it is rebuilt, and its source PR is read only while the issue is
unedited. For at most `HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK`
issues (default 1, clamped 1-2) the scan rebuilds the `autofix_failure`
report from the issue's never-edited body (`workflow_failure_heal.py
heal-scope-recovery reconstruct`: pipeline author, `lastEditedAt` null,
composed `fp` / `source` markers, title, Head SHA, Failed run lines and the
evidence block; the live PR with the body's head, and the trusted failure
markers) and runs the unchanged intake with `WORKFLOW_HEAL_REPORT_ORIGIN=recovery_scan`
plus `WORKFLOW_HEAL_RECOVERY_SCAN_EXPECTED_FP` / `_ISSUE`. The intake accepts
that origin only on a `schedule` event with recovery on, both expected values
well formed and an `autofix_failure` payload, else
`report_auth=rejected reason=recovery_scan_invalid`. It sets
`REPORT_AUTH=reconstructed`, which only `_heal_scope_recovery_evaluate`
accepts; binding, provenance and the `heal-scope verify` creation guard are
unchanged. The recomputed fingerprint must equal the issue's `fp`
(`heal_scope_recovery outcome=skip reason=fingerprint_mismatch`) and the
budget must be a duplicate of that issue (`reason=decision_mismatch`);
anything but a recovery exits with `reason=not_recovered`. A replay posts
nothing on the source PR (`source_comment=skipped_reconstructed`), records
no occurrence (`occurrence=skipped_reconstructed`) and skips the
non-retryable PR handling (`non_retryable outcome=skip reason=reconstructed`).
A failed attempt, or a permanent reconstruct skip (`HEAL_SCOPE_RECOVERY_SCAN_PERMANENT_REASONS`),
leaves `<!-- ai:heal-scope-recovery-scan:v1 outcome=failed reason=<r> run=<id> -->`
(trusted only from the pipeline login, on the comment's last line); after
`HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` (default 3) the issue is skipped
(`outcome=attempts_exhausted`) and one WARNING was sent. A permanent skip
runs no intake, so it does not use the run's per-tick slot. Other heal kinds log
`reason=kind_unsupported` and still need a fresh verified report.
The scan records the intake's specific skip reason, not its final
`reason=not_recovered` line. `budget_budget_exhausted`, `budget_failed`,
`comments_unavailable`, `identity_unavailable`, `issue_record_missing` and the
intake's `error heal_issue_list_failed` read failure clear on their own: they log
`outcome=deferred`, post no attempt marker and do not count as intake failures; up to
two deferrals per run give their per-tick slot back, so one deferring issue
cannot starve later candidates. An open
issue that already carries a trusted recovery marker (a failed close after
`outcome=replaced`) is closed by the scan (`reason=already_replaced close=done|failed`;
a failed close leaves an attempt marker with `reason=close_failed`, so the
attempt cap bounds the retries and only the last attempt sends a WARNING,
which says to close it by hand; when the marker cannot be posted either, the
run sends a WARNING because no attempt was counted).
`HEAL_SCOPE_RECOVERY_ENABLED=false` turns the scan off with recovery.

Non-retryable reports and recovery (#7333 gap 2): a verified (OIDC)
`autofix_failure` report routed by `non-retryable-route` with
`conflict_resolver_sandbox_path_unsupported` or
`conflict_resolver_sandbox_support_missing`, while recovery is on, is no longer
stopped at the non-retryable block: it logs
`non_retryable outcome=deferred reason=heal_scope_recovery` and installs an
EXIT trap, so the unchanged PR-side handling (`_non_retryable_route_apply`:
diagnosis comment, `ai:needs-human`, WARNING, pr/head/reason dedup) runs after
recovery on every exit path and keeps the exit code. When no eligible
markerless heal issue matches, the intake exits right after
`_heal_scope_recovery_evaluate` (`heal_scope_recovery outcome=skip
reason=not_eligible_non_retryable`) with today's PR-side outcome and no
occurrence or heal issue. A deferred report whose heal-issue list cannot be
read logs `heal_scope_recovery outcome=skip reason=heal_issue_list_failed` and
exits 0 with the same PR-side outcome; the hourly scan retries recovery. The host-only stop and unauthenticated reports are
never deferred.

Heal lineage across unblock-judge re-issues (Q147, Q149, from #7082): the
unblock judge files the re-issue of a blocked heal issue as an ordinary issue
without heal markers, so the intake's `ai:workflow-heal` list stopped seeing
the lineage once the original closed, and a later report for the same failure
opened a new generation next to the open re-issue (#7082 next to #7059).
With `HEAL_REISSUE_DEDUPE_ENABLED` on (default), when the budget finds a
closed heal issue in the report's lineage (same fingerprint, same `source=`,
or the linked heal issue) and no open duplicate, `_heal_reissue_lookup` runs
one `search/issues` read per repository that can own the heal issue (self,
plus the source repository when it differs) and re-runs `budget` with
`--reissues-json`. `workflow_failure_heal.py reissue_origin` reads the
`Re-issued by the unblock judge from #N.` line only as the body's first line
that is not an HTML comment; `resolve_reissue_roots` follows a re-issue of a
re-issue back up to five steps in the same repository and requires every issue
on the chain to be written by the heal issue's author. An open re-issue of a
closed lineage issue becomes the duplicate (`match: reissue`, `reissue_of`,
the heal issue's `gen` / `root`), so the report is an occurrence comment on
the re-issue; heal-scope recovery skips that match. A reconstructed recovery
replay never runs the lookup, and a failed search keeps the first decision.
Log: `WORKFLOW_HEAL reissue_lookup outcome=found|none|skip reason=search_failed|budget_failed|invalid_switch`.

The recovery scan's `reconstruct` (Q149) reads the failed run from the issue
body's `Failed run` lines and, when the body has none (a heal issue composed
without run summaries, such as #7082), from the newest occurrence comment
posted by the pipeline login whose first line is the occurrence marker and
whose `Source pull request` and `Head SHA` lines match the body. The intake
still verifies that run's provenance and the fingerprint before anything is
replaced; `runs_missing` stays a permanent skip when neither has a run.

**Daily consumer health check.** In `shubhodeep1/coding-workflows` only, the `poll` job of `.github/workflows/orchestrate_poll.yml` runs `scripts/consumer_health_check.sh` once per UTC day: the step `Look up today's consumer health check` checks for an Actions cache entry `consumer-health-check-<YYYY-MM-DD>`, and `Record today's consumer health check` saves it only after a successful check, so later ticks that day skip without an API call and a failed or missed tick is retried on the next one. For each repository in `.github/ai/consumer_repos.json` the script reads the workflow list, the newest `ai-orchestrate-poll.yml` run (only when that wrapper is active) and the open `ai:orchestrator-tracking` issues, and `scripts/consumer_health_check.py evaluate` decides. A wrapper (a `*.yml` under `workflow-templates/`) in state `disabled_inactivity` is switched back on; one disabled by a person or token, or in any other non-active state, is reported (`workflows_disabled`), as are an active poller with no run in `CONSUMER_HEALTH_POLLER_SILENT_HOURS` (default 6, `poller_silent`), an open tracking issue with `CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT` or more comments (default 2000, `tracking_issue_comments`) and a failed read (`unreadable`). The workflow and tracking-issue listings are paginated, and a failed issue create or close makes the run exit 1 so the next tick retries the day; a failed enable is logged and retried by the next day's check, and its WARNING is sent with the day-recording run or, when issue writes keep failing, in one WARNING per UTC day (the script writes `failure_alerted=true` and `Record today's consumer health failure alert` saves `consumer-health-alert-<YYYY-MM-DD>`, which the next ticks pass back as `CONSUMER_HEALTH_FAILURE_ALERTED`). The silence window is measured from the newest poll run's `run_started_at` (falling back to `created_at`), and an issue is closed before its closing comment is posted so a retry never duplicates the comment. Each consumer and problem gets one `ai:consumer-health` issue in this repository, whose body starts with `<!-- ai:consumer-health:v1 repo=<owner/repo> problem=<key> -->`, and one Telegram WARNING when it opens. Only issues the pipeline account filed with that first line count: an open one suppresses a duplicate, and one whose problem is gone (or whose repository left the consumer list) is closed with a comment. A repository that could not be read keeps its other issues open. `CONSUMER_HEALTH_CHECK_ENABLED=false` turns the check off.

Heal intake reports with no runs (#7424): a report that claimed no run at all
(a label-only report such as `ai:scope-blocked`, or `engine_fallback`) has no
verified run to bound a heal scope with. Both the pre-compose scope-marker
guard (`WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED`) and the creation gate
(`HEAL_INTAKE_SCOPE_GUARD_ENABLED`) skip it with
`WORKFLOW_HEAL skip reason=scope_unavailable_no_runs stage=marker_guard|scope_guard`
and exit 0, with no CRITICAL alert (`_heal_report_claimed_no_runs` in
`scripts/workflow_failure_heal_intake.sh`). The raw report must name no run as
well: a report whose claimed runs `validate-payload` dropped (malformed or
another repository) keeps the CRITICAL refusal, and so does a heal-scope
recovery replacement (`HEAL_SCOPE_RECOVERY_ACTIVE=true`).

**Consumer health: tracking issues near the comment cap (#7427).** A `tracking_issue_comments` consumer-health issue does not call for a change in this repository. The poller's state-comment prune (`prune_state_comment_chains` in `scripts/orchestrate_poll_process.sh`, `ORCH_STATE_PRUNE_ENABLED`, at most `ORCH_STATE_PRUNE_MAX_DELETES` deletes per tracking issue per run) reaches a consumer only after that consumer's installed wrapper pin includes it, through the `stable` release and the consumer's `auto/update-workflows-*` pull request. The alert stays open while that update pull request is open or unmerged. Pruning deletes only superseded `ORCHESTRATOR_STATE_V2` / V1 state chains. A tracking issue whose other comments still reach `CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT` (default 2000) keeps its health issue open, and that open issue is meant for a person to act on. The threshold and the way comments are counted stay unchanged, because GitHub's 2500-comment cap counts every comment. The health issue closes itself on the first daily check that finds no tracking issue at or above the limit. This was first seen in #7427 (tele-funtoken-msg-scoring tracking issues #3928 at 2500 comments and #4038 at 2040 comments, 2026-10-15).

Heal-issue plan approval and pipeline-only body edits (#7444, unblock fix-up for #7434): with `HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED` on (unset, empty or `true`; `false` turns it off, any other value turns it off with a warning), plan.yml's "Auto-approve clear plan" never runs the Q108 pipeline-grant PATCH for an `ai:workflow-heal` issue. It reads the live body with one GraphQL call, runs `workflow_failure_heal.py heal-scope verify`, and passes the verified paths to `files_touched_scope_guard.py --plan-auto-approval-gate --heal-scope-file` (`run_heal_plan_scope_gate`: strict `path_in_scope`, no lockfile allowance, directory and glob entries denied, `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED=false` still holds). Approval reason `heal_scope_verified`; holds `heal_scope_unverified`, `heal_scope_plan_out_of_scope`, `heal_scope_plan_paths_missing`, `heal_scope_check_unavailable`; log `PIPELINE_PLAN_GRANT ... outcome=skipped reason=heal_issue`. In `scripts/implement_heal_preflight.sh`, a `status=edited` result triggers one more GraphQL read of the body and its `userContentEdits(first:100)`; `heal-scope verify-edits` (`verify_heal_scope_pipeline_edits`) accepts it only when the history is complete, no revision was deleted, every editor is the `GH_PAT` login and every revision's `diff` (GitHub returns the revision's full text) holds the identical marker exactly once. Refusals keep `reason=edited` with `detail=edit_history_unavailable|edit_history_redacted|editor_untrusted|marker_changed|unknown`. The poller's `_plan_auto_approval_gate` still writes Q108 grants into heal issues; the preflight accepts those pipeline-only edits, and so does `scripts/workflow_failure_heal_evidence.sh` (same `verify-edits` read, same switch), so such an issue still gets its failure evidence. The heal gate also holds with `workflow_edits_disabled` when the plan lists an automation path and `ALLOW_WORKFLOW_EDITS` is off, and checks nested Files-section tokens that are not exact file paths (an extensionless `docs/guide`) against the scope instead of dropping them.

Implement and a CI-triage PR that is no longer open (#7540): implement's "Resolve integration ref" step now handles the resolver's exit-3 `reason=pr_not_open` refusal as plan (#7477) and clarify do. It logs the refusal as a `::notice::`, sets `SKIP_IMPLEMENT=true`, writes no `ref` and exits 0, so the default branch is never checked out. "Hand off CI-triage issue whose PR is no longer open" then runs `plan_superseded_merged_pr.py classify` from the verified support copy only (`.codex-workflow-src`, ref from `PR_NOT_OPEN_SCRIPT_REF`, `python3 -I -B`). A merged PR whose check passed on its final head closes the issue as not planned with the `ai:plan-superseded:v1` comment and `ai:closed`. Every other outcome, and `PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED=false`, a missing helper or a failed classifier, moves the issue from `ai:awaiting-approval` to `ai:blocked` with a `<!-- ai:implement-pr-not-open:v1 pr=<n> run=<id> -->` comment for the unblock judge. Log: `AI_PHASE_GATE_V1 phase=implement gate=ci_triage_pr_not_open`. Every other exit-3 reason still fails closed. "Diagnose post-Codex failure and create fix-up issues" skips with a warning when its helper was never staged.

## Plan Decisions advisory lint

- `scripts/lint_plan_decisions.py` is the fail-open linter for the plan
  `## Decisions` convention under `docs/plans/*.md`. It always exits `0`,
  emits advisories to stderr, and treats missing or malformed decision records
  as warnings rather than merge blockers.
- `.github/workflows/ci.yml` runs the linter in the
  `Plan decisions advisory lint` step with `continue-on-error: true`.
- `DOCS_DECISION_LINT_ENABLED` (default `false`) only controls whether CI
  replays captured advisories into the job log and `GITHUB_STEP_SUMMARY`; it
  does not disable the underlying linter run.
- The documented plan schema is `## Decisions` containing one or more
  `### D<n> — <title>` records with `Chosen`, `Alternatives considered`, and
  `Why` bullets.

## Stable-ID convention

- New AI-pipeline identifiers must be created through the canonical helpers
  in `scripts/ai_memory_lib.py`; use `make_record_id(prefix)` unless this
  section documents a purpose-built alongside format. Do not hand-roll new
  ID formats in caller modules. The Phase 5 plan's
  `scripts/ai_memory_lib.py:480` pointer is historical; follow the live
  `make_record_id(prefix)` definition in that file.
- The current format is contractual: `<prefix>_<YYYYMMDDHHMMSS>_<10hex>`.
  The timestamp is UTC and the suffix is the first 10 lowercase hex characters
  from a UUID4.
- `make_record_id(prefix)` sanitizes the caller-supplied prefix through
  `sanitize_segment(prefix, "mem")` before composing the ID. Today that
  preserves ASCII letters, digits, and `_.:-`, replaces other runs with `_`,
  trims leading/trailing `.` or `_`, preserves existing safe prefixes such as
  `mem` and `run_event`, and falls back to `mem` for empty or invalid-only
  inputs.
- Example shapes: `mem_20260709041614_1f025339dd` and
  `run_event_20260709041614_1f025339dd`.
- This format is a compatibility contract. If a future change needs a new
  stable-ID format, follow the §6 backward-compatibility rule: keep
  `make_record_id(prefix)` emitting the current format, introduce the new
  format via an alongside helper/alias so old IDs remain valid and existing
  outputs stay stable, and update the contract test in the same change.
- `make_deterministic_record_id(prefix, *identity_parts)` is the sanctioned
  alongside helper for records that must deduplicate across runs. It emits
  `<sanitized-prefix>-<24hex>` from the SHA-256 digest of the newline-joined
  identity parts; `make_record_id(prefix)` and its format remain unchanged.

## Override conventions

Two override files let maintainers pin specific catalog fields
or prompt files as untouchable by future regenerators:

- `scripts/codex_model_catalog_overrides.yaml` — per-model-slug
  field overrides for `scripts/codex_model_catalog.json`. The
  fields listed under each slug's `overrides:` block are merged
  over the catalog defaults at render time by
  `scripts/generate_codex_model_reference.py` and emitted with a
  `(frozen)` marker in the generated `docs/codex-model-reference.md`.

- `prompts/.overrides.yaml` — per-file freeze markers for any
  prompt file under `prompts/`. Files listed with
  `skip_validation: true` are excluded from any future automated
  prompt-rewriter tooling.

§6 implication: overrides are the canonical way to preserve
intentional non-default values across future regenerations
without renaming or removing the underlying identifier. To freeze
a new value, add it to the appropriate overrides file with a
`notes:` / `reason:` block explaining why.

## Utility helpers

- `scripts/repo_root.py` provides `repo_root()` and
  `repo_root_from(start: Path)` for scripts and tests that need to
  resolve the repository root by walking upward until they find both
  `CLAUDE.md` and `.git/`.
- `scripts/generate_resource_id.py` provides
  `generate_id(prefix, salt=None)` as a wrapper around the live
  `make_record_id(prefix)` helper in `scripts/ai_memory_lib.py`.
  Unsalted calls delegate to `make_record_id(prefix)` unchanged;
  salted calls preserve the same `<prefix>_<UTC timestamp>_<10 hex>`
  shape while deriving the suffix from
  `sha256(salt.encode("utf-8"))`, including the empty-string salt.

## Implement scope-lock label

Implement's automation-path grant guard runs on the existing `/approved` entry
point before and during commit. Staged `.github/`, `.claude/`, `scripts/`,
`prompts/`, and `workflow-templates/` files (case-insensitive, including rename
sources) require `ALLOW_WORKFLOW_EDITS=true` plus an exact `files_touched` entry
from an issue authored by the GH_PAT login or OWNER/MEMBER/COLLABORATOR.
An issue authored by the GH_PAT login with no `files_touched` block at all gets an
open grant for every automation path (`reason=pipeline_author_no_allowlist`); a
declared block grants only its exact entries, and a malformed block blocks for every author.
The host-only issue-bound grant file is built before the isolated editor runs;
missing metadata, identity or helper fails closed only for automation paths.
Both sites reuse `scope_violation_*` outputs and `ai:scope-blocked`; the
`guard=automation-path` rejection is not eligible for unblock overrides.
General `ENFORCE_FILES_TOUCHED` and `ALLOW_OUT_OF_SCOPE_FILES` switches cannot
bypass it. Log prefix: `IMPLEMENT_AUTOMATION_PATH_GUARD`.

- When `SCOPE_LOCK_LABEL_ENABLED=true`, `implement.yml` recognizes one active
  dynamic issue label of the form `ai:scope:<glob>` and copies the glob into
  the implementation context.
- The glob follows Bash `globstar` semantics (for example `scripts/**/*.py` or
  `prompts/mode-*.txt`) and is enforced after the local AI commit is created
  but before push: `scripts/implement_commit_changes.sh` reuses
  `scripts/files_touched_scope_guard.py` against the committed pathset and
  rolls the unpushed local commit back on any out-of-glob path.
- Multiple `ai:scope:` labels are not merged; the workflow enforces only the
  first matching label from the issue metadata and logs a warning.
- This is a runtime dynamic-label exception to the otherwise static
  `.github/ai/label_contract.v1.json` contract. The glob-bearing
  `ai:scope:<glob>` labels are intentionally not enumerated there and are not
  part of contract-driven label repair.
- The contract's `retired_labels` array lists labels that no longer exist
  upstream (the former `ai:claude*` session labels and `ai:permission-prompt`).
  `scripts/ai_labels.py sync-labels` deletes each one from a repo when present
  (log prefix `LABEL_SYNC_DELETED`; `LABEL_SYNC_RETIRED_ABSENT` when already
  gone; the output gains a `deleted` list). Nothing creates or applies them.

`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` (default `true`, #6838) moves
the same grant check in front of the automatic `/approved`. When on,
`plan.yml`'s "Auto-approve clear plan" step and both poller stall-recovery
`auto_approve` branches extract the automation paths from the plan's
"Files ... change" section. They then run the unchanged
`files_touched_scope_guard.py --emit-automation-grant` /
`--check-automation-paths` from the verified support checkout. If any listed
path is ungranted, or the check cannot run, the issue stays in
`ai:awaiting-approval` with one `<!-- ai:automation-path-plan-hold:v1 ... -->`
comment and a WARNING. The poller logs `STALL_SKIP ...
reason=automation_path_ungranted|automation_path_check_unavailable` without
counting a recovery attempt. `orchestrate_poll.yml` forwards the flag to the
poller with a `true` default, so it checks even if the plan-step
hold marker fails to post; explicitly disabling it ignores old markers.
Direct script callers with an unset flag get the same `true` default
(`scripts/orchestrate_poll_process.sh`). The extractor
block is byte-identical in both files, and
`tests/test_plan_auto_approve_safe_state_fetch.py` pins that.

Automation-path grant, source chain (Q82): `build_automation_grant` in `scripts/files_touched_scope_guard.py` walks the source issues a pipeline-authored body names before granting anything. The references are `Re-issued from #N`, `Re-issued by the unblock judge from #N`, `<!-- ai:unblock-fixup:v1 item=N`, `Accepted with this follow-up by the unblock judge (#N)`, `- Source issue: #N` (implement-diagnose fix-up), `- Replaces: #N` (review-blocked re-issue footer) and `- Parent issue: #N` (review-blocked follow-up), all collected, so copied text can only add checks. Whether a `review-blocked-reissue` / `review-blocked-followup` names its source is decided only inside the `---`-delimited block holding the last `- Type:` line of that kind. Each source is read with `fetch_issue_with_gh` (`gh api repos/<repo>/issues/<n>`, two attempts) when the caller passes `--origin-repo`. `implement.yml` (Fetch issue metadata) passes `${GITHUB_REPOSITORY}` and `unblock_try_regrant` passes `${REPOSITORY}`. A trusted person's source ends its branch, and a pipeline-authored source is walked in turn, bounded by `ORIGIN_MAX_DEPTH=4` and `ORIGIN_MAX_ISSUES=8`. Any untrusted source sets `reason=untrusted_origin`. A failed read, an exceeded bound, a missing `--origin-repo`, or a review-blocked re-issue or follow-up footer without an issue number sets `reason=origin_unverified`. Both deny every automation path, open and exact grants alike, and the grant record lists `origin_issues`. `IMPLEMENT_AUTOMATION_PATH_GUARD mode=grant` logs `origin_issues=<n,...|none>`. A person's own issue is never walked. Tests: `tests/test_files_touched_scope_guard.py`.

Plan auto-approval gate (#6769, replacing the #6838 gate): with
`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` off (`false`; the default is on), no automatic
`/approved` is posted for a plan that lists automation paths. This covers
`plan.yml`'s "Auto-approve clear plan" step, both stall-recovery `auto_approve`
arms, and both `retrigger_implement` arms while the issue is in
`ai:awaiting-approval`. With the flag on, every listed protected path needs an
exact trusted `files_touched` grant; the open pipeline-author grant does not
count. `files_touched_scope_guard.py --plan-auto-approval-gate` decides (exit 0
approves, 30 holds, every error holds). Plan paths are taken whole up to
whitespace, a backtick or a quote, so an unusual filename is never cut to a
grantable prefix. Only a heading at the Files lead-in's level or shallower
ends the section, and negated wording hides no path (fail closed). The poller reads only the newest
`<!-- ai:plan-proposal:v1 -->` comment by the `GH_PAT` login (none found holds
with `plan_comment_unavailable`), and the batched issue GraphQL now carries
`author_login` / `author_association`. A `plan.yml` hold posts one deduplicated
`<!-- ai:automation-path-plan-hold:v1 ... -->` comment and a WARNING; a
poller hold logs `AI_PHASE_GATE_V1 phase=stall_recovery ... outcome=defer` and
`STALL_SKIP ... reason=automation_path_plan_hold`, posts the same comment with
`source=poller` once per reason and path list (a re-plan listing different
paths gets a new comment), and does not count a recovery attempt. Both gate
callers pass `--origin-repo`, so a pipeline-authored issue that names a source
issue gets the same origin-chain check as at implement time. The implement-time guard and the staged-support latch release are
unchanged.

Idle-tick standalone recovery (#7174): on cron ticks with no open
`ai:orchestrator-tracking` issue, `orchestrate_poll.yml` runs "Run standalone
stall recovery without active projects" (`has_work != 'true'`, the complement
of "Process each tracking issue", every repository, `continue-on-error: true`
so a failure does not skip the latch-release and unblock-scan sweeps after
it). It carries the full poller step's env plus `STANDALONE_RECOVERY_SWEEP_ONLY=true`
(default `false` in the script), which makes `scripts/orchestrate_poll_process.sh`
prime the phase concurrency caps, run only `run_standalone_stall_recovery` and exit, so a standalone plan held
for automation paths is re-checked after its body gains a grant (the
plan-step and poller grant checks already pass `--origin-repo`). The step
skips with a warning when the staged poller lacks the mode. "Resolve AI
engine" now runs on every tick and the Claude CLI / pool / pool-alert steps
gate on `any_claude` alone, so the idle-tick stall judge resolves its engine
as on a busy tick. The OpenCode install also runs on idle ticks unless
`ENABLE_STANDALONE_STALL_RECOVERY` or `ENABLE_STALL_JUDGE` is `false`. `ENABLE_STANDALONE_STALL_RECOVERY=false` turns the
idle-tick recovery off.

`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` defaults to `true` everywhere it
is read; `files_touched_scope_guard.py` treats an unset or empty
`--flag-enabled` as on and any value outside `1|true|yes|on` as off. The
planner prompts ask for the file list under `## Files likely to change` with
the next item under its own `##` heading, which every plan parser
(`extract_plan_automation_paths`, `plan.yml`'s template-owned path guard and
`targeted_file_context.extract_paths_from_plan`) treats as the end of the list.

Pipeline plan grant (Q108). The open grant a pipeline-authored issue without `files_touched` gets never auto-approves a plan. When it is the only reason the gate holds, `files_touched_scope_guard.py --plan-auto-approval-gate --pipeline-grant-body-out PATH` proposes the issue body with one appended block: a `---` rule, a short note, `<!-- ai:pipeline-plan-grant:v1 -->` and `files_touched:` listing every exact file in the plan's Files section (`extract_plan_file_paths`) plus `tests/**`, `changelog.d/*` and `agents.d/*`. It re-checks that body (the Q82 source-issue check included, with each source issue read once) and returns `decision=approve reason=pipeline_plan_grant pipeline_grant_body=true` only when it would grant every protected path. A directory, a glob or more than 50 files gets no proposal; a nested path whose last segment has no file extension (other than a known name such as `LICENSE` or `Makefile`), or one that starts with a digit (`v1.0`), counts as a directory, because a `files_touched` entry without a trailing slash covers everything beneath it. A root-level name counts as a file when it has a letter-led extension (`main.c`, `app.php`, `.gitlab-ci.yml`) or is a listed extensionless name or dotfile (`Pipfile`, `.dockerignore`); `.env*` and other extensionless dot names (`.vscode`) are never written. `plan.yml`'s "Auto-approve clear plan" step PATCHes that body onto the issue before it posts `/approved`. The poller's `_plan_auto_approval_gate` first re-reads the issue body over REST and skips the write when the body differs from the tick's snapshot. Either failure holds (`pipeline_plan_grant_write_failed`, `pipeline_plan_grant_body_changed`). The written block is then the issue's ordinary exact grant and its implement-time scope. `AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED=false` turns this off with the rest of the gate.

`files_touched:` lists flush with their key (Q111). YAML lets a bare `files_touched:` key keep its `- path` list at the key's own indent, and the orchestrator judge wrote #7296 that way, which every reader rejected as `malformed_allowlist`. `extract_files_touched` in `scripts/files_touched_scope_guard.py` and its verbatim copy in `scripts/review_reject_verify.sh` (a test keeps the two identical) now accept that form. The first entry fixes the indent: a flush list continues only with flush entries, and an indented list stops at a flush line. The list-item form `- files_touched:` still needs indented entries, because a flush `- x` there is a sibling item. `unblock_judge.sh`'s `edit_files_touched` adds new paths at the list's own indent. `prompts/mode-judge.txt` and its template now ask for the two-space form and exact files for automation paths.

Plan split to a project (Q151/Q152/Q164). The pipeline plan grant covers at most 50 files. When a pipeline-authored issue's plan lists more, `files_touched_scope_guard.py --plan-auto-approval-gate` holds with `reason=plan_too_large_for_grant` and `plan_path_count`, unless the implement-time automation-path check would refuse the plan anyway (for example `ALLOW_WORKFLOW_EDITS=false`, which keeps `reason=workflow_edits_disabled`). The plan stage then runs `scripts/plan_split_dispatch.sh` instead of holding:

- It starts `internal-orchestrate.yml` (this repo) or `ai-orchestrate.yml` (consumers) with `engine=claude`. The `project_description` carries the issue title, the full plan text, and instructions to keep the plan's phases, give every issue at most 50 `files_touched` entries, and put the phase that switches a behaviour on last. The `tracking_comment` starts with `<!-- ai:plan-split-source:v1 issue=N -->`.
- Before the dispatch, the source issue moves from `ai:awaiting-approval` to `ai:split-to-project` and then gets `<!-- ai:plan-split:v1 issue=N wrapper=... -->` from the pipeline account, so the orchestrator run always sees the label. A failed label change or marker comment holds the plan without a dispatch. The step outputs `plan_split=true`, so no "plan awaiting approval" alert is sent. The dispatch is never retried, because a dispatch that timed out may already have started a project. After posting its marker the script re-reads the comments; when an earlier split marker since the last release marker is not its own (two plan runs raced), it deletes its marker, logs `outcome=deduplicated` and exits 0 without dispatching. When its own marker cannot be posted but a split marker since the last release marker exists (another run holds the split), it releases nothing and exits 0 (`reason=concurrent_split_marker_failed`); a marker carrying this run's own `RUN_URL` (the POST landed but its response was lost) does not count, so that run releases and holds (`reason=marker_failed`). A dispatch rejected with an HTTP 4xx status started nothing: the script posts a release marker (up to 3 attempts), then removes the label (restoring `ai:awaiting-approval`), and holds; if the release marker cannot be posted the label stays on and it sends a WARNING naming the recovery (delete the plan-split comment, remove the label, then `/reclarify`). Any other dispatch failure may have started a project: the label stays, a release marker with instructions is posted, a WARNING is sent (`outcome=dispatch_unconfirmed`), and a person removes the label and comments `/reclarify` if no tracking issue appears.
- `orchestrate.yml` comments the tracking-issue link on an open source issue that carries `ai:split-to-project`. If its run fails before a tracking issue exists, the "Release plan-split source issue" step posts `<!-- ai:plan-split-released:v1 issue=N -->` (up to 3 attempts) and only then removes the label, so a removed label always has a release marker after the split marker; its warning says whether the marker or only the label removal failed. A split marker followed by a release marker no longer counts as a split, so a `/reclarify` can hand the plan to a new project.
- When the project's status is `complete`, the poller (`close_plan_split_source_if_needed`) closes the source issue as completed. It acts only on a marker comment from a trusted author (OWNER/MEMBER/COLLABORATOR or `github-actions[bot]`), and only if the issue is open and carries the label. The outcome is kept in the state file as `.plan_split_source`, so this costs one issue read once. A failed project leaves the source issue open. `ai:split-to-project` is a phase label in `PHASE_LABELS_PRIORITY` and `DEDICATED_HANDLER_PHASES` (`scripts/orchestrate_lib.py`) and in the standalone stall loop's skip list, so stall recovery never posts `/reclarify` on a source issue while its project runs.
- The plan is held as before (`exit 3`) when `PLAN_SPLIT_TO_PROJECT_ENABLED=false`, the issue is a project child (`ai:orchestrator-managed` or `Managed by: AI Orchestrator`), it is already split (label or marker), the wrapper is missing or switched off, the description is over 60,000 characters, the source issue cannot be marked, the consumer wrapper predates the `tracking_comment` input, or the dispatch is rejected. Plans held before this change are not split; `/reclarify` re-plans them.


## Implement self-repo staged-support ledger

- `implement.yml` stages the runtime helpers *in-tree* (`install -m … "scripts/${f}"`,
  `prompts/*`, `ai-memory/schemas/*`) from `SCRIPT_REF`, which in this repository is
  `github.sha` (the default branch) while the checkout may be an orchestrator
  integration branch. Those installs overwrite tracked files, and the self-repo
  commit path in `scripts/implement_commit_changes.sh` has no `scripts/` exclusion.
- The staging step inventories every actual worktree install and records each path whose post-stage
  state differs from `HEAD` in
  `STAGED_SUPPORT_LEDGER` (`${RUNTIME_DIR}/staged_support_overwrites.txt`) with the
  installed content under `STAGED_SUPPORT_BASE_DIR`; it also records support
  paths recreated over branch-side deletions. Executable support-ref copies live
  under `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`. The ledger and base paths are
  exported through `GITHUB_ENV` and only exist when `github.repository` is this
  repository; `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` exists in every repository (see
  "Isolated Codex agents").
- `scripts/implement_commit_changes.sh` consumes the ledger before `git add`:
  restore-to-HEAD for untouched copies, 3-way `git merge-file` re-base for
  editor-edited copies, preserve editor-selected modes and branch/editor deletions, remove untouched
  staging recreations, and fail closed on incomplete inputs or merge conflicts.
  The workflow invokes the immutable runtime copy of the commit helper and uses
  runtime copies for all later helper calls, so restoring the worktree cannot
  replace the running script or downgrade post-commit tooling.
  The preflight scope guard resets only ledger paths still equal to their installed
  baseline in its temporary index; editor-modified or deleted paths remain checked.
  Log keys: `IMPLEMENT_STAGED_SUPPORT_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_LEDGER_MISSING`, `IMPLEMENT_STAGED_SUPPORT_RESTORED`,
  `IMPLEMENT_STAGED_SUPPORT_REBASED`, `IMPLEMENT_STAGED_SUPPORT_DELETED_BY_EDITOR`,
  `IMPLEMENT_STAGED_SUPPORT_RECREATED_BY_EDITOR`, `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING`,
  `IMPLEMENT_STAGED_SUPPORT_HEAD_READ_FAILED`, `IMPLEMENT_STAGED_SUPPORT_REBASE_CONFLICT`,
  `IMPLEMENT_STAGED_SUPPORT_REBASE_FAILED`, `IMPLEMENT_STAGED_SUPPORT_RESTORE`.
- Both codex editor launches in `implement.yml` (the "Run Codex implementation"
  attempt loop and the "Attempt post-Codex syntax repair" loop) run
  `bash scripts/codex_thread_reuse.sh direct-run` through
  `env -u STAGED_SUPPORT_LEDGER -u STAGED_SUPPORT_BASE_DIR -u STAGED_SUPPORT_EDITOR_HEAD_LEDGER -u IMPLEMENT_STAGED_SUPPORT_RUN_DIR`.
  The editor never reads those paths; only the restore / reinstall / commit
  steps of the job do. A `pytest` the editor starts to validate its own change
  therefore cannot write fixture paths into the live run's ledgers even when
  the checkout (a review-blocked `ai/reissue-baseline/*` branch, or an
  integration branch that has not synced `main`) carries a `tests/conftest.py`
  older than the session-wide strip. `tests/test_implement_post_codex_recovery.py`
  pins both launch lines and the pass-through of the `CODEX_THREAD_REUSE_*`
  prefix assignments.
- `scripts/implement_staged_support_workspace.sh` (staged into the run dir with the other
  helpers, self-repo only, no-op without a ledger) changes what the *editor* sees:
  `restore` runs right before the Codex implementation loop and before the post-Codex
  syntax-repair loop, puts every ledger path still equal to its installed copy back to
  `HEAD` (removing staging recreations of branch-deleted files) and lists them in
  `STAGED_SUPPORT_EDITOR_HEAD_LEDGER` (`${RUNTIME_DIR}/staged_support_editor_head.txt`);
  `reinstall` runs after each loop and puts the installed copy back for every listed path
  the editor left untouched. The commit helper commits a listed path the editor changed
  as a plain edit (`IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD`) and never re-bases it.
  Regression: #4113 (run 35072286584) edited `main`'s `scripts/codex_helpers.sh` on
  `orchestrator/project-3965`, the re-base conflicted, and the issue halted in
  `ai:needs-human`. Log keys: `IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORED`,
  `IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALLED`, `IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED`,
  `IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED_PATH`, `IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORE`,
  `IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALL`, `IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD`.
- Staged-support failures are consumed by the runtime-preserved rejection handler,
  which attempts and verifies the `ai:needs-human` latch, comments with the affected paths and
  latch status, sends the configured CRITICAL alert, and prevents generic diagnosis/re-issue handling.
  Genuine three-way rebase conflicts add
  `<!-- ai:needs-human-latch reason=staged_support_rebase_conflict -->`; missing ledgers,
  baselines, unsafe paths, and merge-tool failures remain human-gated without that marker.
  The source-repository poller's `release_staged_support_needs_human_latches` sweep (gated by
  `STAGED_SUPPORT_LATCH_AUTO_RELEASE_ENABLED`, default `true`) runs in sweep-only mode when no
  tracking project is active and matches that marker or the exact pre-marker #4113 incident
  from run `35072286584`, only on comments from an `OWNER`, `MEMBER`, `COLLABORATOR`, or installed
  `[bot]`, and once the running engine carries
  `scripts/implement_staged_support_workspace.sh` it restores `ai:awaiting-approval` and posts
  `/approved` with a `<!-- ai:needs-human-auto-release reason=staged_support_rebase_conflict
  engine=<sha> -->` marker, at most once per issue per engine commit (log keys
  `STAGED_SUPPORT_LATCH_RELEASED`, `STAGED_SUPPORT_LATCH_SKIP`,
  `STAGED_SUPPORT_LATCH_RELEASE_SKIPPED`). The latest `ai:needs-human` label event must strictly
  precede the staged-support comment and have the same actor; same-second timestamps fail closed,
  so clearing that latch and later setting another human gate cannot reuse the stale marker.
  Immediately before changing labels, the sweep re-reads the paginated live labels and latest
  latch event; a changed or unreadable latch, or a residual `ai:implementing` label, skips release
  for that tick. A failed `/approved` response triggers a paginated history check; a trusted
  release marker confirms an accepted write, while a confirmed failure restores
  `ai:needs-human`. If that compensation also fails, the unresolved latch marker blocks
  managed and standalone auto-approval and raises a CRITICAL alert. Those recovery guards use
  the batched comment cache only when it explicitly reports an available array with fewer than
  100 entries; a missing/partial field or a full window triggers a paginated history read, and
  unavailable or malformed history fails closed for that tick.
  Consumer repositories and other latch reasons stay human-cleared.
- The other in-tree staging workflows (`clarify.yml`, `plan.yml`,
  `orchestrate_clarify_respond.yml`, `orchestrate.yml`, `orchestrate_poll.yml`,
  `check_failure_triage.yml`) either never commit from that checkout or run on
  the same ref they stage from, so the ledger is wired into `implement.yml` only.
  `review_autofix.yml` and `validate.yml` stage out of tree via
  `scripts/stage_workflow_support.sh`.
- Incident: implement runs 34392788763 (PR #4071) and 34613019339 (PR #4079)
  committed `main`'s copies of eight helpers onto `orchestrator/project-3965`,
  reverting the branch's security-pass fixes. `tests/test_implement_post_codex_recovery.py`
  pins the ledger, immutable-execution, deleted-path, handler, and restore outcomes.

- Host Python helpers that run while `GH_TOKEN` holds `GH_PAT` must not import from the branch checkout's `scripts/` (issue #7159). Implement's security-dependency gates and bulk-delete override check run `security_dependency.py` / `unblock_ledger.py` from `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` with `python3 -I -B`, and fail closed (`reason=trusted_helper_unavailable`) when that copy is missing or a symlink. Before the run dir exists, implement's WORKFLOW.md overlay loader runs a support-ref copy under `${RUNTIME_DIR}/host_python`; clarify stages its overlay and security-dependency helpers from the support ref into `${CLARIFY_HOST_PYTHON_DIR}` under runner temp. Both invoke `python3 -I -B`; a missing clarify dependency helper returns a held verdict. `tests/test_security_dependency.py` and `tests/test_review_host_python_safe_path.py` pin this.

---

## Review self-repo support staging runs under main's workflow YAML

- `internal-review.yml` calls the reusable
  `shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@main`
  (a `uses:` ref cannot vary per PR), while `review_autofix.yml`'s "Resolve
  workflow support ref" step consumes the SHA selected by the gate from
  `toJSON(job)` identity (direct `job.workflow_*` expressions are not supported
  by the pinned actionlint version). A same-repo PR reports its branch and PR
  head in that identity; the gate therefore reads the protected `main` branch's
  SHA independently via the GitHub API and fails closed if protection or SHA
  cannot be confirmed. Consumer `@stable` calls resolve the release tag, and
  consumer SHA pins use their literal pinned value. Every job checks out the
  selected immutable SHA and compares HEAD before running support. Self-repo
  PR-head `scripts/*`, `prompts/*`, and `ai-memory/schemas/*` are reviewed as
  data; the executable runtime bundle comes only from the verified workflow
  commit. Consumer release pins use the same SHA-bound checkout. The
  failure-path reporter skips when support staging did not complete or its
  optional Python helper is absent; neither case executes `scripts/` from
  the PR worktree.
- The review `codex-agent` sets `PYTHONSAFEPATH=1` so host Python invoked
  with `-`, `-c`, or `-m` cannot import PR-controlled modules from its working
  directory (also after switching to `WORKSPACE_PATH`). Keep sibling imports
  anchored to the trusted support directory; `workspace_init.sh` uses `-I -B`
  independently of job env. Python older than 3.11 ignores `PYTHONSAFEPATH`.
- Host-side static-context reads of checkout-controlled `README.md` and
  `agents.md` skip symbolic links rather than following them into prompt text.
  Required `unattended_system_instructions.md` and `ai_pipeline.md` symlinks
  instead fail prompt assembly before any content is written; review checkout
  credential persistence is unchanged.
- Static prompts omit the optional overflow-runbook pointer when
  `probably_unnecessary_but_read_if_stuck.md` is a symlink; regular runbooks
  still receive the pointer. A symlink emits a warning without directing the
  model to read its target from the credential-bearing checkout.
- The review gate's existing PR read validates the same-repository head and
  exports `review_checkout_sha` only for a 40-hex SHA. `Checkout repo` uses
  that SHA for every PR event, so reviewer and other `GITHUB_WORKSPACE` file
  reads see the verified PR head rather than the dispatching branch or a stale
  pull-request event payload; the no-PR push route still uses `github.sha`.
  Fork, missing-repo and invalid-head PRs skip both review and deterministic
  auto-merge. Before OpenCode setup, the job
  refuses project OpenCode configuration/plugins in the source checkout; it
  also requires an existing split workspace and checks its configuration
  before reviewer launch. An unreadable fetched git head fails the job; a
  source/workspace/PR-metadata SHA mismatch, or a branch that advances during
  fetch, sets `AUTOFIX_STALE_BASE_SKIP` so no reviewer, editor or merge acts
  on stale files.
- Before host-side pre-review helpers run against the PR tree, `review_autofix.yml`
  requires a Python that honors `PYTHONSAFEPATH`. The Semble and Serena
  bootstrap import probes run from private neutral directories; Serena's
  trusted absolute-path handshake probe retains the project cwd for
  `--project-from-cwd` while clearing `PYTHONPATH` and using safe-path for
  the probe only. Its installed server keeps script-directory imports.
  Serena requires an absolute `HOME` for Codex config writes; a relative or
  missing `HOME` leaves the tool unavailable rather than writing under the PR tree.
  Other pre-review `python3 -c`, `-m` and stdin calls set `PYTHONSAFEPATH=1`
  per call, including consolidator, reviewer and host-side editor helpers;
  reviewer Python imports require absolute trusted support directories rather
  than falling back to the PR checkout. The host-side partial-finalize timeout
  extractor also uses safe-path when reading the workflow YAML. Script-file
  imports and the checkout's Git auth are unchanged.
- `orchestrate.yml` and `orchestrate_clarify_respond.yml` also assemble static
  context on the host. They reject symlinked required instructions and pipeline
  files before writing prompt output, and omit symlinked local agents files,
  `README.md`, and the optional overflow-runbook pointer with warnings. Regular
  files retain their existing content and local agents-file precedence in the
  clarify-respond workflow. Checkout credential handling is unchanged.
- Review's optional break-glass scan and conflict-resolution prompt rendering
  also use safe-path Python in the credential-bearing checkout. The conflict
  preparation path can run before reviewers when pre-review conflict resolution
  is enabled; resolver retries remain host-side but no longer import checkout
  modules through Python's implicit current-directory entry.
- `internal-review.yml` itself must not forward a `with:` input that
  `review_autofix.yml` on `main` does not define yet: GitHub validates the
  call against `main`'s file, so every review run on the PR adding the input
  ends as a zero-job `startup_failure` (runs 36088124636 through 36095647423
  on PR #4438, `input "claude_fixer_converged_head" is not defined`). Add the
  input to `review_autofix.yml` (and the `@stable`-pinned consumer
  `ai-review.yml`, which moves in lockstep with the release), and in this
  repo dispatch `review_autofix.yml` directly for it.
- Consequence for contributors and the unattended editor: a helper on the PR
  branch may not depend on a new workflow export until that export is on
  `main`. New variables a staged helper reads must default inside the helper
  (`unattended_system_instructions.md` §8), typically to a
  `${RUNTIME_DIR}/<artifact>` path, and the helper should publish the resolved
  value to `GITHUB_ENV` when later steps consume it.
- Incident: PR #4174 (`ai/issue-4173`, head `662aacb`) added
  `LINKED_ISSUE_METADATA_FILE` as a required env of
  `scripts/review_collect_pr_metadata.sh` together with the matching
  `review_autofix.yml` export. The export never ran, and review runs
  35546298657, 35549937758, 35551938072, and 35552937934 all failed in
  "Collect PR metadata" with `required env LINKED_ISSUE_METADATA_FILE is
  unset` while the stall poller kept re-dispatching. `main` now exports the
  variable as well, so the same path is defined on both sides.
- `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` remains a registry for compatibility;
  it is staged from the verified workflow SHA, not a moving `main` checkout.
  Changes to a PR's support scripts take effect in reviews only after a
  trusted workflow update. Missing required runtime scripts fail closed.
  The existing `STAGE_MAIN_PINNED_DIVERGENCE` notice still compares PR data
  against trusted files but never selects the PR copy for execution.
- Editor preconditions are checked before the reviewers: the preflight step
  runs `scripts/review_apply_fixes.sh --preflight` (`REVIEW_EDITOR_PREFLIGHT`,
  kill switch `REVIEW_EDITOR_PREFLIGHT_ENABLED`). A new `: "${VAR:?…}"` guard
  in that script must also be listed in `review_apply_fixes_preflight()`
  (contract-tested), and the variable must already be set when the preflight
  step runs.

## Isolated Codex agents

Every Codex agent that reads untrusted text (issue bodies, comments, PR diffs,
CI and workflow logs, audited code) runs through
`scripts/codex_isolated_exec.sh`, never as a host `codex` process. Prompt
injection in that text therefore cannot read `GH_PAT` (`GH_TOKEN`), the
OpenRouter key, the Telegram secrets, or the checkout's `.git` (whose config
carries the `GH_PAT` remote URL and the checkout extraheader).

- **Container.** A Docker container built from a fixed, generated build
  context (Node 22 + Python 3 + git + build-essential + ripgrep + the
  `CODEX_VERSION` Codex CLI), run with `--network none --read-only --cap-drop
  ALL --security-opt no-new-privileges --init`, the runner's UID, and no
  environment from the runner. Nothing from the host is mounted except a
  disposable copy of the working directory (at its own absolute path), the
  broker socket, the helper's own support files, and the runtime files a call
  site names with `--include` (read-only).
- **Model traffic.** `scripts/clarify_openrouter_broker.py` runs on the host
  with the key (`broker` mode, Unix socket) and in the container as a loopback
  bridge (`bridge` mode). It forwards only `POST /api/v1/responses` for the one
  model the call names. The provider-hosted `web_search` tool keeps working.
- **Modes.** `read-only`: a copy of the tracked regular files (all top-level
  directories; symlinks, `.git`, `.env*`, `secrets`, `credentials`, `.ssh`,
  `.npmrc`, `.netrc`, `.config`, credential-store filenames (including `.conf`,
  `.cfg`, `.properties`, `.xml`, and dot-separated names such as
  `oauth.secret.properties`) and key files
  skipped; source modules such as `secret_manager.py` remain visible;
  files over 2 MiB skipped and logged; 50,000 files / 512 MiB cap) mounted
  read-only. Security audits supply explicitly scoped oversized files, and in
  full scans every other eligible oversized text file, through capped read-only
  chunks. An explicitly scoped file past a cap stops the audit before the model
  runs; other binaries and files past the caps go to the coverage note.
  `workspace`: a copy of the directory excluding the same
  credential paths (allowed symlinks kept as symlinks); afterwards every
  changed regular file is written back with
  mode 0644/0755 and removed files are deleted
  (also after a failed or interrupted attempt). A new or changed symlink, a
  special file, or a host path that changed since the snapshot rejects the
  whole transfer before the first host write. Replacements and backups are
  staged first and a write failure rolls back changed paths
  (`scripts/codex_isolated_workspace.py`). Both modes get a credential-free
  synthetic `.git` whose `HEAD` contains only allowed blobs from the host's
  `HEAD`, so filtered tracked files cannot be retrieved with `git show`.
- **Heal scope from failing tests.** A workflow-failure heal's edit scope
  (`<!-- ai:workflow-heal-scope:v1 paths=... -->`) is frozen before the
  diagnosis runs (#6463), from the verified run's workflow paths, the crash
  file and the ownership facts, plus `tests/**` and `changelog.d/*.md`. For
  a CI failure that left the implementer with tests only, so when a test
  and the code disagreed it rewrote the test to match the code (heal PRs
  #6907 and #6916 flipped the security-pass cap rule that #6906 restored).
  Since operator decision Q37: A (2026-10-09) the intake also extracts the
  failing test names and files from the verified runs' own filtered job
  logs (`heal-scope failing-tests`, shapes: the CI shard runner's
  `TEST_CASE_EVENT ... "status":"fail"` and `FAIL  test_x`, pytest's
  `FAILED tests/x.py::test_y`, and for a test file CI runs as a script the
  traceback frame `File ".../tests/x.py", line N, in test_y` or unittest's
  `FAIL: test_y (...)` / `ERROR: test_y (...)`), freezes them with the other inputs, and
  `heal-scope render` resolves each name to the test file that defines it
  (`git grep` at the scope commit) and that file's stem to the subject it
  covers: `scripts/<stem>.sh`, `scripts/<stem>.py`,
  `.github/workflows/<stem>.yml` (also with `-` for `_`), only when the file
  exists at the scope commit and passes the usual path rules (never
  `.claude/` or `.github/ai/`). Guard tests check other files, so their own
  name maps to no subject; `HEAL_SCOPE_GUARD_TEST_SUBJECTS` maps them to the
  files their fix edits (`tests/test_ci_job_split_contract.py`,
  `tests/test_ci_inventory_parity_order_contract.py` and
  `tests/test_ci_shared_shell_block_guard.py` to
  `.github/workflows/ci.yml`; `tests/test_ci_poll_test_sharding.py` to
  `ci.yml`, `mark-stable.yml` and `test-and-mark-stable.yml`).
  A failing `tests/test_workflow_file_size_limit.py` adds every workflow at
  or above 480,000 bytes at the scope commit, `scripts/stage_workflow_support.sh`,
  `docs/INVENTORY.md`, and `scripts/<workflow>_step_*.sh` for each oversized
  workflow (`-` becomes `_`) so the heal can create the step scripts §27
  asks for. That step-script glob is the only glob a scope accepts besides
  the trailing `tests/**` and `changelog.d/*.md`, and only as a guard
  subject. The diagnosis still cannot add a path. The
  heal prompt tells the healer to pick the wrong side from the documented
  behaviour (README, agents.md, changelog fragments, the introducing issues
  and PRs), never from "the test should match what the code does now".
  `tests/test_workflow_failure_heal_scope_tests.py` covers extraction, the
  mapping, the render CLI at a scope commit and the intake wiring.
- **Merge-base freshness.** `scripts/pr_checks_lib.sh` carries
  `_pr_base_fresh_for_merge <pr> [<head_sha>] [<base_ref>]` beside the
  check-runs gate. Every path that enables auto-merge or merges directly
  (`scripts/review_enable_auto_merge.sh`, the `deterministic-skip-merge`
  job, `scripts/review_rb_judge.sh`, and the poller's backward-scan,
  `attempt_merge` and review-blocked merge sites) calls it first. It reads
  `compare/{head}...{base}` and, only when the base gained commits, the
  PR's file list; when the two sets share a path (renames under both
  names; 300 base-side files counts as overlap) it requests
  `PUT pulls/{n}/update-branch` bound to the head and returns 1, so the
  caller skips the merge and the `synchronize` run re-validates CI and
  review on the combined tree. `fresh`, `clean` and `disabled`
  (`MERGE_BASE_FRESHNESS_ENABLED`) return 0. `unknown` (any API failure,
  or an unresolved head or base) logs a warning with `action=defer` and
  returns 1 without an update request, so the merge waits for the next
  poll tick or review round (#6988, security-pass finding
  `merge-freshness-unknown-proceeds`). Every poller merge (final
  integration merge, `attempt_merge`, backward-scan, ready-to-merge loop,
  review-blocked merge / force-merge / no-fix, noop-suspicious
  force-merge) passes `--match-head-commit` with the head SHA its checks
  and freshness gate saw, through `_orch_squash_merge_bound` or inline
  (log `ORCH_MERGE_HEAD_BOUND`); an unresolved head never reaches `gh`.
  Review/autofix failure markers count only as the comment's trailing
  standalone line (`_FAILURE_MARKER_TRAILING_RE`), so a marker quoted in
  an error line cannot shadow the real one. The `deterministic-skip-merge` job reads the
  library from the gate's verified support commit (`.codex-freshness-src`);
  an unverified checkout skips the freshness gate with a warning and
  refuses auto-merge, because the required-checks wait below cannot run.
  A PR file list of 3,000 entries (GitHub's cap) counts as overlap. The
  review-blocked judge's `merge_with_followup` and the poller's force-merge,
  no-fix and `merge_with_followup` sites are gated too. The orchestrator's
  sub-issue path already aligns and defers through
  `_sync_integration_and_rebase_subissue`, and the final integration PR is
  synced by the poller every tick, so those paths see `fresh`. Operator
  decision Q35: A (2026-10-09), after #6741 merged green against a base
  that #6549 had changed under its tests. `tests/test_pr_base_freshness_gate.py`
  covers the outcomes, the switch and every wiring point.
- **Required checks before auto-merge.** `gh pr merge --auto` merges as
  soon as the base's required status checks pass, and an unprotected base
  has none, so the review's auto-merge paths landed #6906 on `main` while
  its own PR CI run showed `orchestrate-poll (3)` failing. The same two
  paths and the review-blocked judge's `merge` and terminal `fix` paths
  now call `_pr_wait_for_required_checks <pr> <head> <base>`
  (`scripts/pr_checks_lib.sh`) first: it polls `_pr_checks_completed` with
  the calling run excluded (`PR_CHECKS_SELF_RUN_ID`) for up to
  `AUTO_MERGE_CHECKS_WAIT_MINUTES` (default 45, every
  `AUTO_MERGE_CHECKS_POLL_SECONDS`, default 60), enables auto-merge only on
  `ok`/`allow_all`, and refuses on a settled failure (`PR_CHECKS_LAST_PENDING`
  is 0 while the gate still blocks) or a timeout; a failed query is retried
  until the budget is spent. It logs
  `AUTOFIX_AUTO_MERGE_CHECKS pr=<n> head_sha=<sha> outcome=<...>` (a
  timeout while a listed CI workflow run is still queued or running adds
  `reason=ci_run_pending`: `_pr_head_ci_runs_pending` keeps the wait going
  while any `AUTO_MERGE_WAIT_WORKFLOWS` run, default `CI`, on the head is not
  completed, because its check-runs do not exist until its jobs start; a
  listed workflow whose every run on the head ended `cancelled` or
  `startup_failure` refuses with `outcome=failed reason=ci_run_cancelled`
  until a re-run finishes), and a
  missing library fails the wait closed. A head with no check-runs yet
  (`PR_CHECKS_LAST_TOTAL=0`) is polled two more intervals before it counts
  as green. After a wait that actually polled, every caller
  (`review_enable_auto_merge.sh`, the `deterministic-skip-merge` job and
  the judge) re-checks freshness before merging. The
  orchestrator's direct merges already gated on `_pr_checks_completed`.
- **Dependencies.** `codex_isolated_exec.sh prepare --deps` (implement) installs
  dependencies once per job. The network-isolated, credential-free container sees
  only staged Node manifests and filtered third-party Python requirements
  from both `requirements.txt` and `pyproject.toml` when present,
  never the source tree. A host-side
  `scripts/dependency_registry_proxy.py` accepts only HTTPS CONNECT tunnels to
  allowlisted public registry hosts, vets all resolved IPs and connects by IP.
  The review dependency container uses the same proxy; the default allowlist is
  PyPI and npm/Yarn registries, replaceable via `DEPENDENCY_PROXY_ALLOWED_HOSTS`.
  A missing proxy skips dependency installation without restoring direct
  container network access; review preparation continues, but validations
  needing those dependencies may be unverified.
  Each review install command runs under `DEPENDENCY_INSTALL_TIMEOUT_SECONDS`
  (default 600) inside the container; a command that exceeds it is killed and
  reported as a failed install, and a dependency container that still exceeds
  its 900-second budget (`timeout` exit 124, or 137 once the budget has
  elapsed; an earlier 137 is an OOM kill) is treated the same way,
  with a warning, instead of as `Review dependency isolation failed`. Any
  other non-zero container exit stays fatal. Before this, pip backtracking
  through a consumer's unpinned `[dev]` extras hit the 900-second budget
  twice per run (the Claude prepare and the OpenCode fallback), the editor
  was skipped and the PR looped on "produced no output, will retry"
  (`drhyg_ecommerce_automation` PR #70, 2026-10-09).
  An editable source install runs separately with `--network none` for
  parsed `pyproject.toml` projects or requirements with a regular `setup.py`,
  including Node/Python hybrids. Requirements-only projects without an
  installable source skip that step; a failed dev dependency install warns
  even when retrying base dependencies succeeds.
  `prep-finalize` restores source files and keeps dependency output in the
  sandbox ("prep roots"), never copying it back.
  The agent itself has no network: it marks validators it cannot run
  UNVERIFIED instead of installing them.
- **Trusted copies.** The helper reads its support files from its own
  directory. Callers run it from a copy no agent can write: implement runs the
  helper, `codex_thread_reuse.sh` and every script its Codex, repair and later
  steps execute from `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` (now staged in every
  repository), and runs `after_run` workspace hooks from a copy taken before
  the editor. In the orchestrator poller, the review-blocked fix and the
  integration-conflict judge work in separate git worktrees under
  `RUNTIME_DIR`; the poller (not the agent) fetches, merges, checks
  conflict markers and the merged sub-issue fingerprints, commits and pushes.
  Before that push, it rejects judge changes outside the conflicted paths;
  all conflicted paths may contain only lines from either side, retaining each
  side's line order and duplicate counts when combining them.
  Lines shared by both sides must also remain at their minimum shared count;
  there is no automated deletion override, including for lines both sides added.
  Deleting a conflicted file present on both sides or using a file mode from
  neither side is rejected; one-sided delete/modify conflicts may still resolve
  to deletion. Symlink and gitlink resolutions may select an intact target from
  either merge side; gitlinks are not read as text blobs.
  Lines inherited unchanged from the common base cannot be duplicated; a
  provenance check that exceeds its fixed work limit rejects the resolution
  without pushing.
  Its push uses a one-shot credential helper instead of storing `GH_TOKEN` in
  the shared Git config of the judge worktree.
  A failed publication logs a warning but counts as a completed judge
  invocation; the next poll tick rechecks mergeability rather than terminalizing
  the project. An already-up-to-date merge creates no empty commit.
  The poller's review-blocked `fix` path prepares a fix only for the issue's
  implementation PR with a same-repo head, verifies the fetched branch tip
  against the PR head SHA, and rechecks the head before pushing; failed
  provenance checks skip the fix. It checks every staged path before commit:
  every path must occur in the PR's complete paginated changed-file list
  (including rename source paths); judge citations cannot authorize writes.
  An unavailable or incomplete PR file list and any out-of-scope edit reject the whole fix,
  warn via Telegram, and consume a review-blocked retry without a push. Empty
  staged sets also reject; listing failures report the unverified staged paths.
  Staged edits to `scripts/`, `prompts/`, `.github/`, `workflow-templates/`,
  or `.claude/` always reject through that same path, before the PR file list
  is read and even if present in it (#6478): the judge is driven by untrusted
  PR comments and a filename check cannot vouch for content. The reason is
  `workflow_edits_disabled` when `ALLOW_WORKFLOW_EDITS=false`, otherwise
  `protected_path_forbidden`; the judge prompt carries a
  `Protected-path fixes: unavailable` line so it picks `merge_with_followup`
  or `close_and_reissue` for such findings. The standalone
  `scripts/review_rb_judge.sh` fix path is unchanged. `.github/prompts/` and
  `.github/scripts/` remain excluded from staging and forbidden when pre-staged.
  The review-blocked poller rejects a selected PR whose head repository is
  not the origin before its diff reaches the judge. For open PRs, branch
  preparation also requires the fetched origin tip to match the PR head SHA;
  identity, ref, or fetch failures defer the judge without consuming a fix
  retry, so the next poll tick can check again.
  The review-blocked judge's OpenCode fix writer runs in
  `scripts/review_untrusted_sandbox.sh`. The sandbox keeps `GITHUB_WORKSPACE`
  pointed at the checkout containing `.git` and snapshots/transfers the
  separately validated per-PR `WORKSPACE_PATH`; invalid paths fail before the
  writer runs.
- **Sites.** plan, implement (attempts, post-Codex repair, diagnose, PR issue
  summary), validate discover / diagnose / self-heal, the validation discovery
  bootstrap, the orchestrate decomposer, the four
  workflow-log-analysis passes and the consumer retro fan-out, check-failure
  triage, the security audit, the workflow failure heal intake, and the
  activation verifier (`scripts/activation_verify.sh`).
  `tests/test_codex_agent_isolation_contract.py` fails when a direct `codex`
  launch appears anywhere else (clarify keeps its own
  `scripts/clarify_isolated_run.sh`). The poller judges are not on this list;
  see "Poller judges" below.
- **Poller judges.** All five poller judges (`WAVE_JUDGE`, `STALL_JUDGE`,
  `INTEGRATION_JUDGE`, `SECURITY_JUDGE`, `RB_JUDGE`) intentionally run on
  `scripts/review_untrusted_sandbox.sh`, through `poller_claude_judge` →
  `poller_judge_isolated` → `_poller_rb_judge_sandbox_attempt`
  (`prepare-ephemeral` / `run` / `cleanup`), not on `codex_isolated_exec.sh`.
  Phase 5c (#6208) moved them there from #6187's `codex_isolated_exec.sh`
  path; `ORCH_CODEX_ISOLATED_EXEC` is still defined in the poller, but no judge
  uses it. Reasons (decision recorded in #6607): every attempt gets a fresh
  snapshot, and a root is never reused after a Claude exit 75; the
  review-blocked fix and integration write paths use validated transfer with
  rollback of new untracked files; the judges share one OpenCode fallback
  stack with the review write roles; and escalation is accounted per role
  through `JUDGE_ISOLATION` / `RB_JUDGE_ISOLATION`
  (`JUDGE_ISOLATION_MAX_FAILURES`, `RB_JUDGE_ISOLATION_MAX_FAILURES`), not
  `CODEX_ISOLATION`. For these judges the "codex" engine means OpenCode in a
  fresh review sandbox (`write_opencode_config.sh`), never the Codex CLI and
  never the host; a Claude exit 75 retries OpenCode in a new root, and a
  sandbox failure returns 77 and defers the judge with no host fallback. The
  two isolation stacks have different failure modes, log prefixes, retry
  accounting and support-file staging lists, so a fix to one does not cover
  the other. `test_poller_judges_stay_on_the_review_sandbox` in
  `tests/test_codex_agent_isolation_contract.py` pins this decision;
  converging the stacks means revising both this bullet and that test.
- **Claude engine.** `claude_run` (`scripts/ai_engine.sh`) never starts the
  Claude Code CLI on the runner: each account attempt runs
  `codex_isolated_exec.sh run --engine claude` in the same container, with
  the pinned CLI (`cli_version` in `.github/ai/claude_engine.json`) added to
  the image. `scripts/claude_anthropic_relay.py` is the host broker: it reads
  the account's `0600` token file (`--claude-token-file`, never mounted or
  passed in the environment), swaps it into each request and allows only
  `--claude-models` (the role's model and the probe model); the container
  holds `CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder` and
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8765`. A `read` profile runs in
  `read-only` mode, write profiles in `workspace` mode. The rendered settings,
  the `gh_api_write_guard.py` hook and the instructions are copied to
  `/support/settings.json`, `/support/guard.py` and `/support/instructions.md`.
  `$RUNNER_TEMP/claude-isolated-home` (`ai_engine_claude_home`) is mounted as
  `~/.claude` so `--resume` finds the session; `codex_thread_reuse.sh` looks
  there for a resumable session before sending the continuation prompt. A
  write role in a job that prepared a workspace sandbox (implement:
  `CODEX_ISOLATED_ROOT` with `CODEX_ISOLATED_MODE=workspace`) runs in it, so
  Claude implement attempts get the preinstalled dependencies too. `hide_claude_md` sets
  `CODEX_ISOLATED_HIDE=CLAUDE.md`: the top-level `CLAUDE.md` is left out of
  the copy and the synthetic `.git`, and the write-back never creates or
  changes it (`CODEX_ISOLATION transfer ignored=CLAUDE.md reason=hidden`).
  Helper exit `75` (`CODEX_ISOLATION unavailable engine=claude reason=`
  `docker_missing` / `support_missing` / `image_build_failed`) makes
  `claude_run` log `CLAUDE_POOL … outcome=unavailable` and return `75` with
  `AI_ENGINE_FALLBACK … reason=isolation_unavailable`, so the role runs codex.
  Exit `73` (`CODEX_ISOLATION relay_unavailable engine=claude`, for example a
  token file that is not `0600`) moves to the next account. A root runner
  passes `IS_SANDBOX=1`, which the CLI needs for `bypassPermissions` as root.
  `tests/test_codex_agent_isolation_contract.py` fails when `claude -p` is
  started outside a container entrypoint; the token step's fixed-prompt usage
  probe (`scripts/claude_pool_token.sh`) is the one listed exception.
- **Fallback policy (plan item 3e, D1).** `ai_engine_fallback` records every
  fallback (one `v1` TSV line in `AI_ENGINE_FALLBACK_RECORDS_FILE`, default
  `${RUNNER_TEMP}/ai-engine-fallback-records.tsv`) and sets
  `AI_ENGINE_FALLBACK_EXIT`. Under `AI_ENGINE_FALLBACK_POLICY=capacity` (the
  default, also for unknown values) only the capacity reasons `all_gated`
  (the pool step gated every account: `CLAUDE_POOL_REASON` or the
  `CLAUDE_POOL_REASON_FILE` token `claude_pool_token.sh` writes, default
  `${RUNNER_TEMP}/claude-pool-reason`) and `all_usage_limit` (every tried
  account hit its usage limit) keep exit `75` and the codex/OpenCode path;
  every other reason (including the helper's isolation exit 75 above, now
  `isolation_unavailable`, and `all_accounts_failed`, which now means a mixed
  or rejected-token failure) makes `claude_run`, the Claude branches of
  `clarify_isolated_run.sh` and `review_untrusted_sandbox.sh` exit **`76`**
  and log `::error::AI_ENGINE_FALLBACK_REFUSED role= reason=`. Exit 76 is a
  failure everywhere: no caller runs codex for it, `security_audit.sh` fails
  the audit, and the poller's `_poller_rb_judge_sandbox_attempt` maps a raw
  sandbox 76 to the isolation deferral (77, reason `engine_fallback_refused`;
  its own rejected-transfer 76 is kept apart). `AI_ENGINE_FALLBACK_POLICY=always`
  restores exit 75 for every reason (rollback). The last step of each
  Claude-calling job in clarify, plan, orchestrate, orchestrate_clarify_respond,
  orchestrate_poll, security-audit and validate runs
  `scripts/ai_engine_fallback_report.sh` from trusted support: one
  `engine_fallback` heal dispatch per refused (role, reason), fingerprinted
  on role and reason only (`workflow_failure_heal.py
  engine-fallback-fingerprint`, deterministic reason `engine_fallback_refused`,
  no run references or log reads), plus one Telegram WARNING. Implement and
  review report refusals through their existing heal reporters.
- **Merged-PR guard.** Both `.claude/hooks/pr_merge_status_guard.py` and its
  `workflow-templates/` copy check origin PR history only for pushes to origin.
  An explicit `--repo` or positional remote naming a different or unverified
  destination requests human confirmation instead of silently checking the
  checkout's origin. A positional repository overrides `--repo` when both
  are supplied; without a positional repository, `--repo` is the fallback.
  Explicit URL destinations request confirmation even when their slug matches
  origin, since Git's `url.*.insteadOf` or `pushInsteadOf` can rewrite the URL.
  Deletion-only and tag-only pushes to such URLs follow the same rule.
  Remote URLs are never printed in the prompt (they may
  contain credentials). Pushes with `git -c`, `--config-env`, or inline
  `GIT_CONFIG_*` or `GIT_CONFIG` assignments, or an `env` wrapper, ask too:
  those per-command settings can affect the push destination, so the guard
  does not trust its stored URL or PR history for that push. Wrapped commits
  are checked in the directory selected by `env -C` or `GIT_DIR`; an explicit
  commit directory override that cannot be resolved (including `env -C` and
  an appended `GIT_DIR+=` / `GIT_WORK_TREE+=`) asks for confirmation without
  querying the session checkout's PR history. An absolute `env -C` or
  `git -C` path is resolved even inside shell control flow. Other ambiguous
  commit directories warn and check the checkout; an unparseable `env -S`
  command also asks for confirmation. A push whose directory is unresolved
  because of an explicit override (an appended `GIT_DIR+=` /
  `GIT_WORK_TREE+=`, which Git applies on top of a shell state the hook
  cannot read, or an unresolvable `-C`, `env -C`, `GIT_DIR` or `--git-dir`
  path) asks for confirmation without querying the session checkout's PR
  history, since that checkout is not the pushed repository. A push whose
  directory is unknown only because of shell control flow or an unresolved
  `cd` is still checked against the session checkout, which can block, and
  otherwise asks.
  Leading redirections, including those after environment assignments, do
  not bypass commit/push detection. A spaced, quoted or escaped digit before a
  redirection (`2 >out`, `'2'>out`) is a push refspec with the normal check;
  only digits glued to it (`2>&1`) are a file descriptor. An
  unresolved push source asks rather than checking the session checkout's HEAD.
  Shell `-c` scripts (`bash`, `sh`, `zsh`, `dash`, also by absolute path),
  `eval` arguments and `$(...)` / backtick / `<(...)` bodies are parsed up to
  three levels deep and get the same check; wrapped text that cannot be read
  (an expansion in command position, an unterminated body or heredoc, deeper
  nesting) asks when it could run a Git write. Transparent prefixes (`time`,
  `command`, `nice`, `timeout`, ...) are skipped and checked; `sudo`, `xargs`
  and other runners with a Git write in their words ask (#6038); `... | bash`
  is still not inspected.
- **No MCP tools inside.** Serena (and any other MCP server) is not configured
  in the container, so isolated prompts carry no Serena hints. Semble results
  are rendered into prompts on the host and are unaffected.
- **Failure modes.** Missing Docker, an image build, broker or snapshot failure
  exits 1 with `::error::CODEX_ISOLATION …`; Codex never falls back to the
  host, and there is no switch that turns isolation off. A stall-guard
  `SIGKILL` can leave a container behind; the next run on the same sandbox
  root removes it by label, and an orphan without its broker has no model
  access. Implement's final cleanup removes its persistent sandbox root;
  a failed cleanup warns. Log prefix: `CODEX_ISOLATION`.
- **Prebuilt images.** Every image build goes through
  `scripts/sandbox_image.sh build --family <f> [--build-arg K=V]... [-f DOCKERFILE] CONTEXT`,
  which pulls `${SANDBOX_IMAGE_REGISTRY}:<family>-<first 32 hex of the input hash>`
  and falls back to the local `docker build -q` (same args, plus the label
  `coding-workflows.sandbox-input=<full hash>`). It runs under the caller's
  `env -i` / `env -u` wrapper, so pass `SANDBOX_IMAGE_REGISTRY` and
  `SANDBOX_IMAGE_PULL_TIMEOUT_SECS` through explicitly. A caller without the
  helper next to it builds locally, so support-copy lists that ship a build
  site must ship `sandbox_image.sh` too (`tests/test_sandbox_image.py` checks
  them). `codex_isolated_exec.sh image-context [--engine claude] --out DIR`
  writes the generated context, and `scripts/sandbox_images_publish.sh`
  mirrors each site's default inputs; change a site's default and the
  publish script together. Log prefix: `SANDBOX_IMAGE`.

- **Bounded review dependency installs (#6835).** With `REVIEW_BOUNDED_DEPENDENCY_INSTALL_ENABLED` on (default `true`; `false`/`0`/`no`/`off` turn it off, any other value turns it off with a warning) the review dependency container's optional installs and pytest bootstrap share one `REVIEW_DEPENDENCY_INSTALL_BUDGET_SECS` deadline (default 600, 30-780, normalized on the host) on top of each command's existing `DEPENDENCY_INSTALL_TIMEOUT_SECONDS` budget. A step that times out, or starts after the deadline, logs `::warning::Review dependency install timed out|skipped (step=<name>)` and preparation continues; an early exit 137 (OOM kill) is not called a timeout. Venv, container isolation and snapshot-refresh failures stay fatal; registry-proxy failures keep their existing warn-and-skip behaviour.
- **Required CI check before auto-merge (#6963).** `_pr_wait_for_required_checks` also requires the configured CI check (`AUTO_MERGE_REQUIRED_CI_CHECK`; unset or empty means `lint` here for PRs into every base (`ci.yml`'s pull_request trigger covers every base since #7283; consumers are off), `none` turns it off, an explicit name applies to every base, and a malformed value falls back to that default or, where there is none, to the never-matching `__invalid__`, so it fails closed) to exist on the reviewed head and succeed: an absent, pending or unreadable check keeps polling until the budget is spent (`timeout`), any failed, skipped, neutral or cancelled run with that name refuses (`failed`), and the two-interval zero-check grace applies only when the requirement is off. `_pr_checks_completed` only records `PR_CHECKS_LAST_CI_NAME` / `PR_CHECKS_LAST_CI_STATE` from the listing it already read; its result is unchanged. The log line gains `ci_check=<name> ci_state=<state>`. Only check-runs whose `details_url` run id belongs to an `AUTO_MERGE_WAIT_WORKFLOWS` run on the head count (`PR_CHECKS_CI_RUN_IDS`, read from the existing `actions/runs?head_sha=` call), because `lint-pr-body-auto-close.yml` and `lint-plan-archival.yml` also have a job named `lint`; an unreadable runs listing turns a success into `unknown`, and `AUTO_MERGE_WAIT_WORKFLOWS=none` falls back to name-only matching. The binding is by workflow name, not workflow path, so a same-repository PR could still add a workflow named `CI` with a job named `lint`.
- **Freshness updates after approval (#7084).** `_pr_base_fresh_for_merge` posts `<!-- ai:merge-base-sync:v1 pr=<n> from=<approved head> sync=<k> -->` after each accepted `update-branch` (`_pr_base_sync_marker_post`, log `MERGE_BASE_SYNC_MARKER`). It stops updating once the workflow login's markers on the PR reach `MERGE_BASE_FRESHNESS_MAX_SYNCS` (default 2; `_pr_base_sync_count`), and then returns 0 so the caller merges after its required-checks wait (`reason=max_syncs`). In the review gate, `gate_freshness_sync_fast_path` runs `scripts/review_head_gate.sh freshness-sync` on the marker comments the gate already read. It skips the reviewer panel (`DET_SKIP_REASON=freshness_sync`, routed to `deterministic-skip-merge`) only when the head is GitHub's own merge of the marker's `from` head with a commit already on the base: two parents, `from` first, committer `web-flow` with a verified signature, and `compare/<second parent>...<base>` `ahead` or `identical`. A conflicted PR (`mergeable=false` or `dirty`) and a PR eligible for the single-issue security pass are reviewed normally, as on the doc-only / small-diff skip. Any failed check or read reviews normally. `force-review` wins, and `MERGE_BASE_SYNC_FAST_PATH_ENABLED=false` turns the fast path off.
- **Image-build retries (#7196).** `build_image` in
  `scripts/codex_isolated_exec.sh` retries only the image build, up to
  `CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS` (default `1`, capped at `5`; an
  invalid value warns and uses `1`), waiting
  `CODEX_ISOLATED_IMAGE_BUILD_RETRY_SLEEP_SECS` (default `15`) times the
  attempt number between tries. Implement's two `prepare --deps` calls set
  it to `3` as an inline prefix, never a step `env:` or `GITHUB_ENV` entry,
  so `run` builds and the Claude engine keep one attempt and its exit-75
  fallback. Every other isolation failure is still not retried, and an
  exhausted build fails closed as before. Logs
  `CODEX_ISOLATION image_build attempt=<n>/<max> outcome=ok|retry|fail`
  (only when more than one attempt is allowed).

- **Bound freshness-update markers (security-pass findings `freshness-sync-signed-merge-review-bypass`, `merge-base-sync-cap-proceeds-on-overlap`).** This supersedes the #7084 bullet's cap rule. After an accepted `update-branch`, `_pr_base_sync_observe_update_commit` (`scripts/pr_checks_lib.sh`) polls the PR head (`MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS`, default 5, 1-20; `MERGE_BASE_SYNC_HEAD_POLL_SECONDS`, default 3, 1-30) and requires the new head to have exactly two parents, the approved head first and `PR_BASE_FRESHNESS_BASE_SHA` (the compare's base tip) second; past 250 base commits that tip is unknown and no marker is posted, because mere containment would also accept a web conflict-resolver merge against a newer base tip, committed by `web-flow` with a verified signature. Only then is the marker posted, now `<!-- ai:merge-base-sync:v1 pr=<n> from=<H> sync=<k> base=<X> merge=<M> -->`; otherwise `MERGE_BASE_SYNC_MARKER ... outcome=skipped reason=head_not_observed|base_tip_unknown|pull_read_failed|commit_read_failed|parents_mismatch|not_github_merge|base_mismatch|unbound_update` and no marker. `review_head_gate.sh freshness-sync` skips review only for `head == merge` with parents `[from, base]` (new reasons `unbound_marker`, `marker_not_for_head`, `base_parent_mismatch`). Remaining overlap always returns 1: `MERGE_BASE_FRESHNESS_MAX_SYNCS` is only a warning threshold (`action=update reason=max_syncs_exceeded`), never a merge (`action=proceed reason=max_syncs` is retired). The four library-missing `_pr_base_fresh_for_merge` stubs return 1 with `outcome=unknown reason=library_unavailable`.
- **Claude capacity wait, Phase 1 (issue #7251).** `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` (default on since issue #7370: unset, empty or `true` in any letter case is on; `false` is off; any other value is off with a `::warning::`) turns a capacity fallback (`all_gated`, `all_usage_limit`) under `AI_ENGINE_FALLBACK_POLICY=capacity` into a wait. `ai_engine_fallback` then sets `AI_ENGINE_FALLBACK_EXIT=80` (`_AI_ENGINE_EXIT_WAIT`), writes a `v1` record with action `wait`, logs `AI_ENGINE_CAPACITY_WAIT role= reason=` and sends no codex-fallback Telegram note. `claude_run`, `claude_run_selected` (which runs codex only on 75), `clarify_isolated_run.sh` and `review_untrusted_sandbox.sh` return 80 unchanged, and a wait never reaches the review sandbox's validated transfer. `AI_ENGINE_FALLBACK_POLICY=always` and non-capacity reasons are unchanged (75 / 76). `scripts/ai_engine_fallback_report.sh` records waits: with `CLAUDE_CAPACITY_WAIT_ITEM_KIND=issue|pr` and `CLAUDE_CAPACITY_WAIT_ITEM_NUMBER` (also `CLAUDE_CAPACITY_WAIT_STAGE`, `CLAUDE_CAPACITY_WAIT_SOURCE_COMMENT`; all default to a run-scoped wait, and no workflow sets them yet) it first opens or reuses the `kind=capacity` tracker (`provider_outage.py open --provider claude-pool`, overridable with `CLAUDE_CAPACITY_WAIT_TRACKER_PY`) and posts `<!-- ai:claude-capacity-wait-item:v1 kind= item= workflow= run= role= -->` there, then posts `<!-- ai:claude-capacity-wait:v1 stage= source= run= role= -->` on the item and adds the `ai:claude-capacity-wait` label only when the tracker marker is posted or already present (a label without a tracker record would strand the item, because the resume pass finds waiting work through the tracker markers); both markers are skipped when the `GH_PAT` login already posted one for the same run (and item), and markers by other logins never count. One Telegram WARNING is sent only when the call opened the tracker, or when `provider_outage.py` could not read or create one (the alert then says the tracker is unavailable). A failed identity read records nothing; every failure warns and the step still exits 0. `provider_outage.py` uses "work is waiting, not falling back" capacity tracker and alert text only while the switch is on. `ai:claude-capacity-wait` sits right after `ai:needs-human` in `PHASE_LABELS_PRIORITY` and in `DEDICATED_HANDLER_PHASES`, so stall recovery leaves the item alone; the standalone stall loop in `scripts/orchestrate_poll_process.sh` skips the phase too; removing the label changes the detected phase and restarts the stall window. It is not in any `phase_groups` entry: the stage label stays. Phases 2-4 (issue #7370) handle exit 80 at every call site, pause review/autofix as `claude_capacity` and add the hourly capacity-resume pass, so the switch now defaults to on (see `agents.d/7370-claude-capacity-wait-phases-2-4.md`).
- **Claude capacity wait, Phases 2-4 (issue #7370).** `CLAUDE_POOL_CAPACITY_WAIT_ENABLED` is on by default (`scripts/ai_engine.sh`, `scripts/provider_outage.py`), and every Claude workflow sets it at workflow level to `vars.CLAUDE_POOL_CAPACITY_WAIT_ENABLED || (vars.WORKFLOW_PROFILE == 'core' && 'false') || 'true'`: the `core` profile has no orchestrate-poll wrapper to resume waiting work. `claude-engine-smoke.yml` pins it `'false'` to keep testing exit 75. Phase 2: exit 80 never runs codex. Plan (`run_plan_codex.sh` sets `SKIP_PLAN`), clarify, clarify-respond (no RECOMMENDED fallback), implement (`SKIP_IMPLEMENT`, back to `ai:awaiting-approval`), the orchestrate decomposer and validate (`validate_process.sh` exits 80, no verdict label) set `CLAUDE_CAPACITY_WAIT=true` and skip their later output steps. Utility roles (activation verify, heal intake, check triage, implement diagnose) log `reason=claude_capacity_wait` and stop. The unblock judge posts no verdict. `security_audit.sh` exits 80 and the security pass retries next tick (`SECURITY_PASS_STARTED ... outcome=deferred`). Poller judges defer with reason `claude_capacity_wait`, which never counts toward `JUDGE_ISOLATION_MAX_FAILURES` / `RB_JUDGE_ISOLATION_MAX_FAILURES`. Each waiting job's report step names its item (`CLAUDE_CAPACITY_WAIT_ITEM_KIND` / `_NUMBER` / `_STAGE` / `_SOURCE_COMMENT`); kind `none` (the poller) records the tracker only. The report step's `AI_ENGINE_FALLBACK_REPORT_WAITS_ONLY=true` (implement, review) ignores refused fallbacks, which those workflows report through their own heal reporters. Clarify's route step treats `/reclarify [auto-requeued-by-capacity-wait]` ending in `<!-- ai:claude-capacity-wait-replay:v1 source=<id|none> run=<id> -->` as an automated restart only when the GH_PAT login wrote it with `source=none`; a replay of a human `/reclarify` keeps human semantics. Phase 3: with every account gated, review/autofix's "Defer review on Claude capacity" step (`scripts/review_autofix_step_claude_capacity_wait.sh defer`) fails the run as `AUTOFIX_FAILURE_REASON=claude_capacity`, as do the editor, resolver, summariser and review-blocked judge (`review_rb_judge.sh`) on a mid-run 80, each without retry or failure alert. A panel slot that gets 80 is skipped as `capacity_wait` and stays fail-open; it does not set `AUTOFIX_FAILURE_REASON`, so a review the other slots finish is not labelled as waiting. Validation self-heal exits 80 like discover and diagnose. The four workflow-log-analysis passes and the consumer retro fan-out stop on 80 without codex or retries; those scheduled passes are not resumed and run again on their next trigger. The consolidator stays fail-open. `claude_capacity` is in `workflow_failure_heal.PAUSE_FAILURE_REASONS` (literal copy in `provider_outage.py`, pinned by a test): it counts toward neither the identical-failure cap nor the heal streak or re-arm, and no `ai:review-blocked` label, review-blocked comment, Telegram failure or heal report follows. The PR gets "AI review paused — Claude pool at capacity", and "Record Claude capacity wait" labels it `ai:claude-capacity-wait`. `review_autofix_sweep.yml` skips labelled PRs (`AUTOFIX_SWEEP_SKIP reason=claude_capacity_wait`). Phase 4: `orchestrate_poll.yml`'s `find_tracking` lists open `ai:provider-outage` trackers once per tick (`capacity_wait`). While a capacity tracker is open, the pool is probed only in the hourly window (`pool_window`, `CLAUDE_POOL_HEALTH_WINDOW_MINUTES`). Between windows the poller's Claude roles get `CLAUDE_POOL_REASON=all_gated` and wait. "Resume Claude capacity waits" runs `provider_outage.py capacity-resume`: it reads the tracker's `ai:claude-capacity-wait-item:v1` markers by the GH_PAT login, lists them for the pool-health alert (`CLAUDE_CAPACITY_WAIT_ITEMS`, "Claude pool exhausted: N item(s) waiting ...", up to 10 links), and once an account is usable resumes each one:
  - a PR is re-dispatched through the PR-named review wrapper when its current-head marker reason is `claude_capacity`;
  - an issue gets its source command replayed, with `[auto-requeued-by-capacity-wait]` and the replay marker, unless a newer trusted command or a replay for that run exists; the source must be on that issue from a trusted author;
  - a validate wait only loses its label;
  - a run is re-run with `actions/runs/<id>/rerun` (403/404/422 means `not_rerun`, named in the alert).

  The capacity-wait label listing is paginated. A PR or source comment that returns HTTP 404 counts as superseded. An issue's replay comment is posted before its label is removed, and any failed label removal leaves the item pending. A PR's label is removed before its review is dispatched, so a retry never queues a duplicate review, and an HTTP 404 on the label counts as removed; a failed dispatch puts the label back so the next pass retries. A failed capacity-wait label listing does not fail issue items. While items wait, the pool-health alert is sent in any tick where the resume pass ran, not only in the first minutes of the hour. The weekly/dispatched security audit (`security-audit.yml`) ends its step successfully on exit 80 and skips the single-issue report, so that pass stays pending until its marker goes stale; check-failure triage skips without filing and exports no job state. An open PR that no longer carries the label counts as superseded (an earlier pass or the review sweep already dispatched it). An issue missing from the label listing is still checked and replayed, because the reporter records the tracker marker before it adds the label; a replay marker for that run or a newer trusted command supersedes it. Each pass writes `<!-- ai:claude-capacity-wait-probe:v1 at=<iso> -->` to the tracker body, and `pool_window` also opens when that marker is missing or at least 55 minutes old (read from the tracker listing `find_tracking` already saved), so the pass runs about hourly even when no tick lands in the first minutes of the hour. Stall recovery leaves an issue alone while its open linked PR carries `ai:claude-capacity-wait` (`STALL_SKIP ... reason=claude_capacity_wait`, no recovery budget spent). The items file the pool-health alert reads is rewritten after the pass with only the items still waiting, and the "capacity back" notice is sent at DEBUG. The tracker's comments are read again before it closes, so a wait recorded during the pass keeps it open. `find_tracking` counts a capacity tracker only when its author is OWNER/MEMBER/COLLABORATOR. Progress is kept in the tracker's `<!-- ai:claude-capacity-wait-resume:v1 done=<kind:item:run,...> -->` line. The tracker closes (with one "capacity back" alert) only when nothing is pending, and `close --kind capacity` from review runs answers `pending_waits` until then. Capped at `PROVIDER_OUTAGE_RESUME_MAX_DISPATCH` (50) items per pass. Not resumed: project-mode activation verification that waited inside the poller (logged `outcome=skip reason=claude_capacity_wait`). While the tracking issue carries the label, `dispatch_validation_if_needed` holds without counting toward `MAX_VALIDATION_DISPATCHES_PER_CYCLE` (`VALIDATION_DISPATCH_CAP outcome=capacity_wait`, plan D7).

## Workflow file size limit

- Early warning: the `budget-watch` job of `ci.yml` (pushes to main only,
  `scripts/ci_budget_watch.py`) opens one `ai:ci-budget` issue when a
  workflow file reaches `CI_BUDGET_WORKFLOW_WARN_BYTES` (default 440,000)
  or a CI job's duration reaches `CI_BUDGET_JOB_WARN_RATIO` (default 0.75)
  of its `timeout-minutes`, deduplicated by
  `<!-- ai:ci-budget:v1 kind=<workflow_size|job_duration> subject=<s> -->`.
  The issue enters the normal clarify/plan/implement pipeline, so the split
  or rebalance lands before the hard guard or the timeout breaks CI. Logs
  `CI_BUDGET kind= subject= value= limit=` and
  `CI_BUDGET_ISSUE kind= subject= action=opened|exists` (plus
  `CI_BUDGET_LABEL name= action=created` when it creates the label); never fails.
- GitHub does not start runs for a workflow file over **512,000 bytes**
  (500 KiB). Measured on 2026-09-24 with padded probe workflows: 512,000
  bytes ran, 512,001 did not. Nothing reports an error. Every push instead
  creates a zero-job run named after the file path (for example
  `.github/workflows/review_autofix.yml`) that concludes `failure` with
  "This run likely failed because of a workflow file issue", and a reusable
  workflow over the limit cannot be called.
- Incident: #4327 grew `review_autofix.yml` from 505,283 to 540,537 bytes.
  The phantom runs matched the `test-and-mark-stable.yml` Phase 4 review-run
  regex on the pinned head SHA, Phase 4 accepted one as "completed with
  failure", and Phase 4b failed with `retry_timeout` (stable gate run
  35903885958). Phase 4 now drops runs whose `name` equals their `path`,
  which only happens when GitHub cannot load the workflow, since every
  workflow here and in `workflow-templates/` sets `name:`.
- Guard: `tests/test_workflow_file_size_limit.py` (CI step "Review autofix
  step-script contract and workflow file size guard tests") fails when any
  `.github/workflows/*.yml` reaches **480,000 bytes**, 32,000 bytes before
  the hard limit.
- **Split rule, for interactive sessions and the unattended pipelines alike**
  (also `CLAUDE.md` §27 and `unattended_system_instructions.md` §24):
  when a change leaves a workflow file at or above 480,000 bytes, move the
  largest inline `run:` bodies into `scripts/` **in the same PR** until the
  file is well under the guard (aim for 50,000+ bytes of headroom). Never
  raise the guard threshold and never split a workflow into a second
  workflow file to get under it. For `review_autofix.yml`, follow the
  existing pattern:
  - Move the body verbatim to a new `review_autofix_step_<slug>.sh` under `scripts/` with
    the standard comment header (shebang plus a comment naming the step),
    mode `0755`. The body must contain no `${{ }}` expression: GitHub only
    substitutes those inside the workflow file, so pass the values through
    the step's `env:` first.
  - Keep the step's `name:`, `id:`, `if:`, `env:` and `continue-on-error:`
    in the workflow (§6). Replace `run:` with the resolving wrapper, which
    tries `${SUPPORT_SCRIPTS_DIR}`, then
    `${GITHUB_WORKSPACE}/.codex-workflow-src/scripts`, then
    `${GITHUB_WORKSPACE}/.codex-workflow-src-main/scripts`, and `source`s
    the script in the step shell. A missing script fails the step with
    `::error::`; only `always()` steps, which also run after support staging
    failed, skip with `::warning::` instead.
  - Add the script to `REQUIRED_BOOTSTRAP_SCRIPTS` in
    `scripts/stage_workflow_support.sh`, to the `REVIEW_AUTOFIX_STEP_SCRIPTS`
    registry in `tests/review_autofix_step_scripts.py`, and to
    `docs/INVENTORY.md`.
  - Contract tests keep reading the step bodies through
    `expanded_review_autofix_text()`, which inlines each script again, so
    their assertions do not change.
  - For other workflows, move the body to a `scripts/` file that the job
    already stages or checks out, and invoke it the same way.
- The `.codex-workflow-src` fallback is the verified workflow-commit checkout;
  a missing required script never falls back to a PR-head or moving-ref copy.

- Steps in the `gate` job of `review_autofix.yml` run before support
  staging, so their wrapper has no `SUPPORT_SCRIPTS_DIR` or
  `.codex-workflow-src` fallback. Add the script to the SHA-pinned
  "Checkout head-gate helper" sparse checkout and to the file check in
  "Verify head-gate helper identity". Resolve only
  `${GITHUB_WORKSPACE}/.codex-head-gate-src/scripts/<script>`, and require
  `HEAD_GATE_HELPER_VERIFIED=true`, failing the step otherwise.
  `REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS` in
  `tests/review_autofix_step_scripts.py` lists these scripts. The first is
  the "Evaluate review gate" body (`review_autofix_step_evaluate_gate.sh`,
  #6972).

## Test-suite git environment isolation

- `tests/conftest.py` strips the repo-pinning git variables (`GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`) from `os.environ` for the whole pytest
  session and restores them afterwards. Every git subprocess a test spawns
  therefore resolves its repository from `cwd`, or from variables the test
  sets itself.
- Why: implement.yml, review_autofix.yml, and validate.yml export `GIT_DIR` /
  `GIT_WORK_TREE` into `$GITHUB_ENV` ("Activate workspace shell context"), and
  the codex editor inherits them when it runs `pytest`. A scratch-repo test
  that only set `cwd` was rebound to the live checkout: during the implement
  run for issue #4092, `tests/test_assemble_changelog.py` produced commit
  `37c72a5` ("base", author `test <test@example.invalid>`) on `ai/issue-4092`,
  reverting 1,654 lines of runtime helpers, and PR #4093's review editor then
  failed with `model_provider_broker_start: command not found` (run
  34982425230).
- Per-test scrubs that also drop `BASH_ENV`, `ENV`, or `WORKSPACE_PATH` stay
  as they are; the conftest fixture is the floor, not a replacement.
  `tests/test_pytest_git_env_isolation.py` pins the contract with a nested
  pytest run against a sentinel repository.
- The same fixture strips the self-repo staged-support ledger paths that
  implement.yml's "Stage workflow support files" step exports into
  `$GITHUB_ENV` (`STAGED_SUPPORT_LEDGER`, `STAGED_SUPPORT_BASE_DIR`,
  `STAGED_SUPPORT_EDITOR_HEAD_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`;
  `STAGED_SUPPORT_RUNTIME_ENV_VARS` in `tests/conftest.py`).
  `scripts/implement_staged_support_workspace.sh` and
  `scripts/implement_commit_changes.sh` read `STAGED_SUPPORT_EDITOR_HEAD_LEDGER`
  from the environment before their ledger-relative default, so a test that
  copies `os.environ` and only overrides `STAGED_SUPPORT_LEDGER` /
  `STAGED_SUPPORT_BASE_DIR` writes its fixture paths into the live run's
  editor-head ledger. Incident: implement runs 35614385686, 35628923735,
  35642366131 and 35656715219 (issues #4227 / #4242, project #4139) each
  finished the editor with a complete change set, then the editor's own
  pytest run of `tests/test_implement_post_codex_recovery.py` appended a
  fixture-only `scripts/` + `helper.sh` path to
  `/tmp/codex-implement-<run>/staged_support_editor_head.txt`, and the
  post-editor `reinstall` failed closed with
  `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING` for that path. The stall
  poller retried twice, the stall judge re-issued #4227 as #4242, and the
  replacement failed the same way. The second nested run in
  `tests/test_pytest_git_env_isolation.py` pins this contract against a
  sentinel ledger. The conftest strip only protects checkouts that carry it: #4242's
  runs 35656715219 / 35668070395 checked out `orchestrator/project-4139`
  and the preserved `ai/reissue-baseline/pr-4174-*` head predates it, so
  `implement.yml` also drops the four variables from the editor's
  environment at both launch sites (see "Implement self-repo staged-support
  ledger").

## Models in use (defaults; overridable via repo-vars)

| Phase | Default model | Default reasoning | Verbosity | Engine · Claude role |
|---|---|---|---|---|
| clarify, clarify-respond | `openai/gpt-6-sol` | `high` (smoke: `low` — `clarify.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` | Claude (Opus 5.5; codex fallback) · `CLARIFY`, `CLARIFY_RESPOND` |
| plan | `openai/gpt-6-sol` | `high` (smoke: `low` — `plan.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` | Claude (Opus 5.5; codex fallback) · `PLAN` |
| orchestrate (decompose), judge | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback for `ORCHESTRATE`, isolated OpenCode fallback for poller judges) · `ORCHESTRATE`, `WAVE_JUDGE`, `STALL_JUDGE`, `INTEGRATION_JUDGE`, `SECURITY_JUDGE` |
| implement (main editor) | `openai/gpt-6-sol` | `high` (smoke: no override — see `.github/workflows/implement.yml:597-606`) | `low` | Claude (Opus 5.5; codex fallback) · `IMPLEMENT` |
| implement-repair, implement-repair-syntax | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback) · `IMPLEMENT_REPAIR` |
| implement-diagnose | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback) · `IMPLEMENT_DIAGNOSE` |
| review autofix editor | `openai/gpt-6-sol` | `high` (smoke: `medium`) | `low` | Claude (Opus 5.5; OpenCode fallback; `CLAUDE_FIXER_ENABLED=false` keeps OpenCode) · `REVIEW_EDITOR` |
| review autofix reviewers (pass 1) | `REVIEWER_MODELS` (default roster: `minimax/minimax-m3`, `anthropic/claude-sonnet-5.5`, `z-ai/glm-5.3-flash`, `google/gemini-3.1-flash-lite`, `qwen/qwen3.7-plus`, `openai/gpt-6-luna`) | `xhigh` per reviewer call (hardcoded at the `run_reviewer_pass ... "xhigh"` callsite in `scripts/review_run_reviewers.sh:4733`; not affected by the smoke `REVIEWER_REASONING_EFFORT=low` override in two-pass mode) | `low` | OpenCode for five slots; the `anthropic/claude-sonnet-5.5` slot runs on the Claude account pool (Sonnet 5.5, one Haiku 5.5 retry, skipped when the pool is unavailable) · `PANEL_REVIEWER` |
| review autofix reviewers (pass 2) | `REVIEWER_MODELS` (same roster, after pass-2 scope / tier filtering) | `high` on diffs below `REVIEWER_PASS2_DIFF_LARGE_LOC=200`, `xhigh` at or above that threshold; smoke: `low`; operator override wins | `low` | Same as pass 1 · `PANEL_REVIEWER` for the Claude slot |
| review consolidator | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; OpenCode fallback; `CLAUDE_FIXER_ENABLED=false` keeps OpenCode) · `REVIEW_CONSOLIDATOR` |
| conflict resolver | `openai/gpt-6-sol` | `high` (decoupled from smoke; `scripts/review_conflict_resolve.sh` validates `xhigh`, `high`, `medium`, `none` only — `low` is rejected; default lowered from `xhigh` after runs `25627236793` / `25627316961` hit `timeout`-killed retries on degenerate orchestrator-stack integrations; override per-repo via `vars.THINKING_LEVEL_CONFLICT_RESOLVER`) | `low` | Claude (Opus 5.5; OpenCode fallback; `CLAUDE_FIXER_ENABLED=false` keeps OpenCode) · `CONFLICT_RESOLVER` |
| security audit (weekly / dispatch, orchestrator security pass) | `openai/gpt-6-sol` on codex | `xhigh` on codex; `high` on Claude | `low` | Claude (Opus 5.5 at `high`; codex `gpt-6-sol` fallback on any Claude failure, including every account at the 95% usage gate) · `SECURITY_AUDIT` |
| validate generate, diagnose | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback) · `VALIDATE` |
| validate discover | `openai/gpt-6-sol` | `high` (per-phase override via `MODEL_REASONING_EFFORT_DISCOVER`) | `low` | Claude (Opus 5.5; codex fallback) · `VALIDATE` |
| validate fix-harness, self-heal | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback) · `VALIDATE_SELF_HEAL` |
| workflow log analyze | `openai/gpt-6-sol` | `xhigh` | `low` | Claude (Opus 5.5; codex fallback) · `LOG_ANALYSIS` |
| workflow audit | `openai/gpt-6-sol` | `xhigh` (hardcoded in `.github/workflows/workflow-log-analysis.yml:716-717`) | `low` | Claude (Opus 5.5; codex fallback) · `LOG_AUDIT` |
| workflow api-redundancy | `openai/gpt-6-sol` | `high` (default of `THINKING_LEVEL_ANALYSIS`) | `low` | Claude (Opus 5.5; codex fallback) · `LOG_ANALYSIS` |
| workflow log summary | `openai/gpt-6-luna` | default | `low` | Claude (Sonnet 5.5; OpenRouter fallback) · `LOG_SUMMARY` |
| reviewer consensus summariser | `openai/gpt-6-luna` | `medium` (`XPOLL_SUMMARISER_REASONING`) | `low` | Claude (Sonnet 5.5; sandboxed OpenCode fallback) · `SUMMARISER` |

The **Engine · Claude role** column names today's engine and the role name
`scripts/ai_engine.sh` resolves for that row (README "Claude engine").
Every role's default in `.github/ai/claude_engine.json` is `codex`
until its cutover (Phase 5a moved `CLARIFY`, `CLARIFY_RESPOND` and `PLAN`
to `claude`, Phase 5b `IMPLEMENT`, `IMPLEMENT_REPAIR` and `IMPLEMENT_DIAGNOSE`, Phase 5c `ORCHESTRATE`, the four orchestrator judges and the review write roles `REVIEW_EDITOR`, `REVIEW_CONSOLIDATOR`, `CONFLICT_RESOLVER` and `RB_JUDGE`, the security audit moved `SECURITY_AUDIT`, and plan item 3c moved `WORKFLOW_HEAL`, `CHECK_TRIAGE`, `ACTIVATION_VERIFY` and `UNBLOCK_JUDGE`; a missing config file still means codex for every role);
`AI_ENGINE_<ROLE>`, `AI_ENGINE` or the `ai:engine-claude`
/ `ai:codex` labels select it per run. On Claude a role uses its existing
model variable only when that value starts with `claude-`, else Opus 5.5
(`claude-opus-5-5`), or Sonnet 5.5 (`claude-sonnet-5-5`) for `LOG_SUMMARY`,
`RETRO`, `MATERIALITY`, `SUMMARISER` and `BEHAVIOURAL_SMOKE`; the reasoning
column is the effort (`none` / `minimal` → `low`). Apart from the
`anthropic/claude-sonnet-5.5` slot (`PANEL_REVIEWER`), the reviewer rows have no
engine switch. When Claude is unavailable (exit 75, `AI_ENGINE_FALLBACK`),
poller judges retry OpenCode in a fresh isolated sandbox; other roles use
their codex/OpenCode fallback. The pinned CLI is `@anthropic-ai/claude-code`
`cli_version` from the same file,
installed by `.github/actions/install-claude`.
The write profile has no `WebFetch`/`WebSearch`, and the host relay
(`scripts/claude_anthropic_relay.py`) accepts only untyped or `custom`
client tools, rejecting unknown provider-side tool types as well as known
web, code-execution and MCP-connector tools. A missing
`codex_isolated_exec.sh` makes `claude_run` fall back to codex rather than
running Claude with host credentials.

OpenCode version `1.18.23` is installed by the dispatch-only
`.github/workflows/opencode-live-smoke.yml` rollout gate and by production
`review_autofix.yml`, which warms the models.dev cache before the complete
review model pipeline runs. Reviewer, summariser, interim-judge,
behavioural-smoke synthesiser, editor, review-blocked judge/fix, consolidator,
and conflict-resolver calls use isolated OpenCode configurations. OpenCode
failures do not fall back to Codex; compatibility
`CODEX_*` identifiers and watchdog helper names remain unchanged. Other
production phases remain Codex-backed until their cutovers land.

<!-- Historical Phase 1 rollout snapshot retained for integration fingerprint verification:
OpenCode Phase 1 is operationally inert: version `1.18.23` is installed only
by the dispatch-only `.github/workflows/opencode-live-smoke.yml` rollout gate.
All production phases, including review/autofix, remain Codex-backed until a
later cutover phase lands after the all-slug live smoke succeeds.
-->

All gpt-6-sol phases now resolve to `low` verbosity at every layer: the per-phase
`MODEL_VERBOSITY` env-var default in `.github/workflows/*.yml` (`VERBOSITY_*`
repo-vars), the `-c model_verbosity=low` CLI flag on every `codex exec`
callsite (≈20 sites across `scripts/*.sh` and `.github/workflows/*.yml`),
the `model_verbosity = "low"` line that `scripts/write_codex_config.sh:242`
writes into `config.toml`, and the `"default_verbosity": "low"` for
`openai/gpt-6-sol` in `scripts/codex_model_catalog.json`. Third-party
reviewer models (`minimax/minimax-m3`, `z-ai/glm-5.3-flash`,
`google/gemini-3.1-flash-lite`, `qwen/qwen3.7-plus`) carry
`support_verbosity = false` in the catalog (the `anthropic/claude-sonnet-5.5`
slot runs on the Claude account pool, not codex/OpenCode)
(the `openai/gpt-6-luna` reviewer slot supports it, with catalog default
`low`) — codex CLI logs
`model_verbosity is set but ignored as the model does not support verbosity`
and continues; the value is operationally moot for those rows. The
historical `high` value across every layer was a workaround for the
openai/codex#11151 announce-without-emit failure mode (implement /
review_autofix smoke runs at 2026-05-07 12:41 / 12:42, where the model
emitted a reasoning trace and exited without a tool call); the workaround
now relies on `include_apply_patch_tool = true` as the primary
belt-and-suspenders. If the announce-without-emit pattern recurs at `low`,
raise verbosity at the layer that needs it (start with the editor /
implement callsites, since those are the original 11151 reproducers).

Every editor / consolidator / resolver phase now defaults to `openai/gpt-6-sol`.
Reviewer fan-out remains driven by the `REVIEWER_MODELS` roster in
`.github/workflows/review_autofix.yml` (currently the models listed in the
table above). The previous legacy editor split (patch-heavy
phases on a separate older slug) was retired after the announce-without-emit
regression (openai/codex#11151) drove repeat no-edit failures. The
2026-05-07 ablation suite then identified the underlying root cause as
`apply_patch_tool_type: "freeform"` on the OpenRouter Responses path (see
the `openai/gpt-5.4` catalog entry — `apply_patch_tool_type` is now
`function`).

The reviewer-only multi-model run (claude-branch-review) uses the same
reviewer models (`minimax/minimax-m3`, `anthropic/claude-sonnet-5.5`,
`z-ai/glm-5.3-flash`, `google/gemini-3.1-flash-lite`,
`qwen/qwen3.7-plus`, `openai/gpt-6-luna`) plus
`unattended_system_instructions.md` as system context. Nothing triggers it
by default any more: PR #6438 removed the `claude/**` push trigger from
`internal-review.yml`, so a `claude/**` branch is reviewed once it has a
PR, like every other branch. The mode stays in `review_autofix.yml` behind
the `force_claude_branch_review` input.

---

## Interactive slash-command model selection

**Interactive Claude Code sessions only.** This section is unrelated to
`## Models in use` above: that table covers the unattended codex/OpenCode
pipeline models, which read `unattended_system_instructions.md` and are
driven by repo-vars.

The 13 commands in `.claude/commands/*.md` deliberately carry **no**
`model:` frontmatter, and no `context:` / `background:` keys either. Every
`/command` runs on the model the operator picked for the session (via
`/model` or the session's configured model), for every turn of the command.
Do not re-add per-command `model:` pins: they were tried (PRs #3967 and
#3971) and removed because the operator's session choice should decide the
model, not the command file. A file that starts with `---` would be parsed
as frontmatter, so the command body must remain the first line.

`/implement-plan-claude` and `/implement-issue-claude` are thin hand-offs
that run in the operator's session on the model the operator picked, like
every other command: `/implement-plan-claude` dispatches the AI orchestrator
(the `/implement-plan-ai` procedure) and marks the project `ai:engine-claude`,
and `/implement-issue-claude` labels one standalone issue `ai:engine-claude`
and posts `/reclarify`. Neither implements anything in the session, opens a
PR, or starts other sessions. The session-driven chain they used to run
(stage sessions, the Sonnet checker, hand-back Routines, the Claude issue
pickup and its session sweep, auto-decisions, the `docs/implement-plan/`
logs) was retired on 2026-10-03 together with CLAUDE.md §26 / §28; see
`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`.

No field here changes what any consumer repo receives on the `@stable`
sync: `.claude/commands/` is not part of the synced surface, and the
template copies under `workflow-templates/.claude/commands/` have never
carried frontmatter.

### Live `.claude/` copies and their templates

This repo runs its own `.claude/` copy of the files it ships to consumers
under `workflow-templates/.claude/`. The pipeline's editors cannot edit
`.claude/**`, so an AI fix that changes only the template leaves the live copy
behind; #6133 (the merged-PR guard hook) and #6176 (four command files) broke
`main` that way. Three pieces keep the pairs in step:

- `tests/test_claude_template_live_parity.py` (own `ci.yml` step) fails when a
  template and its live copy differ in content or executable bits, unless the
  file is listed in `.github/ai/claude_template_divergence.json` with a reason.
  Six command files are listed today; both copies of those are edited by hand.
- `scripts/sync_claude_live_copies.py` runs from
  `.github/workflows/sync-claude-live-copies.yml` on every push to `main` that
  touches `workflow-templates/.claude/**`. When the push changed a template
  but not its live copy, it copies the template over, pushes
  `ai/sync-claude-live-copies` and opens a PR (or refreshes the open one). It
  also carries forward still-drifted live copies from the existing sync branch
  only when that branch's copy matches the current template; a live copy
  changed on `main` since the earlier sync is left for the parity test.
  The auto-merge-eligible branch accepts a path only when every template
  commit since the last live-copy edit is associated with a merged PR from a
  non-`ai/*`, non-`orchestrator/*`, non-`auto/*` branch targeting the sync base
  from a same-repository head, authored by an OWNER/MEMBER/COLLABORATOR
  non-bot account, and neither its subject nor any PR commit subject carries
  a pipeline marker or a squash `(#N)` suffix. A trusted collaborator's
  account or session can still submit AI-written content.
  Unverifiable paths instead go to `ai/sync-claude-live-copies-held` as a draft
  PR for human review, with a Telegram WARNING listing their source commits.
  Malformed merge timestamps also fail authorization and hold the path.
  A ready held PR is converted back to draft before a refresh pushes content.
  Logs add `CLAUDE_LIVE_SYNC authorized`, `held`, and `converted_to_draft`;
  the history cap defaults to 30 distinct commits per run
  (`CLAUDE_LIVE_SYNC_MAX_PROVENANCE_COMMITS`).
  An existing sync branch is never force-pushed (the ruleset refuses
  non-fast-forward pushes): the refresh commit's parents are the old tip and
  the pushed `main` commit (`CLAUDE_LIVE_SYNC fast_forward`). Pushes stay
  lease-checked; push or PR API failures fail the job with a structured
  `CLAUDE_LIVE_SYNC error` line (git's error text in `detail=` for a push),
  leaving the branch for a later sync attempt.
  Its existing-PR lookup accepts only an open PR from this repository's sync
  branch into the configured base branch, even when other PRs are returned.
  It fails open on an unusable `before` commit: no sync is attempted, and
  the parity test still reports the drift. Provenance and open-PR reads use
  GraphQL: one aliased association lookup covers up to 30 distinct template
  commits, followed by 1-3 pages per distinct merged PR and one sync-PR lookup
  per nonempty group.
  Only PR creation uses REST, at most once for each of the authorized and held
  groups (two REST calls total). `GH_PAT` remains broad to trigger CI/review;
  branch names and commit subjects are not proof of human authorship, and
  marking a held PR ready between conversion and push is a residual race. Log
  prefix `CLAUDE_LIVE_SYNC`.
- A push that changed only the live copy is left to the parity test.

For PRs targeting `main`, the `tests-hooks-and-orchestrator` CI job runs
`sync_claude_live_copies.py sync --dry-run` in its disposable checkout,
using the PR base SHA and full git history. This prepares only eligible
template-only command changes for the tests without committing or pushing live
files. Security hooks under `.claude/hooks/**` and `.claude/settings.json` are
never prepared: their committed live copies must match the templates for CI
to pass, even when the editor cannot write the live file.
Changes to both halves that still differ remain test failures. Push CI and
PRs targeting `stable` check the committed tree without preparation, so drift
on `main` is still reported while the post-merge sync PR is pending.
Both release gates' `validate-scripts` jobs also run
`tests/test_claude_template_live_parity.py` on the committed tree: drift,
including a pending or held sync PR, blocks the `stable` release until both
copies match or the file is allowlisted.

New templates without a live copy are also treated as drift and copied into
`.claude/` (including new subdirectories) with their executable permissions.
Template symlinks (including directory links) and symlinks anywhere in a live
destination path fail the sync/parity check rather than being followed; the
workflow must not copy checkout-local credential files into a PR.
A later template push recovers an earlier failed or superseded sync only if
the template's most recent change is newer than the live file's; an equal or
newer live edit is left untouched.
An unusable `before` commit still skips the sync, so CI reports any drift.

CI also runs on pull requests into, and pushes to, `orchestrator/project-*`
integration branches. The template-only live-copy preparation runs for those
PRs (`--before` = PR base SHA) and pushes (`--before` = the push's `before`).
When that value is empty, all zeros (branch creation) or unreachable (force
push), the step logs a `::warning::` and uses the merge-base of `HEAD` with
`origin/main`, because `sync_claude_live_copies.py` skips drift recovery
without a reachable `--before`. Integration-branch pushes are not healed by
the workflow failure heal intake, and `budget-watch` runs on `main` pushes only.

---

## Repo-specific batching helpers

The following helpers are the canonical batched GraphQL paths for the
GitHub API hygiene rules in `unattended_system_instructions.md` §14:

- `_fetch_candidate_issue_details_graphql` (in `scripts/orchestrate_poll_process.sh`)
- `_fetch_linked_pr_status_graphql` (in `scripts/orchestrate_poll_process.sh`)

BATCH_HELPER.name=_fetch_candidate_issue_details_graphql kind=graphql-batch path=scripts/orchestrate_poll_process.sh cache=_candidate_details_json
BATCH_HELPER.name=_fetch_linked_pr_status_graphql kind=graphql-batch path=scripts/orchestrate_poll_process.sh cache=STALL_MANAGED_LINKED_PR_CACHE

Both return a dict keyed by issue number so the caller can drop the result
into a cycle-local cache.

Cycle-local caches that must not be re-fetched per iteration:
`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`,
`_candidate_details_json`.

## Workflow install profiles

PROFILE.default=full
PROFILE.name=core manifest=workflow-templates/profiles/core.txt wrappers=ai-clarify.yml,ai-plan.yml,ai-implement.yml,ai-review.yml,ai-issue-pr-status.yml,ai-cancel-on-pr-close.yml,ai-orchestrate-clarify-respond.yml
PROFILE.name=standard manifest=workflow-templates/profiles/standard.txt wrappers=ai-clarify.yml,ai-plan.yml,ai-implement.yml,ai-review.yml,ai-issue-pr-status.yml,ai-cancel-on-pr-close.yml,ai-orchestrate.yml,ai-orchestrate-poll.yml,ai-orchestrate-clarify-respond.yml,ai-validate.yml,ai-sync-labels.yml,review_rb_judge_dispatch.yml,unblock_judge_dispatch.yml
PROFILE.name=full manifest=workflow-templates/profiles/full.txt wrappers=ai-cancel-on-pr-close.yml,ai-check-failure-triage.yml,ai-clarify.yml,ai-implement.yml,ai-issue-pr-status.yml,ai-memory-maintenance.yml,ai-orchestrate-clarify-respond.yml,ai-orchestrate-poll.yml,ai-orchestrate.yml,ai-plan.yml,ai-review.yml,ai-security-audit.yml,ai-sync-labels.yml,ai-update-workflows.yml,ai-validate.yml,ai-workflow-failure-heal.yml,review_rb_judge_dispatch.yml,unblock_judge_dispatch.yml

## Immutable consumer wrapper pins

- Canonical `workflow-templates/*.yml` files retain `@stable` as a delivery-time
  render token. Installed consumer wrappers must use
  `@<40-character-release-sha> # stable` instead.
- `scripts/workflow_wrapper_refs.py` is the single renderer used by the updater,
  drift audit, and `/seed-repo`; do not duplicate its substitution rules.
- `.github/workflows/update_workflows.yml` renders every top-level wrapper before
  any consumer mutation, updates an existing `ai-update-workflows.yml` regardless
  of profile, and never creates that self-updater when absent.
- Its `Remove retired upstream files` step reads
  `workflow-templates/retired_files.txt` (`.claude/` files upstream no longer
  ships) and deletes a consumer copy only when it is byte-identical to a
  released version (log prefix `RETIRED_FILE_REMOVED`); a locally modified copy
  is kept (`RETIRED_FILE_KEPT_MODIFIED`).
- Attested release manifest (#6943). The `release` job of `mark-stable.yml`
  and `test-and-mark-stable.yml` runs `scripts/release_manifest_publish.sh build`
  after the version tag is pushed: it checks that the tag peels to the release
  SHA, checks that commit out into a detached worktree and runs that tree's own
  `scripts/release_manifest.py build`, writing `release-manifest.v1.json`
  (`RELEASE_MANIFEST_ASSET_NAME`, default set in the helper; schema
  `ai-memory/schemas/release_manifest.v1.json`). The job attests the file with
  `actions/attest-build-provenance@v3` (only that job holds `id-token: write`
  and `attestations: write`), and `upload` adds it to the GitHub Release
  (`--clobber`; the manifest is deterministic per tag) and checks the asset
  landed. Any failure prints `::error::RELEASE_MANIFEST outcome=failed
  reason=<token>` and stops the release before "Notify consumer repos via
  repository_dispatch". Log prefix `RELEASE_MANIFEST`; tests
  `tests/test_release_manifest.py` and
  `tests/test_release_manifest_workflow_contract.py`.
- Fail-closed verification (#6957). The `Verify attested release manifest`
  step (`id: verify_release_manifest`) of `update_workflows.yml` acts only when
  `UPDATER_VERIFY_RELEASE_MANIFEST=true` (default `true`; set to `false` it logs
  `UPDATER_MANIFEST_VERIFY outcome=skip reason=disabled` and makes no API
  call). It resolves the `vX.Y.Z` release whose tag points at the `stable`
  commit, downloads the asset, runs `gh attestation verify` with a
  `--cert-identity-regex` limited to this repo's `mark-stable.yml` /
  `test-and-mark-stable.yml` at `refs/heads/stable`, hash-checks the
  verifier modules against the manifest (`verifier_unattested` for releases
  whose manifest predates them, `verifier_hash_mismatch`), and runs
  `scripts/verify_release_manifest.py verify` on every file the updater reads
  (the `verify-pr-tree` subcommand serves the `verify` job below). A rejection
  logs `UPDATER_MANIFEST_VERIFY outcome=rejected reason=<token>
  stage=<input|tag|release|download|attestation|header|bootstrap|paths|verify>`
  and stops the run before any consumer file is written; the summary shows
  `ERR_RELEASE_MANIFEST_VERIFY_FAILED`. Verifier reasons include
  `schema_invalid`, `repository_mismatch`, `sha_mismatch`, `unlisted_path`,
  `missing_file`, `hash_mismatch`, `mode_mismatch`, `size_mismatch`,
  `symlink_mismatch`, `read_failed` and `internal_error`. The step logic stays
  inline because in consumer runs the only `scripts/` copy is the release tree
  under verification. Tests `tests/test_verify_release_manifest.py` and
  `tests/test_update_workflows_manifest_verify.py`.
- With `UPDATER_PR_DELIVERY_ENABLED=true` (default `true`) its `Commit and push
  updates` step (`id: commit_push`) never pushes to the default branch: it
  commits to `auto/update-workflows-<release-sha-12>` (never `ai/issue-*`, so
  the merge train ignores it) with a `Updater-Release-SHA:` trailer, pushes
  with `--force-with-lease`, fails closed on commits it did not write (an
  existing branch is replaced only when the repository activity API shows
  every push to it came from the `GH_PAT` account and the newest produced its
  tip; committer email and trailer alone are forgeable), lints
  the PR text with `scripts/lint_pr_body_auto_close.py` (now part of the
  attested release manifest) and opens or refreshes one PR; for a
  `verified=true` release it records `auto_merge=pending_verify`. PR calls use
  `GH_PAT`; no job requests a `pull-requests` permission because a reusable
  job cannot raise the caller's grant. Log prefix `UPDATER_PR_DELIVERY`.
- The separate `verify` job (#7004; job id `verify`, status context
  `ai-update-workflows/verify` and log prefix `UPDATER_PR_VERIFY` as shipped in
  `update_workflows.yml`) runs after a PR was opened
  or refreshed. It checks the PR head out without credentials and never runs
  code from it, re-attests the release manifest, hash-checks every release
  module it runs, checks that the PR adds one commit with the
  `Updater-Release-SHA:` trailer on top of the default branch, and runs
  `scripts/verify_release_manifest.py verify-pr-tree`, which accepts a changed
  path only when it matches the attested release (re-rendered wrapper,
  manifest hash, attested retired-file hash, or the audit-gate/changelog
  output the attested scripts reproduce on the PR's parent). It posts the
  commit status `ai-update-workflows/verify` with the job token and is the
  only place that enables auto-merge (bound to the verified head, `verified=true`
  releases only). It has no job-level `permissions:` (a reusable job asking for
  more than the caller grants fails the whole run at startup); the consumer
  template grants `contents`, `pull-requests` and `statuses: write`, and
  `tests/test_update_workflows_template_gate.py` pins that contract. Log prefix
  `UPDATER_PR_VERIFY`.
- Stable-release repository dispatch payloads carry both `version` and the peeled
  commit `sha`. Consumers validate the payload but independently resolve current
  `stable`, so delayed events cannot downgrade installed pins.

- Release tagging switch defaults on (issue #7272, from #7029). In the
  `release` jobs of `mark-stable.yml` and `test-and-mark-stable.yml`,
  `RELEASE_TAGGING_ENABLED` resolves as `vars.RELEASE_TAGGING_ENABLED || 'true'`
  and `${RELEASE_TAGGING_ENABLED-true}`: unset, empty or `true` (any letter
  case) publishes, and any other value skips the changelog commit, tag pushes,
  release manifest build/attestation/upload, GitHub Release and consumer
  dispatch with a `::warning::`. This is intentional. The safety control is
  the fail-closed `CI / lint` check-run gate (`scripts/release_ci_gate.sh`)
  in `validate` and `release`; the switch is an operator's emergency off
  switch, and defaulting it off would only stop releases. Activation
  verification should not report the default-on fallback as a gap. To pause
  releases, set `RELEASE_TAGGING_ENABLED=false` and
  `AUTO_RELEASE_STABLE_ENABLED=false`.

- Updater safeguards default on. `UPDATER_VERIFY_RELEASE_MANIFEST` and
  `UPDATER_PR_DELIVERY_ENABLED` resolve as `vars.<NAME> || 'true'` and
  `${<NAME>:-true}` in `update_workflows.yml`: unset, empty or `true` (any
  letter case) is on, `false` is off, and any other value is off with a
  `::warning::`. The `verify` job posts every `ai-update-workflows/verify`
  status (the pending post in "Validate verify inputs and mark pending", the
  success post in "Publish verify result and enable auto-merge" and the post
  in "Mark verify failure") with `github.token` first. When that is refused,
  because the consumer's wrapper predates `statuses: write`, it retries once
  with `GH_PAT` and logs `::warning::UPDATER_PR_VERIFY posted the status with
  GH_PAT ...` (the failure post only warns), so the update PR that fixes the
  wrapper still auto-merges. Only when both tokens are refused do the pending
  and success posts log `reason=status_post_failed` and fail the job.

- Stable release switch defaults on (issue #7298, from #7029 / #7272). In the
  `release` jobs of `mark-stable.yml` and `test-and-mark-stable.yml` (step
  `Resolve stable release switch`), `STABLE_RELEASE_ENABLED` resolves as
  `vars.STABLE_RELEASE_ENABLED || 'true'` and `${STABLE_RELEASE_ENABLED:-true}`:
  unset, empty or `true` (any letter case) publishes, `false` turns publishing
  off, and any other value also turns it off with a `::warning::`. While off,
  the validation jobs and the CI lint gate still run; the changelog push, tags,
  GitHub Release, consumer dispatch and `sync-to-main` are skipped, and the job
  logs `STABLE_RELEASE_SWITCH enabled=false` and `STABLE_RELEASE_DISABLED`.
  This is intentional. The safety control is the fail-closed `CI / lint`
  check-run gate (`scripts/release_ci_gate.sh`, step `Require successful CI
  lint check-run`), which runs whatever the switch says; the switch is an
  operator's emergency off switch, and defaulting it off would only stop
  releases, as it did before #7029. Activation verification should not report
  the default-on fallback of `STABLE_RELEASE_ENABLED` or
  `RELEASE_TAGGING_ENABLED` as a gap (the verification of PR #7207 filed #7298
  for it); this applies to those two switches only, not to other default-on
  gates. To pause releases, set `STABLE_RELEASE_ENABLED=false` (or
  `RELEASE_TAGGING_ENABLED=false`) and `AUTO_RELEASE_STABLE_ENABLED=false`.

---

## Optional `.github/ai` operator surfaces

The Symphony closeout left three consumer-authored config surfaces on current
HEAD. Each fails open when its file is missing; a consumer enables one by
committing the corresponding file:

- `.github/ai/WORKFLOW.md` — loaded by `scripts/load_workflow_overlay.py`
  and validated by `ai-memory/schemas/workflow_overlay.v1.json`. The shipped
  schema is intentionally narrow: `schema_version` plus `prompt_overrides[]`
  append/replace entries only. This repository ships a no-op overlay
  (`schema_version` only, no `prompt_overrides`), so `WORKFLOW_OVERLAY_ENABLED`
  is `true` but no rendered prompt is altered until override entries are added.
  Review/autofix and validate stage it and its fragments from one fetched,
  pinned default-branch commit, not from the PR/integration checkout; a fetch
  failure disables the overlay instead of falling back to the checkout.
  Missing or non-regular trusted fragments fail staging; unreadable trusted
  blobs and Git call timeouts (60 seconds per call, no credential prompts)
  disable the overlay.
  `replace_path` is ignored with a warning for `mode-judge`, `mode-judge-*`
  and `mode-orchestrate-poll-judge`; `append_path` remains supported.
- `.github/ai/concurrency_caps.yml` — parsed by
  `scripts/orchestrate_lib.py::load_concurrency_caps`. Missing or empty files
  disable the cap layer and restore legacy uncapped dispatch.
- `.github/ai/workspace_hooks/<phase>/<hook>.sh` — executed by
  `scripts/run_workspace_hook.sh`. Supported hook names are `after_create`,
  `before_run`, `after_run`, and `before_remove`; missing files are a no-op.
  Validate's four hooks run from trusted support in a tokenless, network-disabled
  container against a bounded, screened workspace copy, never the host checkout
  or its `.git`. Only non-executable, simple-name `.txt` data under
  `validation/hook-output/<hook>/` may be replayed to the host; it must not be
  sourced or executed. Any other changed or deleted path rejects the entire
  replay as an isolation/transfer failure, stopping validation even for
  nonfatal hooks. An explicit `validate.yml` `target_ref` (a `claude/implement-plan-*` branch)
  requires exactly one open trusted-author same-repo project PR targeting the
  default branch; any other base is refused. The stacked project-branch and
  `stable` targets of the retired `/implement-plan-claude` chain (#4734,
  #4791) were removed. Checkout
  pins and verifies that PR's SHA without persisting checkout credentials.
  Empty `target_ref` retains integration/default selection.
- Implement workspace hooks (#7158). `scripts/run_workspace_hook.sh` runs every
  phase's hook in the validation hook container, never on the host: no network,
  read-only root, `env -i`, and a screened workspace copy without `.git` (whose
  config carries the `GH_PAT` origin URL in implement), `.ai`, the
  workflow-support checkouts or credential files. The trusted runner copies the
  hook it resolved into the snapshot (`.github/ai/workspace_hooks/<phase>/<hook>.sh`,
  regular file reached without symlinks, at most 256 KiB), so implement's
  `after_run` / `before_remove` keep running the pre-editor copy from
  `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`. Outside `validate`, only non-executable
  `<phase>/hook-output/<hook>/<name>.txt` files come back, into
  `${RUNNER_TEMP}/workspace-hooks/output/<phase>/<hook>/`, never into the
  workspace; any other change refuses the whole result (exit 125: fatal for
  `after_create` / `before_run`, a warning for `after_run` / `before_remove`).
  A symlinked implement hook or parent directory fails support staging with
  exit 125 before any hook runs.
  Phase names must start with a letter or digit.
  The helper's own host-side Python runs with `-I -B`, so a root-level module
  in the workspace (its cwd) is never imported on the host, and all four
  implement hook steps start the helper from `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`.

## Validation manifest shell-safety gate

- `.ai/validate.yml` is untrusted checkout data, and
  `scripts/render_validation_templates.py` writes its values unescaped into
  `validation/tests/*.sh`, which `scripts/validate_driver.sh` and
  `validate_process.sh`'s runtime fallback runner execute on the host.
- `run_template_validation_harness_renderer` in `scripts/validate_process.sh`,
  which both the first render and render recovery call, parses the manifest
  with the isolated renderer Python and refuses to render (exit 14, harness
  error) when a value in `slots` or another top-level key holds a quote, `$`,
  a backtick, a backslash, a control character or a non-scalar YAML type such
  as `!!binary`. Exempt keys: `custom_tests`, `skip_tests`, `env_overrides`,
  `health_check`, `services`, `port`.
- The log names only a sanitized JSON pointer and the character class, never
  the value. Parse errors and non-mapping roots are left to the renderer.
  Recursive or shared YAML aliases are checked once per container, and
  nesting deeper than 32 levels is rejected.
- `scripts/validation_refresh_runner.py` renders consumer manifests and runs
  `validate_driver.sh` without `validate_process.sh`, so
  `_run_refresh_pipeline` applies the same check
  (`manifest_shell_safety_violations`) before rendering and reports a red
  pipeline with `manifest_shell_safety_failed: <pointer>: <class>`. It fails
  closed when the manifest is unreadable or PyYAML is missing. Keep the two
  copies' exempt keys and character classes identical.
  Finding `validation-manifest-shell-injection-fallback`; tests:
  `tests/test_validate_process_manifest_shell_safety.py`.

## Validation harness isolation

- `scripts/render_validation_templates.py::enforce_shell_safe_manifest` runs after schema validation and independently of `--schema`. It rejects:
  - a shell-active `slots.project_name` (allowlist `^[A-Za-z0-9](?:[A-Za-z0-9 ._/@+:-]*[A-Za-z0-9._/@+:-])?$`, at most 128 characters);
  - any `slots.canary_tools` entry outside `[A-Za-z0-9._+-]`;
  - a non-integer `slots.tap_plan`;
  - an out-of-range `port`.

  Errors name only the JSON pointer. Canary entries are charset-gated rather than quoted because `validation_lint.py` and the `validate_process.sh` canary-scope preflight read the literal `CANARY_TOOLS="${CANARY_TOOLS:-…}"` line. Every other manifest value in a `*.sh.j2` template goes through the `shell_quote` filter into a `_default_*` local, never inside double quotes. `tests/test_render_validation_templates_shell_quoting.py` fails on an unquoted interpolation.
- `scripts/validate_process.sh` Phase 3 never runs the harness itself. `scripts/validation_harness_sandbox.sh` handles it:
  - `provision` creates the `VALIDATION_HARNESS_SANDBOX_USER` (default `ai-validation`, not in `docker`/`sudo`) and starts its rootless dockerd. It runs once per job; self-heal re-execs reuse it through `SANDBOX_PROVISIONED`.
    It installs `uidmap` and `docker-ce-rootless-extras` with apt when they are missing. GitHub's Ubuntu runner images install Docker from download.docker.com and then delete that apt source, so when the plain install fails `provision` adds Docker's repository for that one install, accepts the key only when its fingerprint is `9DC858229FC7DD38854AE2D88D81803C0EBFCD88`, prefers the installed `docker-ce` version, and removes the source and key again. Success logs `VALIDATION_HARNESS_SANDBOX phase=provision outcome=ok reason=rootless_packages_from_docker_repo`. Failure stays fail-closed with `reason=rootless_packages_unavailable`, after one of `docker_repo_distro_unsupported`, `docker_repo_platform_unknown`, `docker_repo_tmpfile_failed`, `docker_repo_key_download_failed`, `docker_repo_key_fingerprint_mismatch`, `docker_repo_source_write_failed`, `docker_repo_update_failed` or `docker_repo_install_failed` when the repository step itself failed.
  - `selfcheck <pid>` runs as that user and verifies that `/proc/<pid>/environ`, the host Docker socket, `sudo`, credential env vars, git credential config and the runner's home/temp/workspace directories are all unreachable.
  - `run <entry>` stages a screened copy: tracked files, untracked files that are not ignored, `validation/` and `.ai/validate.yml`, minus `codex_isolated_workspace.readonly_allowed` exclusions (env templates and a credential-free `.npmrc` are admitted back; see the staging note in this section), with the trusted `validate_driver.sh` overlaid. It executes the copy under `env -i` with an allowlist, inside `setsid --wait`.
  - After the run, only regular files under `validation/logs/` are copied back, within `VALIDATION_HARNESS_SANDBOX_MAX_COPYBACK_BYTES` / `_MAX_FILES`, all or nothing.
  - `cleanup` kills the harness process group and removes its containers. Phase 3 also calls it after an idle kill.
  - Any sandbox failure exits 3 and writes `fail <phase> <reason>` to `VALIDATION_HARNESS_SANDBOX_STATUS_FILE`. `fail_closed_validation_sandbox` then writes `harness_error`, labels `ai:validation-failed` and sends a Telegram ERROR. There is no host fallback.
- `validate_driver.sh` and the runtime driver heredoc in `validate_process.sh` start every test through `run_test_without_credentials`. It unsets:
  - GitHub, model and Telegram credentials;
  - `*_TOKEN`, `*_SECRET`, `*_PAT` and `*_API_KEY`;
  - `GIT_CONFIG_*`, `ACTIONS_*` and the runner command files.

  It keeps the synthetic `TEST_*` fixtures and masks global/system git config.
- `scripts/validation_refresh_runner.py` runs each consumer self-test through the same sandbox: `provision` once per runner, then `checked-run <runner pid> scripts/validate_driver.sh`, which runs `selfcheck` against the `GH_TOKEN`-holding runner process before `run`. Logs are copied back to the old out-of-clone directory through `VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST` (default `<workspace>/validation/logs`), so drift detection is unchanged. A sandbox failure is a `self_test` failure (`red`); there is no host fallback.
- The nightly self-test (`nightly-validation-selftest.yml`) still calls the driver on the host with the per-test scrub; it runs this repository's own `tests/fixtures/selftest` fixtures, not consumer or model-written tests. Its checkout uses `persist-credentials: false`, and the self-test job holds only a read-only `GITHUB_TOKEN` (`contents: read`). Those tests run as the runner user and could rewrite that checkout's `.git` (hooks, config, `HEAD`), so the status commit does not run there: the `commit-selftest-status` job (`contents: write`) runs on a fresh runner and checkout, receives only the summary JSON artifact, and authenticates its pull/push through a one-shot credential helper that reads `github.token` from its commit step's env. Log prefix: `VALIDATION_HARNESS_SANDBOX`.
- Its `harness-sandbox-check` job is the daily check of this sandbox: on a fresh runner it runs `provision`, then `checked-run` with a probe that asks the sandbox user's rootless Docker for `docker info` and writes a log that must copy back, then `cleanup`, and logs `VALIDATION_HARNESS_SANDBOX_DAILY outcome=ok|fail`. Smoke validations and the fixtures skip the sandbox, so before this a runner-image change that broke provisioning surfaced only when every project validation failed (#6959). A failed run on the default branch reaches the workflow failure heal intake (`SCHEDULED_CHECK_WORKFLOW_NAMES`).
- Self-repo validation runs without an explicit target stage the harness infrastructure from the verified support commit, not from the validation checkout (an integration branch): `self_repo_trusted_scripts` in `validate.yml`'s staging manifest (`validation_harness_sandbox.sh`, `codex_isolated_workspace.py`, `validate_driver.sh`), staged by `stage_self_repo_trusted_support_entry` in `scripts/stage_workflow_support.sh` after the required scripts. It logs `VALIDATE_TRUSTED_SUPPORT_OVERRIDE` when the checkout's copy differed and `VALIDATE_TRUSTED_SUPPORT` once staged, refuses paths outside `scripts/` and symlinked paths, and fails closed when the trusted copy is missing. This generalises the renderer rule (#6940): project #6664 revalidated with an integration-branch sandbox script that predated #6959 and could not provision. The `self_repo_trusted_scripts` pass and the `self_repo_trusted_support_files` pass (issue #6865, described later in this section) both run on self-repo runs without an explicit target; apart from the assets those two lists name (which include `validate_process.sh`), the project's other scripts still come from the checkout, and consumer and explicit-target runs are unchanged.
- **Cgroup driver for the sandbox daemon (#7063).** Rootless dockerd uses systemd's cgroup driver on a systemd cgroup v2 host, and that driver needs a systemd user session for the sandbox user. `cgroupfs` is the default `VALIDATION_HARNESS_SANDBOX_CGROUP_MODE` (#7380); only in opt-in `auto` mode does `provision` (`sandbox_start_verified_dockerd`) enable linger for the user and wait for `/run/user/<uid>/bus` and `user-<uid>.slice`. It then starts dockerd with `DBUS_SESSION_BUS_ADDRESS` set, or with `--exec-opt native.cgroupdriver=cgroupfs` when there is no session (or in the default `cgroupfs` mode). It proves the daemon works by running one container from `ai-validation-probe:local`, an image imported from the host's `/usr/bin/true` and its `ldd` libraries, so no registry pull is needed. A `docker run` exit of 125, an exit of 126/127 whose error mentions a cgroup (the Docker CLI exits 127 for a start error containing "no such file or directory", which is how the #7063 error arrives), or a refused `docker import` (the daemon refused) under systemd, or under a daemon reused from an earlier provision in the job, restarts the daemon with cgroupfs (`outcome=retry reason=container_start_failed_systemd|container_start_failed_reused`); a second refusal fails closed with `container_start_failed`. Exit 126/127 without a cgroup error, or an image the host cannot assemble (no `ldd`, tar failure) is logged as `container_probe_inconclusive` and does not fail provisioning. The daemon's error line is logged as `VALIDATION_HARNESS_SANDBOX phase=provision probe_exit=<n> detail=<text>` (a refused import adds `stage=import` and carries the import's own exit code and error). An unknown `VALIDATION_HARNESS_SANDBOX_CGROUP_MODE` value is logged as `cgroup_mode_unrecognized_using_cgroupfs` and treated as `cgroupfs`. Without cgroups (cgroupfs in rootless mode) container resource limits are dropped; user-namespace isolation and the separate daemon are unchanged.
- In consumer repositories, `validate.yml`'s "Fetch workflow support files" step stages its support scripts from the commit the caller pinned (`shubhodeep1/coding-workflows/.github/workflows/validate.yml@<40-hex>`, read from `toJSON(job)`'s `workflow_ref`, the identity source `review_autofix.yml` and `security-audit.yml` use), fetches that commit directly and fails closed unless the checkout `HEAD` equals it. `@stable`, any other ref, a missing identity, another repository's or another workflow's ref keep `stable`; this repository keeps `main`. `setup-runtime@stable` still comes from `stable`; the GitHub API retry bootstrap and the integration-ref resolver follow the same pin (see "Immutable consumer wrapper pins"). Incident: binance-blessings#249's integration branch pinned an old `validate.yml` that failed against `stable`'s scripts on every dispatch. Logs `VALIDATE_SUPPORT_REF ref=<ref> workflow_ref=<ref|unknown>`.
- In this repository, a validation run without an explicit `target_ref` stages the renderer, its slot schema (`scripts/templates/slot_manifest.schema.json`), `validate_process.sh`, `validate_driver.sh` and `validation_harness_sandbox.sh` from the verified support commit rather than the validation checkout (issue #6865; manifest key `self_repo_trusted_support_files`, logs `VALIDATE_TRUSTED_SUPPORT_OVERRIDE` / `VALIDATE_TRUSTED_SUPPORT`), and the support step fails closed if any trusted copy is missing. `tests/test_render_validation_templates_shell_quoting.py`, `tests/test_validation_harness_sandbox.py` and `tests/test_validate_driver_credential_scrub.py` run in CI and in both release gates' `validate-scripts` jobs.
- The orchestrator poller dispatches the validation wrapper without `--ref` (issue #7138), so GitHub runs the default branch's `ai-validate.yml` / `internal-validate.yml`; `validate.yml` resolves the integration branch from `tracking_issue` and checks it out. Earlier pollers passed `--ref <integration branch>`, which ran that branch's own wrapper and its possibly stale `validate.yml` pin (binance-blessings#249). A project those runs parked with `raw_status=harness_error` is re-dispatched once from the default branch, counters kept, when the failed run's recorded `validation_last_run_head_branch` is not the default branch, or for legacy state (no `validation_last_dispatch_workflow_ref`) when the project has a separate integration branch. Guards: state flag `validation_stale_pin_recovered_ref` and a `<!-- validation-stale-pin-recovery:<sha> -->` comment by the pipeline login; the failure labels are removed (confirmed by a fresh labels read) and then the state with the flag is persisted to the tracking issue before anything is dispatched (a failure restores the failure labels, including `ai:validate-failed` when the issue had it, and retries next tick; a failed restore logs `outcome=label_restore_failed` and sends a WARNING); an operator `/revalidate` on the same tick wins. Kill switch `VALIDATION_STALE_PIN_AUTO_RECOVER_ENABLED` (default `true`); `ENABLE_VALIDATION=false` also skips it (`reason=validation_disabled`).
- **PID namespace for the harness (#7349, finding `validation-harness-credential-inheritance`).** `scripts/validation_harness_sandbox.sh` starts the harness (`run`, and so `checked-run`) through `as_sandbox_in_pidns` only. Root runs `unshare --pid --fork --mount-proc --propagation private --kill-child` and only then drops to the sandbox user with the unchanged `env -i` allowlist, so a test sees a private `/proc` and cannot list runner processes or read their `cmdline` / `environ`. `provision`, `selfcheck` and `run` first prove a namespace can be created; when it cannot, they fail closed with `reason=pid_namespace_unavailable` (exit 3, `harness_error`), and nothing runs without the namespace. Root writes the host pid of `unshare` and a launch marker (written inside the namespace before the privilege drop) to `${VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR}/<run>-<attempt>.pid` / `.started` (default `/run/ai-validation-harness`, created root-owned `0700`; it must be absolute, have at least two components, contain no `//` or trailing `/`, and lie outside the sandbox home, else `invalid_pidns_state_dir`; an existing path is used only when it is already a root-owned 0700 directory, never passed to `install -d`, which would chmod/chown it; otherwise, or when the create fails, `pidns_state_dir_failed`). A missing marker after the launch is `pid_namespace_launch_failed`. `selfcheck` runs a second probe inside a namespace created the same way and fails with `pid_namespace_shared`, `caller_pid_visible` or `host_init_visible` (`pid_namespace_unverifiable` when the host namespace id cannot be read). `start_rootless_dockerd` passes `DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--pidns`, so the daemon and every container it starts (including `--pid=host` ones) get rootlesskit's PID namespace. `selfcheck` then requires at least one of the sandbox uid's `dockerd` processes, read as root by host pid with `pgrep`, never from the sandbox-written `docker.pid`, and requires every one of them to be outside the helper's PID namespace (`docker_pidns_isolated`); otherwise it fails with `docker_pid_namespace_shared`. `cleanup` no longer reads `${work}.pgid` (written by the sandbox user; still removed): root kills only the recorded pid, after checking the pid file is a root-owned regular file, the pid is greater than 1, and the process is still a uid-0 `unshare`, and kills its forked child (the namespace's init) first. In opt-in `auto` cgroup mode the sandbox user's systemd user manager (`loginctl enable-linger`) runs in the host PID namespace, and `systemd-run --user` through its sockets would start commands outside the namespace. So, as defence in depth (the default `cgroupfs` mode starts no user manager, and `selfcheck` refuses while one runs, #7380), before the privilege drop and before the launch marker, root bind-mounts `/dev/null` over every socket under `/run/user/<uid>` except `docker.sock` (found with `find -xdev -type s`, so renaming `systemd/` does not hide `private`) inside the new mount namespace (explicit `--propagation private`, so the daemon outside keeps its bus). A symlink at the runtime dir, `bus` or `systemd/private`, a failed mount on a path that still exists, or any socket other than `docker.sock` still unmasked when root lists them again after the mask (one renamed between `find` and `mount`), exits 125 without the marker (`pid_namespace_launch_failed`); a path removed before its mount is skipped, and the namespace probe fails with `user_manager_reachable` when any socket other than `docker.sock` is still there. `validate_process.sh`, `validation_refresh_runner.py` and the workflows are unchanged; the daily `harness-sandbox-check` job reports a runner image where `unshare` stops working.
- `scripts/validation_refresh_runner.py` blanks every name in `REFRESH_PIPELINE_CREDENTIAL_ENV_NAMES` (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_PAT`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`, `CHECK_TRIAGE_ISSUES_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`) in the environment of every pipeline subprocess that works on the consumer clone: render, lint, and the harness sandbox `provision`, `checked-run` and `cleanup` calls (issue #7351). The credentials stay in the refresh process; the isolated Codex discovery launch is not a pipeline subprocess and keeps `OPENROUTER_API_KEY` for its host-side broker. `tests/test_validation_refresh_runner.py` pins both.
- **No systemd user manager by default (#7380, follow-up of #7349).** `VALIDATION_HARNESS_SANDBOX_CGROUP_MODE` now defaults to `cgroupfs` in `scripts/validation_harness_sandbox.sh`; `auto` (systemd user session first, #7063) is opt-in, and any other value is logged as `cgroup_mode_unrecognized_using_cgroupfs` and treated as `cgroupfs`. In `auto` mode a `systemd --user` manager for the sandbox uid runs in the runner's PID namespace, and a container started through the rootless daemon (outside the harness's mount namespace) could bind-mount its socket and start a transient unit there. `sandbox_release_systemd_session` (best effort, root, always returns 0) disables linger and stops `user@<uid>.service` and `user-runtime-dir@<uid>.service` when a manager or linger file exists, logging `systemd_user_session_released`. It runs before the runtime dir is (re)created on every path that ends in cgroupfs: the default/unrecognized mode, the `auto` path whose session could not be set up, and the probe-failure fallback (which then recreates `/run/user/<uid>`). `selfcheck` then runs `user_manager_route_open` after `docker_pidns_isolated`: as root, from host pids and the host mount namespace, it reports the route open when `pgrep -u <uid> -x` finds `systemd`, `dbus-daemon` or `dbus-broker`, when `/run/user/<uid>/bus` is a socket, or when a socket exists under `/run/user/<uid>/systemd`; an open route, or a uid that cannot be resolved, fails closed with `reason=user_manager_reachable_from_container` (exit 3, `harness_error`). Opt-in `auto` therefore passes only where no user manager starts. Rootlesskit's (`dockerd-rootless/`) and containerd's (`docker/`) sockets in the runtime dir are not counted: every run has them, and they cannot start host processes. The daily `harness-sandbox-check` exercises the default.
- **Socket recheck stats each entry (#7355).** Both the post-mask recheck in `as_sandbox_in_pidns` and the `user_manager_reachable` check in the namespace probe list sockets with `find … ! -type l -xtype s`, never plain `-type s`. GNU `find -type` uses the directory entry's `d_type`, which a bind mount over the path does not change, so `-type s` still lists every socket masked with `/dev/null` and every launch exited 125 (`selfcheck probe_failed`). `-xtype` stats the entry (a masked path reads as a character device), and `! -type l` keeps symlinks out as before. The initial listing that picks the paths to mask keeps `-type s`. `tests/test_validation_harness_sandbox.py` runs both checks under a real bind mount (`test_the_launch_recheck_sees_through_a_real_bind_mount`, `test_the_namespace_probe_sees_through_a_real_bind_mount`); they skip when no mount namespace can be created.
- **Self-check probe failure reasons (unblock round 1 on #7355).** `cmd_selfcheck` in `scripts/validation_harness_sandbox.sh` keeps each probe launch's exit code. When a probe's last stdout line is not a lowercase reason, `selfcheck_probe_failed_reason` picks the reason passed to `sandbox_fail`: with `VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP` set to `true` (any letter case) it is `probe_failed_plain_rc_<n>` (first pass, `as_sandbox`) or `probe_failed_pidns_rc_<n>` (second pass, `as_sandbox_in_pidns`), with `unknown` for an exit code that is not 1-3 digits; unset or any other value keeps `probe_failed`. The placeholder gates only that text, and every outcome still exits 3. Probe stderr stays discarded. `sandbox_env_is_denied` drops `VALIDATION_HARNESS_SELFCHECK_*`, so the flag never reaches the sandbox user's environment. Pending operator step: `validate.yml` forwards no such variable, so a repository variable alone does not reach the helper; a person must export it into the validate job's environment (or retire the gate). Tests: `tests/test_validation_harness_sandbox.py`.

## Workflow scenario traces

- Flag: `WORKFLOW_LOG_SCENARIO_TRACE_ENABLED` (default `false`).
- Renderer: `scripts/render_scenario_trace.py` runs downstream of
  `workflow_log_collector.v2` inside `.github/workflows/workflow-log-analysis.yml`.
- Output: local-only `.ai/workflow_traces/<run_id>.scenario.json` files. The
  directory is gitignored and this phase does not upload or commit the traces.
- Schema: top-level `schema_version`, `run_id`, `phase`, and ordered `steps[]`
  with `schema_version: "workflow_scenario_trace.v1.json"`.
- Step types: `user_message` / `assistant_text` (`ts`, `content`, `tokens`),
  `tool_call` (`ts`, `name`, `args`), and `tool_result`
  (`ts`, `output`, `exit_status`).
- Parser markers are centralized in `scripts/render_scenario_trace.py` and
  currently key off `>>> [model]`, `<<< [model]`, `[tool_call]`, and
  `[tool_result]`.
- Logging: successful writes emit `WORKFLOW_SCENARIO_TRACE_WRITTEN`; fail-open
  per-run skips emit `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL`.

---

## Run-substate ledger + state-snapshot contract

- `scripts/ledger_emit_substate.sh` writes additive run-attempt telemetry into
  `ai-memory:runs/<run-id>/ledger/events.jsonl`, and the open metadata bag in
  `ai-memory/schemas/run_ledger_entry.v1.json` is the authoritative schema
  contract for the shipped `run_substate` + token fields.
- Common shipped `run_substate` values are `PreparingWorkspace`,
  `BuildingPrompt`, `LaunchingAgentProcess`, `InitializingSession`,
  `StreamingTurn`, `Finishing`, `Succeeded`, `Failed`, `TimedOut`, and
  `Stalled`.
- Stall-sidecar markers are emitted as separate ledger event types
  `codex_stall_observed` and `codex_stall_killed` rather than as
  `run_substate` values.
- Token counts are harvested from the editor log named by `--tokens-log-file`.
  The helper reads only the trailing `LEDGER_TOKENS_LOG_MAX_BYTES` (default
  `1048576`, `0` disables the bound), because the `usage`-object scan probes
  every `{` in the document and `json.JSONDecodeError` counts newlines from
  byte 0 on each failed probe, making a whole-file scan quadratic in file
  size. Every parser keeps its last match and editors write their usage
  summary at the end, so the tail carries the same answer.
- The `record-run-event` write is bounded by `LEDGER_EMIT_TIMEOUT_SECONDS`
  (default `120`, `0` disables). It runs while the helper holds the
  per-dedupe-key `flock`, so an unbounded call would stall the caller's job
  silently. On expiry the helper warns and fails open without consuming the
  dedupe key.
- `scripts/build_state_snapshot.py` builds the poller's `state.json` artifact,
  and `ai-memory/schemas/state_snapshot.v1.json` is the authoritative schema
  for that payload.

- Job-scoped circuit breaker in `scripts/ledger_emit_substate.sh`
  (`LEDGER_EMIT_CIRCUIT_BREAKER_ENABLED`, defaulted in the helper: unset,
  empty or `true` in any letter case is on, `false` is off, and any other
  value is off with a `::warning::` that does not print the value). The
  sentinel is the empty file `${marker_dir}/circuit_open`, in the same
  per-job marker directory as the dedupe locks (`<seen file>.d`, scoped by
  run id, run attempt and job, or by `LEDGER_SUBSTATES_SEEN_FILE`), so a
  re-run attempt starts closed. An `emit_failed:timeout:*` result, or an
  `emit_failed` detail matching (case-insensitive) `authentication failed`,
  `could not read username`, `bad credentials`, `http 401`, `http 403`,
  `returned error: 403` or `permission to .* denied`, creates the sentinel
  with noclobber (an auth match whose detail also names `rate limit` or
  `abuse` is a transient 403 and leaves the circuit closed); only the caller
  that created it prints
  `::warning::ledger_emit_substate circuit opened (reason=timeout|auth); later substates in this job are skipped`.
  While the sentinel exists and the switch is on, the helper exits 0 before
  building metadata or calling `record-run-event`, with no output; those
  events are dropped. Other `emit_failed` results keep the per-call warning
  and leave the circuit closed. Output tokens, `LEDGER_SUBSTATES_ENABLED`
  and the timeout parsing are unchanged, and the helper still exits 0 on
  every telemetry failure. The switch is read only from the helper's
  environment: `implement.yml`, `review_autofix.yml` and `validate.yml` do
  not pass it from `vars.*` (unlike `LEDGER_EMIT_TIMEOUT_SECONDS`), so a
  repository variable alone does not turn the breaker off.

---

## Orchestrator tracking-issue comment markers

The orchestrator poller (`scripts/orchestrate_poll_process.sh`) maintains
two distinct marker-keyed comment families on each tracking issue. Both edit
in place every poll cycle so the tracking issue stays a live status
dashboard without producing a fresh comment per tick.

| Marker | Helper | Purpose |
|---|---|---|
| `<!-- ORCHESTRATOR_STATE_V2 part=N/N manifest=<sha> -->` … `<!-- ORCHESTRATOR_STATE_V2 -->` | `post_state_comment` / `_post_state_comment_v2_chunk` | Canonical machine-readable orchestrator state snapshot. Multi-chunk so it can carry state blobs >65 KiB. Reader: `extract_latest_valid_orchestrator_state`. Reader falls back to the legacy V1 marker `<!-- ORCHESTRATOR_STATE_V1 -->` for issues that have not yet been re-written. |
| `<!-- orchestrator:completion-status -->` | `update_completion_status_comment` | Human-readable "what is blocking completion" summary. Second-line tag `<!-- status:<token> -->` exposes the canonical status token (`in-progress` \| `waiting` \| `ready` \| `validated` \| `failed`) for grep-friendly downstream parsing. Idempotent — skips the API call when the rendered body already matches, and persists `.completion_status_comment_id` + `.completion_status_comment_body_hash` in the state file so edit-in-place fallback survives the next cron invocation. |

V1 and V2 state comments are read only when every selected comment is authored by the current `GH_PAT` login; an unavailable identity skips project processing rather than reconstructing state. Integration-sync conflict preparation also rejects an unavailable login before invoking the resolver, so the review run fails and retries on a later trigger instead of resolving without verified state. After rotating `GH_PAT` to a different account, re-post legacy state using the new account (or retain the original account) before polling resumes.

When `ENABLE_SECURITY_PASS=true` (default `true`), every completion route
enters `security-pass` before validation or finalization. A pass is valid only
when `security_pass_status == "passed"` and `security_pass_head_sha` exactly
matches the current integration head (so the periodic `main` sync skips both
security-pass states; a final PR that `main` makes unmergeable is synced and
healed during the pass by `security_pass_sync_final_pr_if_unmergeable`, see
README 12d). Findings enter `security-pass-fixing`
through one consolidated `ai:orchestrator-managed` issue (whose body asks the
implementer to clear every instance of each finding's defect class, not only
the cited line); a merged fix advances `security_pass_cycle`, clears the
recorded SHA, and re-runs the pass as a **delta audit**: the explicit range
stays `merge-base..head`, but `run_security_pass_inline` passes
`SECURITY_AUDIT_DIFF_SINCE=<security_pass_last_audited_sha>` so only range
files changed since the last audited commit stay in scope, plus the files
cited by `security_pass_reported_findings`, which travel to the engine as
`SECURITY_AUDIT_PRIOR_FINDINGS` (the engine re-emits persisting findings under
the same `finding_id` and reports remaining instances of the same class). Both
fields are written by the same `jq` that records `security_pass_head_sha`; a
clean pass empties the memory, `/re-security-pass` and the
`ENABLE_SECURITY_PASS=false` release path clear both. Legacy `passed` state
without the pointer falls back to `security_pass_head_sha`. A pointer that
does not resolve or is not an ancestor of the head falls back to the full
range; every choice is logged as `SECURITY_PASS_SCOPE tracking_issue=<N>
mode=full|delta reason=<no_prior_audit|head_advanced_since_last_audit|head_unchanged_since_last_audit|last_audited_sha_not_ancestor_of_head>
base_sha=<merge-base> since_sha=<sha|none> head_sha=<sha> prior_findings=<count>
waived_findings=<count> fix_cycle_diff_entries=<count> fix_cycle_diff_files=<count>`.
Incident: tele-funtoken-msg-scoring#3928, fun-token-multi-chain#471 and
binance-blessings#249 each merged every fix issue, never repeated a finding ID
between cycles, and still exhausted the budget on fresh full-range samples.
A delta re-audit also sees the code the previous fix cycle wrote as newly
introduced attack surface. `run_security_pass_inline` computes the current
fix-cycle diff entry `{cycle, since_sha, head_sha, files}` (range files changed
between `security_pass_last_audited_sha` and the head, added/modified only,
capped at 200 files) from the local checkout, carries the previous cycle's
entry over exactly once from `security_pass_fix_touched_files` (entries with
`cycle >= security_pass_cycle - 1`; older and malformed rows are dropped by
`ensure_security_pass_state_fields` and at read time), and hands the list to
`scripts/security_audit.sh` as `SECURITY_AUDIT_FIX_CYCLE_DIFFS`. The engine
keeps those files in scope and appends their unified diff hunks to the prompt
between `=== BEGIN/END UNTRUSTED FIX-CYCLE CODE ===` fences with rules to audit
them as fresh attack surface for new defect classes (readiness and
state-transition predicates, money-state transitions, idempotency fences), in
addition to the unchanged prior-findings rules. Hunks are capped by
`SECURITY_AUDIT_FIX_DIFF_MAX_LINES` (`1200`) and
`SECURITY_AUDIT_FIX_DIFF_MAX_BYTES` (`96000`); past the cap files are listed by
name only. The current entry is written by the same `jq` that records
`security_pass_head_sha` (a blocked result keeps at most the last 3 entries, a
clean result empties the list), and `/re-security-pass`, `/security-pass-waive`
in the failed state, the exhaustion judge's accept-all path, and the
`ENABLE_SECURITY_PASS=false` release clear it with the reported findings.
Every step fails open with a `::warning::` and today's behaviour; no GitHub
API call is involved. Incident: tele-funtoken-msg-scoring#4281 exhausted 3/3
cycles with every fix merged and no repeated finding because the last finding
sat in `_season_pool_settlement_readiness`, a predicate cycle 1's fix created
and cycles 2 and 3 were never told to audit as new code.
Line ownership (plan item 4b, D4; `SECURITY_AUDIT_LINE_OWNERSHIP`, default
`project`): the findings-json engine runs `git blame` on each finding's cited
line at the audited head and adds `"advisory": true` when no commit in
`merge-base..head` wrote it (`false` otherwise, and `false` whenever blame
fails), logging `security-audit: line_ownership mode=project blocking=<n>
advisory=<n> unknown=<n>`. A pre-project line still stays blocking when the
project could have removed what protected it (finding
`security-pass-deleted-guard-advisory`): the cited file lost or binary-changed
lines in the range (`reason=deleted_lines_in_file`), the project added lines
anywhere in the cited file (within `SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW`,
default `40`, lines of it `reason=changed_hunk_within_window`, further away
`reason=added_lines_in_file`: line distance is no proof that an appended
override cannot change execution, finding
`security-pass-distant-override-advisory`; the window only picks the logged
reason), or the finding text names another file
that lost lines (`reason=references_file_with_deletions`), the cited file names (as a word)
the module of a file that lost lines
(`reason=cited_file_references_module_with_deletions`), or such a file names
the cited module at base or head (`reason=module_with_deletions_references_cited_file`;
module = basename without extension, two or more characters such as `db`, or the
parent directory for `__init__`, `index`, `init`, `main` and `mod`), or a non-documentation file
(not `.md`/`.markdown`/`.rst`/`.txt`/`.adoc`) names both the cited module and
such a module at base or head, as a router, registry or DI configuration would,
including one the project added (`reason=shared_referrer_links_module_with_deletions`),
or a chain of such files links the cited module and a non-documentation, non-test
file that lost lines through a common referrer, up to 4 hops each way
(`reason=transitive_reference_links_module_with_deletions`; a search needing more
than 4 hops or 32 module names keeps it blocking as `reason=reference_search_limit`,
counted unknown),
or the cited file and such a file both name a third non-documentation, non-test
repository module at base or head, a shared dependency the upward search cannot
see (`reason=shared_dependency_links_module_with_deletions`; one `git ls-tree`
per commit; a one-letter dependency such as `x.py` that both name keeps it
blocking as `reason=module_name_too_short`, counted unknown),
or a file the project changed, other than documentation or tests (a `test`,
`tests`, `spec` or `__tests__` segment, `test_*`, `*_test.*`, `*.spec.*`,
`*.test.*`), names the cited module at head, even with no deletion anywhere
(`reason=changed_file_references_cited_module`); each logs
`security-audit: line_ownership_blocking finding=<id> reason=<r>`. The checks
use one `git diff --numstat --no-renames` for the range, one `-U0` diff and one
`git show` per cited file, and one `git grep` each at base and head per module
stem; a
failure of any keeps the finding blocking
(`reason=diff_failed|hunk_diff_failed|module_reference_check_failed`, counted unknown); a one-letter module name such as `x.py`, cited or with deletions, cannot be searched reliably and also keeps it blocking (`reason=module_name_too_short`, counted unknown). `run_security_pass_inline` splits advisories out of
the result right after re-applying waivers, so they never make the pass
`blocked`, enter the fix issue or `security_pass_reported_findings`, or count
toward `MAX_SECURITY_PASS_CYCLES`; an advisory-only result records `passed` at
the head. `security_pass_record_line_ownership_advisories` stores them as
`security_pass_waived_findings` rows with `source: "line_ownership"` (so the
tracking body's `Waived findings: N` includes them and the next audit does
not re-report them), files them through the deferred
`create_security_pass_advisory_followup` path, and posts one
`🔐 Security-pass advisory findings (pre-existing code)` comment.
`SECURITY_PASS_CLEAN` (main and exhaustion-judge paths) and `SECURITY_PASS_BLOCKED` (findings path)
gain a trailing `advisory=<n>`; `findings=` counts blocking findings only.
`off` restores the previous behaviour exactly.
Persistent findings after `MAX_SECURITY_PASS_CYCLES` (default `5`)
terminalize as `ai:security-pass-failed`; `/re-security-pass` resets the
bounded loop and the next audit covers the full range again.
The terminal state is engine-aware: every terminal path
(`security_pass_terminal_failure`, `security_pass_closed_fix_failure`,
`security_pass_fix_reissue_exhausted`) records the engine commit that ran the
tick in `security_pass_failed_engine_sha` (`ORCHESTRATOR_ENGINE_SHA`, resolved
once at startup by `resolve_orchestrator_engine_sha` from `ORCHESTRATE_ENGINE_SHA`
or the `.codex-workflow-src` HEAD; empty when unresolvable). With
`SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE` (default `true`), a tick whose
engine differs from that record, with no `/re-security-pass` comment claiming
the tick, performs the same reset once per engine commit
(`security_pass_auto_reset_engine_shas`, last 20 kept, both fields normalized by
`ensure_security_pass_state_fields`), logs `SECURITY_PASS_AUTO_RESET` or
`SECURITY_PASS_AUTO_RESET_SKIPPED ... reason=engine_unresolved|same_engine|already_reset_on_engine`,
and posts a `<!-- security-pass-auto-reset:<sha> -->` tracking comment. A
legacy state without the record counts as a different engine, so projects
parked by older engines (binance-blessings#249) re-run on their first tick
after a sync; the same engine failing a project again never re-fires.
The budget bounds *persistent* findings, so a recorded clean pass breaks the
chain: when the integration head advances past a `passed` SHA (a
`chore: sync <default> into <integration>` merge, a resolver/judge conflict
resolution, or a merged fix PR), `run_security_pass_inline` resets
`security_pass_cycle` to `0` before the re-audit and logs
`SECURITY_PASS_CYCLE_BUDGET_RESET ... reason=head_advanced_after_clean_pass`
with the tracking issue number and the new head SHA. Findings in the
newly-arrived code then get their own fix cycles instead of terminalizing the
project on sight. The reset fires only from a `passed` prior status; a
`blocked` chain keeps its spent budget. Completion still requires a clean
SHA-bound pass at the current head, so a fresh budget never admits unaudited
code.
Every security-pass transition that exits its current path or starts the long-running audit also keeps the tracking issue body honest:
`security_pass_fail_closed`, `security_pass_terminal_failure`,
`security_pass_closed_fix_failure`, the `blocked` write in
`create_security_pass_fix_issue`, the running, clean/pass, and
head-changed/pending writes in `run_security_pass_inline`, the stall-recovery
successor adoption, and the `/re-security-pass` reset call
`reconcile_tracking_body_after_security_pass_transition` between their
state write and `post_state_comment`, so the rendered
`<!-- orchestrator:security-pass -->` block matches the label and alert
comment the same transition posts and the persisted
`tracking_body_sync_hash` rides the state comment already being posted.
The tick-level reconcile sites run only on the `merge_conflict` and
wave-status paths and these security-pass paths leave the tick before reaching
them, which is why #3965 kept rendering `Status: passed` after it had failed.
The wrapper is hash-gated through
`reconcile_tracking_issue_body_from_state`: with `project_body_snapshot`
present, an unchanged body costs no API call; legacy state without the
snapshot first fetches the live issue body as its render template. A changed
body costs the single `gh issue edit`. It fails open.
A consolidated fix issue that orchestrator stall recovery closed and re-issued
is *not* a failed fix: `close_and_reissue` re-points wave state only (both
writes are gated on a non-null `local_id`, which a security-pass fix issue
never has), so before terminalizing, `resolve_security_pass_fix_successor`
looks for the live successor by the durable `- Tracking issue: #<N>` and
``- Local ID: `security-pass-fix-cycle-<K>` `` body markers that survive
re-issue, adopts it into `security_pass_active_fix_issues`, and logs
`SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED`. Both markers must match, so
another project or another cycle is never adopted. The review-blocked judge's
`close_and_reissue` replacement carries those markers as well:
`scripts/review_rb_judge.sh` copies the parent's `**Orchestrator metadata**`
lines (tracking issue, integration branch, local ID, priority, managed-by) only
when its tracking and branch lines agree with the GitHub-reported
`orchestrator/project-<n>` PR base. Missing, malformed, or inconsistent metadata
falls back to base-derived tracking and branch lines without an unverified local
ID. The validated block is placed ahead of the review-blocked footer
(`REISSUE_ORCHESTRATOR_METADATA_CARRIED` / `_ABSENT`), and its spot-fix
`files_touched` allowlist unions the judge's cited files with the closed PR's
changed files that still exist at its head (`REISSUE_FILES_TOUCHED_UNION`,
fail-open on a failed `pulls/<n>/files` listing), then with the new files the
judge declares in `new_output_paths` (`REISSUE_FILES_TOUCHED_NEW_OUTPUTS`).
A declared path is kept only when it passes the path validator, is
printable ASCII with no leading or trailing space (the scope guard trims
entries and splits lines on Unicode separators), carries no
glob character or trailing `/`, has no `.git` segment (any depth or
letter case), does not exist at the
closed head (a failed lookup there skips it too), and ends in a segment with
a file extension (`no_extension` otherwise: the scope guard lets a bare entry
cover everything beneath it, so a new directory-shaped path would exempt a
new subtree); at most 10 are read,
and a rejected one is skipped, never a fallback to `redo`. Incident: #4664's
reissue needed a new changelog fragment and four new fixtures that neither
source could list (heal #4665).
Before that block is appended, canonical tracking-issue, integration-branch,
and local-ID lines are removed from the judge-generated issue prose, so only
the PR-base-validated block can supply successor-adoption lineage. Incident:
binance-blessings#249 / #294, 2026-09-19. An inconclusive lookup
(API or parse failure) retains `security-pass-fixing` for retry rather than
reading a transient read failure as evidence of a failed fix.
Setting `ENABLE_SECURITY_PASS=false` remains the immediate operator kill switch.
Exhaustion is no longer terminal by default. When `MAX_SECURITY_PASS_CYCLES`
is spent with findings still open, `security_pass_exhaustion_judge` (gated by
`SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED`, default `true`, and bounded by
`MAX_SECURITY_PASS_JUDGE_ROUNDS`, default `0` = unbounded) renders
`prompts/mode-judge-security-pass-exhaustion.txt` read-only against the
audited checkout and requires one decision per remaining finding:
`accept_with_followup` (waiver row in `security_pass_waived_findings`, id
dropped from `security_pass_reported_findings`, one non-blocking `ai:security`
follow-up issue recorded in `security_pass_followup_issues`), `keep_fixing`
(one more consolidated fix issue for exactly those findings), or `fail`
(the pre-judge `security_pass_terminal_failure`). A disabled judge, a missing
prompt, a codex failure, or an invalid verdict also fall back to the terminal
path; nothing passes silently. Every round increments
`security_pass_judge_rounds` (reset by `/re-security-pass` and the kill-switch
release) and posts a `⚖️ Security-pass exhaustion judge` comment with the
decision table. With `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED` (default `true`) the
accepted finding's advisory follow-up is not filed at judge time: the waiver
row keeps `followup_pending: true`, `audited_head_sha`, and the `finding`
payload (`SECURITY_PASS_ADVISORY_FOLLOWUP_DEFERRED`), and
`security_pass_file_deferred_advisory_followups`, called from every site that
records `final_merge_status = "merged"`, files it once the integration branch
is on the default branch (`SECURITY_PASS_ADVISORY_FOLLOWUPS_FILED`, body line
naming the merging PR, `🔐 Security-pass advisory follow-ups filed` comment).
Judge-time filing planned #4090 / #4091 against a `main` that did not yet
contain the broker module the findings cite, so the planner emitted
`BLOCKED: PR #3968 is still open` and both sat in `ai:blocked`. The
`/security-pass-waive` path defers the same way. A create that fails keeps the
row pending for the next merged-state tick; `create_security_pass_advisory_followup`
clears `followup_pending` and drops the payload when it records the issue.
Right before the deferred filer, the same merged-state sites call
`security_pass_unblock_filed_advisory_followups`: each
`security_pass_followup_issues` row whose issue is not in
`security_pass_followups_merge_checked` costs one issue GET; an open follow-up
labelled `ai:blocked` costs one paginated comments GET and gets one
`/answer [auto-answered-by-poller]` comment unless a trusted
OWNER/MEMBER/COLLABORATOR User comment carries both that prefix and its durable
unblock marker (plan.yml moves ai:blocked to ai:planning on `/answer`). Every read
issue is appended to `security_pass_followups_merge_checked` (deduped,
last 100; `security_pass_mark_followup_merge_checked`) so it is never re-read
after a successful state write; failed state writes may repeat the reads, but
the durable comment marker prevents a second `/answer` POST
(`SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED ... outcome=answered|not_blocked|closed`,
`🔓 Security-pass advisory follow-ups re-planned` comment when any were
answered). The filer records the issues it creates after the merge as checked
at creation, and the completed-project finalizer re-enters both steps while
pending waiver rows or unchecked follow-ups remain. The row shape of
`security_pass_followup_issues` is unchanged. This is what un-parks advisories
filed before the merge (#4090 / #4091) without a human.
`MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default `2`, `0` = unbounded) bounds
how many judge rounds can grant another fix cycle: `judge_round` beyond the cap
puts `keep_fixing_available: false` and `max_keep_fixing_rounds` in the
diagnostics, and after verdict normalization the poller rewrites low/medium
`keep_fixing` decisions to `accept_with_followup` with the justification
prefixed `[keep_fixing capped after <c> judge round(s); converted to advisory
follow-up]` (`SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED tracking_issue=<N>
round=<r> cap=<c> converted=<n>`), so the accept-all path runs and the project
completes with deferred advisories only if no high, critical, or unrated
`keep_fixing` finding remains. A remaining blocking `keep_fixing` decision
terminalizes the pass as `ai:security-pass-failed` without waiving it,
without recording the converted rows of the same verdict, and without
creating another fix issue. The cap never waives a high, critical or unrated
finding (#6517, #6729: only low and medium findings may be converted at the
cap; #6539's stricter "convert to fail" terminalized projects whose remaining
findings were all low or medium and contradicted #6729). Recovery from the
terminal state is the unblock judge, the engine-change auto-reset,
`/re-security-pass` or `/security-pass-waive`.
Project #3965
ran fix cycles 6 and 7 on a 5-cycle budget because rounds 1 and 2 each chose
`keep_fixing` and nothing bounded the sequence.
Waivers travel to the engine as `SECURITY_AUDIT_WAIVED_FINDINGS`
and `security_pass_apply_waivers_to_findings` re-applies them to the result
(exact non-empty `finding_id` only, and only when every category, severity
and exploit scenario the waiver recorded also matches; no proximity match since
#6987, so a different id at the waived location is a new, blocking finding.
A field the waiver left empty is not compared, so a legacy row with no exploit
scenario still matches by exact id plus any recorded category and severity.
`SECURITY_AUDIT_WAIVER_LINE_WINDOW` is still validated but no longer used).
`/security-pass-waive <finding_id> ...` (human
OWNER/MEMBER/COLLABORATOR only, dedup marker
`<!-- security-pass-waive-dedup:<comment-id> -->`) records operator waivers; in
the failed state it then resets the loop like `/re-security-pass`, in
`security-pass-fixing` it only persists. The scan runs before the
`security-pass-fixing` handler because that handler ends its tick. The
tracking body's `### Security pass` block renders `- Waived findings: N` only
when N > 0, so bodies without waivers are byte-identical.
If an externally merged final PR has already deleted its integration branch,
the only permitted analysis fallback is that PR's verified immutable head SHA;
an unavailable or mismatched PR head fails closed and never substitutes the
default-branch HEAD. If that audit returns findings, the poller recreates the
still-absent integration branch at exactly the verified PR-head SHA, accepts a
create race only when the authoritative branch SHA matches, and clears stale
final-delivery state before creating the fix issue. The repair then advances
the branch that the next security pass audits, and the existing finalizer opens
a replacement delivery PR; unknown or mismatched branch state fails closed.

The completion-status comment is updated from three call sites:

1. The cycle-level wave-status decision block (every poll tick — derives
   `in-progress` / `waiting` / `ready` / `failed` from the
   `check-wave-status` JSON plus the live validation-recovery state,
   and fires a once-per-project `tg_notify` CRITICAL on the first
   `any_failed=true` observation, guarded by
   `.completion_status_failure_alert_sent` in the state file; compare-API
   failures surface as an explicit "integration status is unknown"
   line instead of silently omitting the integration gate state).
2. `mark_validation_complete` — final transition to `validated`.
3. `mark_validation_failed` — transitions to `in-progress` during
   validation recovery, and to `failed` on both terminal branches (the
   deterministic-class short-circuit and the recovery-budget-exhausted
   path).

The security-pass helpers update the same pinned comment to `waiting` while
the audit engine or consolidated fix issue gates completion, and to `failed`
when the bounded fix-cycle budget is exhausted.

The same change also adds a defensive preflight inside
`dispatch_validation_if_needed`: when the current wave's PRs are not all
merged into the integration branch (`WAVE_COMPLETE != "true"`) at dispatch
time (rare race against label-reconciliation, or a wave PR that transitioned
back from merged), the dispatch is skipped this cycle so the
runtime-validation workflow is not burned on a state that cannot pass. When
the validating / `/revalidate` paths reach the helper before the loop's main
wave-status block has populated the scratch `WAVE_COMPLETE` / `ANY_FAILED`
variables, the helper recomputes live wave status first and fails closed if
the probe itself cannot run. Re-entry on the next 5-minute poll tick
converges the project once wave PRs settle.

The preflight deliberately does **not** blanket-gate on `ANY_FAILED`.
`ANY_FAILED` is broad: a wave issue legitimately closed without a merged PR
(reconciled status `"closed"` — e.g. a judge-fix-up whose premise turned out
false, so no code change was needed) yields `WAVE_COMPLETE=true` **and**
`ANY_FAILED=true` simultaneously, because `"closed"` is in the
merged/closed/skipped set that keeps `all_merged` true. Gating on
`ANY_FAILED` there deferred dispatch on every poll cycle and wedged the
project in `ai:validating` forever (validation never dispatched → never
earned `ai:validated` → integration never merged → tracking issue never
closed; real-world repro: `hylifegroup.com#3`, stuck 10 days).

But `ANY_FAILED` also covers explicit failed terminal phases (for example
`ai:plan-failed`) and the dedicated `ai:implementation-failed` status, and
those **must** continue to block validation even if the wave still reconciles
to `WAVE_COMPLETE=true`. So `check-wave-status` now emits a narrower
`validation_dispatch_safe_despite_failures` signal: it is `true` only when
every failed issue is an adjudicated closed-without-merge case (live issue
closed or `ai:closed`, with no blocking terminal-failure phase). The validate
dispatch preflight keys on `WAVE_COMPLETE` plus that finer signal, while
still logging `ANY_FAILED` for observability.

The preflight gates on the wave-merge signal (`WAVE_COMPLETE`) rather than on
`PROJECT_COMPLETE`. `PROJECT_COMPLETE`
additionally folds in `integration_contained_in_default` (`ahead_by == 0`),
but runtime validation dispatches against `ref=integration_branch`, so the
integration→default merge is **not** a precondition for validation — that
merge is performed afterward by
`mark_validation_complete → finalize_integration_merge_if_needed` once the
run earns the `ai:validated` label. Gating dispatch on `PROJECT_COMPLETE`
deadlocked any project using a separate integration branch: `ahead_by`
stays `> 0` until the final merge lands, but that merge waits for
`ai:validated`, and `ai:validated` waits for a validation run that the gate
would never dispatch (validation needs the merge; the merge needs
validation). Default-branch-only projects never hit it because
`ahead_by ≡ 0`. This is the validation-dispatch sibling of the judge
hard-guard fix for `bitsafe.io#325`, which removed the same over-broad
`ahead_by == 0` gate from the `JUDGE_STATUS=complete` override. The
judge-side override at the `JUDGE_STATUS=complete` branch remains the
primary completion gate and likewise no longer blocks on integration drift.

Security-pass audit worktree (#7295, Refs #6664): `prepare_tracking_judge_checkout` may force the poller's checkout to the integration head and then restore the poller's staged support copies of `scripts/` and `prompts/` over it, so the audited tree held the default branch's helpers while the diff came from the integration branch. `run_security_pass_inline` therefore checks the audited head out with `security_pass_prepare_audit_worktree` into `${RUNTIME_DIR}/security-pass-audit-wt-<tracking issue>` (detached, HEAD must equal the audited SHA, no tracked change, else `security_pass_fail_closed engine_unavailable`). The engine runs there from the trusted support root (`bash "${security_pass_support_root}/scripts/security_audit.sh"`, `SECURITY_AUDIT_SUPPORT_DIR=<support root>`, and `SECURITY_AUDIT_FP_EXCLUSIONS` pinned to the support root's catalog so the audited branch's copy cannot suppress findings), and the exhaustion judge runs with that worktree as its working directory, which its read-only sandbox snapshots (`POLLER_JUDGE_WORKSPACE_OVERRIDE` is not used: non-RB judges snapshot `pwd`). `ensure_security_pass_before_completion` removes the worktree after every return (`security_pass_remove_audit_worktree`). `SECURITY_PASS_AUDIT_CLEAN_WORKTREE_ENABLED` (default `true`; `false` audits in place, any other value warns and audits in place). The poller runs the default branch's copy, so the change takes effect once it is on `main`. The integration judge's sandbox still snapshots `GITHUB_WORKSPACE`; that is a separate follow-up.

---

## Stable log prefixes (contractual)

Workflow-log-analysis and API-hygiene reporting depend on these stable log
prefixes. Renames are breaking unless an alongside-old shim is documented
and shipped:

- `CODEX_ISOLATION`
- `DEPENDENCY_PROXY`
- `LABEL_REPAIR`
- `LABEL_REPAIR_DIFF`
- `LABEL_SYNC_CREATED`
- `LABEL_SYNC_UPDATED`
- `LABEL_SYNC_UNCHANGED`
- `LABEL_SYNC_ERROR`
- `LABEL_SYNC_DELETED`
- `LABEL_SYNC_RETIRED_ABSENT`
- `RETIRED_FILE_REMOVED`
- `RETIRED_FILE_KEPT_MODIFIED`
- `AUTOFIX_PEER_CHECK`
- `AUTOFIX_DISPATCH_SKIPPED`
- `AUTOFIX_DISPATCH_ISSUED`
- `AUTOFIX_GATE_SKIP`
- `REVIEW_HEAD_GATE`
- `AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD`
- `AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED`
- `AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE`
- `AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED`
- `AI_PHASE_FAILURE_V1`
- `AI_PHASE_GATE_V1`
- `WORKFLOW_SCENARIO_TRACE_WRITTEN`
- `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL`
- `RETARGET_MERGED_BASE` (`scripts/retarget_merged_base.sh`: `mode=resolve|pr repo= pr= from= to= merged_pr= outcome=retargeted|unchanged reason=`)
- `STANDALONE_AUTO_DECIDE` (`clarify.yml` "Standalone auto-decide": `issue= outcome=answered|skip|failed reason= decisions=`, `reason=delegated_to_clarify_respond` when the worker answers; `orchestrate_clarify_respond.yml` "Standalone RECOMMENDED fallback" and "Record standalone auto-decisions": `issue= outcome=fallback|skip|answered reason=worker_failed|worker_failed_undecided decider= decisions= ad_total= setup_total=`)
- `AI_ENGINE_SELECTED` (`scripts/ai_engine.sh`: `role= engine= model= effort= source=`)
- `AI_ENGINE_FALLBACK` (`scripts/ai_engine.sh`: `role= reason=`; the run uses codex unless `AI_ENGINE_FALLBACK_REFUSED` follows)
- `AI_ENGINE_FALLBACK_REFUSED` (`scripts/ai_engine.sh` and the sandbox Claude branches, as `::error::AI_ENGINE_FALLBACK_REFUSED role= reason=`: a non-capacity fallback under `AI_ENGINE_FALLBACK_POLICY=capacity`; exit 76, codex does not run)
- `AI_ENGINE_FALLBACK_REPORT` (`scripts/ai_engine_fallback_report.sh`: `outcome=none|reported pairs= dispatched= records= codex_fallbacks=`, `dispatched role= fallback_reason=`, `skip_dispatch reason=`, `skip reason=no_records|records_symlink|records_oversized`)
- `REVIEW_UTILITY_ISOLATION` (`scripts/summarize_reviewer_consensus.sh`, `scripts/review_synthesise_smoke.sh`, `scripts/review_run_judge_interim.sh`, as `::error::REVIEW_UTILITY_ISOLATION role=SUMMARISER|BEHAVIOURAL_SMOKE|JUDGE_INTERIM engine=claude|codex outcome=refused reason=sandbox_unavailable|sandbox_helper_outdated`: the review sandbox could not run the role, so the attempt was refused instead of running OpenCode on the host. The interim judge (`JUDGE_INTERIM_ENABLED`) runs only sandboxed OpenCode in the read-only `JUDGE_INTERIM` role of `scripts/review_untrusted_sandbox.sh` (`engine=codex`; the role refuses the Claude engine) and, when refused, logs `JUDGE_INTERIM_PASS_FAIL reason=isolation_unavailable` and stays fail-open. All three scripts load their helpers (the sandbox helper, `ai_engine.sh`, `opencode_helpers.sh`, `write_opencode_config.sh`) from an absolute `SUPPORT_SCRIPTS_DIR`; when it is unset or relative they use their own directory and log a `::warning::`, so a relative `scripts` path never selects the PR checkout's copies)
- `NEEDS_HUMAN` (`scripts/operator_step_issue.py needs-human park|prune`, called by `scripts/unblock_judge.sh` and `scripts/orchestrate_poll_process.sh`: `item= kind= reason= outcome=parked|updated|removed|skip`, poller skips add `detail=`)
- `OPERATOR_STEP_TICK` (`scripts/operator_step_issue.py tick`, run by `scripts/promote_main_cycle.sh`: `key= outcome=ticked|already_done|no_source_sha|not_ancestor|unknown_commit stable=`)
- `CLAUDE_POOL` (`scripts/ai_engine.sh` and the sandbox Claude branches: `run role= account= outcome= reason= exit_code=`, `account_skipped account= reason=`)
- `CLAUDE_POOL_HEALTH` (`scripts/claude_pool_health_alert.sh`, the "Alert on Claude pool accounts at the usage gate" step of `orchestrate_poll.yml`: `accounts= gated= auth_failed= probe_failed= alert=sent|not_delivered|none|outside_window|disabled|no_probes|invalid_probes`; `sent` means one Telegram WARNING named every account at or above `gate_utilization` and every rejected token; the step reads the pool action's `probes` output and sends at most once an hour, see README "Near-cap alert")
- `SANDBOX_IMAGE` (`scripts/sandbox_image.sh`: `family= outcome=pulled|built|published|publish_skipped reason= ref=`)
- `AI_ENGINE_PROJECT_LABEL` (`orchestrate.yml` "Ensure orchestrator labels exist": `label=`, `none` when unset; the label the tracking and wave-1 issues get)
- `AI_ENGINE_PR_LABEL` (`implement.yml` "Create Pull Request": `issue= label=`; the engine label copied from the issue to its PR)
- `SINGLE_ISSUE_SECURITY_PASS` (`scripts/review_single_issue_security_pass.sh`: `mode=gate|status|report pr= head= outcome=clean|hold|dispatched|skip|findings|failed|exhausted reason= cycle=`; clean markers require the authenticated pipeline author and an exact audited PR head. Missing/disabled audits report failed, and an unverifiable marker source holds auto-merge. A failed dispatch logs `outcome=hold reason=dispatch_failed` and posts a `failed` marker for the used cycle (past the cap, `reason=dispatch_failed_exhausted` and the marker counts as a used head attempt). If result publication fails, report skips review re-dispatch so it cannot run without the marker. After dispatch the gate confirms the pending-marker comment response with bounded retries and fails closed with `reason=pending_marker_failed` if none is confirmed. `mode=status` writes no GitHub state or step output, but may fetch missing Git history to verify extension ancestry before the review-blocked judge chooses its mode; failed verification reports `unverifiable`. `outcome=hold reason=cycles_exhausted` writes `exhausted=true` only for completed current-head findings, and status also emits `SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD`. Without a completed audit the gate retries a bounded number of times per head before reporting `exhausted_unaudited` and holding without the judge bypass. The judge re-verifies the audited head before a security-mode merge. Cycles available = `MAX_SECURITY_PASS_CYCLES` plus one per distinct fix SHA in a trusted `ai:single-issue-security-pass-extension:v1` marker whose commit is reachable from the audited head; duplicate comments for one SHA count once, and a mismatched checkout holds the gate and skips report publication. On a current-head findings marker before exhaustion, `awaiting_followups` requires an open `ai:security` issue authored by the pipeline account for that branch and a findings marker younger than `SECURITY_PASS_FOLLOWUP_STALE_HOURS`; otherwise the gate holds with `followups_missing`, `followups_unverifiable` or `followups_stalled`.)
- `SECURITY_HOLD_SWEEP` (`scripts/review_security_hold_sweep.py`: `pr= head= cycle= outcome=dispatch_failed|dispatched marker=posted|failed`, `pr= outcome=skip reason=comment_history_unavailable`, final `outcome=complete dispatched=`; the source 30-minute review sweep and consumer `ai-review.yml` hourly schedule batch open PRs, trust only the pipeline account's current-head findings marker (reading the full comment history only when a PR has more than 100 comments and none of the latest 100 carries a current-head result), and dispatch the judge once per stale head/cycle through the PR-named wrapper (`internal-review.yml` with `force_rb_judge=true` in this repo, `ai-review.yml` in consumers) so the active-run guards see it. Marker `<!-- ai:security-followup-stale-dispatch:v1 ... -->` deduplicates successful dispatches.)
- `RB_JUDGE_SECURITY_PASS` (`scripts/review_rb_judge_security_pass.sh`, sourced by `review_rb_judge.sh`: `mode=detect|findings|merge_gate|extension|severity_block pr= outcome= reason=`; `severity_block` converts merges to fixes while retries remain and holds any final-round action, including `close_and_reissue`, when high/critical/unrated findings remain. For blocking findings the judge withdraws prior auto-merge enrollment even if the live head moved, then refuses to act on a mismatched head; an unreadable enrollment or failed disable stops the judge. Unavailable hold-comment history fails closed to avoid duplicate comments and alerts. `merge_gate outcome=hold` means a judge merge waited for the single-issue security pass and the judge step output `judge_action=security_hold` with `judge_skip_reason=security_hold_<hold_reason>`; the log line carries `hold_reason=`. The Telegram judge alert is suppressed only for `audit_dispatched`, `audit_pending` and `awaiting_followups` and sent as CRITICAL, naming the reason, for any other hold, including `followups_missing`, `followups_unverifiable` and `followups_stalled`; a failed gate that wrote `pending_marker_failed` retains that reason instead of reporting `gate_failed`. A final-retry `fix` is treated as a merge without creating a fix commit only when no blocking findings remain.)
- `RECLARIFY_UNROUTED` (`detect_unrouted_blocked_comments` in `scripts/orchestrate_poll_process.sh`: `issue= comment= reason=command_unrouted|no_command age_minutes= outcome=flagged|post_failed`, `outcome=skip reason=budget_low|issue_list_unavailable|graphql_unavailable|classifier_failed`)
- `ACTIVATION_VERIFY` (`scripts/activation_verify.sh`: `mode=pr|project item= verdict=LIVE|DORMANT code_gaps= operator_gaps= outcome=posted|skip reason=`)
- `UNBLOCK_SCAN` (`run_unblock_scan` in `scripts/orchestrate_poll_process.sh`: `candidates= dispatched= skipped= outcome=idle|done|skip reason=`, and `item= kind= outcome=dispatched|dispatch_failed`)
- `UNBLOCK_JUDGE` (`scripts/unblock_judge.sh`: `item= kind= stop= fingerprint= verdict= round= outcome=acted|waiting|followup|skip reason=`, and `op= outcome=failed` per failed operation; `op=regrant run= paths= outcome=regranted`, `op=regrant [run=] outcome=skip reason=still_denied|already_regranted|not_automation_path|missing|truncated|malformed|grant_unavailable|rejection_unreadable|disabled`, `op=regrant run= outcome=failed reason=actuation_failed`; `origin= outcome=escalated reason=fixup_blocked`, `origin= outcome=skip reason=fixup_escalated`, `origin= outcome=failed reason=fixup_escalation_failed`; `[verdict=wait] [fixup=] op=hold hold=pr_on_branch|item_closed|placeholder_retired ref= outcome=held|holding|met|expired|skip reason=max_days|cannot_be_met|waits_disabled|hold_check_unavailable|hold_refresh_failed|followup_failed|hold_record_failed|marker_failed|record_failed`; `op=wait_config outcome=disabled|default reason=invalid_value|invalid_max_days`; `op=triage_resolution outcome=closed|skip|judge status= [rc=] [reason=write_failed|default_branch_unknown]`)
- `UNBLOCK_PROJECT` (`handle_unblock_judge_project_hooks`: `tracking_issue= action=abandoned|fixup id= item= issue= outcome= [retarget=true|false]`; `outcome=superseded` marks a re-issued original `skipped`)
- `UNBLOCK_HANDOVER` (poller and `review_autofix.yml`: `tracking_issue=|pr= stop=judge_output|rb_judge reason= failures= outcome=counted|failed|labelled`)
- `UNBLOCK_BULK_DELETE_OVERRIDE` (`implement.yml` "Unblock judge bulk-delete override" and the destructive guard: `outcome=enabled|skip reason= marker=`, `applied deletions=`)
- `JUDGE_INTERIM_PASS_OK`
- `JUDGE_INTERIM_PASS_FAIL`
- `JUDGE_INTERIM_PRIORS_MERGED`
- `BEHAVIOURAL_SMOKE_SYNTHESISED`
- `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL`
- `BEHAVIOURAL_SMOKE_PRESENT_FAILED`
- `BEHAVIOURAL_SMOKE_PRESENT_PASSED`
- `BEHAVIOURAL_SMOKE_SANDBOX` (`scripts/validate_driver.sh`: `test= outcome=ran exit=` or `outcome=skipped reason=docker_missing|image_unavailable|start_failed|stage_failed|source_snapshot_unavailable|source_snapshot_incomplete|unsafe_test_path`; a test is synthesised (`is_synthesised_test`, also in the fallback runner) when its name matches `synth_round_*.sh` in any letter case, when it carries the generated-wrapper marker (`BEHAVIOURAL_SMOKE_SANDBOXED` or `__BEHAVIOURAL_SMOKE_`), or when its content cannot be checked; such a test is never the canary. Before Phase 3 `scripts/validate_process.sh` regenerates `validation/validate.sh` and launches it only when it equals the canonical wrapper byte for byte and `scripts/validate_driver.sh` equals the copy snapshotted into `RUNTIME_DIR` before any model phase (otherwise `harness_error`, `ai:validation-failed`; a driver absent at startup selects the fallback runner); without `scripts/validate_driver.sh` it always uses its own fallback runner. The materializer refuses a symlinked `.ai` or `.ai/review_runtime` and any git-tracked file under `.ai/review_runtime` (a failed check fails closed in a git checkout), reads cache files without following symlinks up to 1 MiB, and writes relative to a no-follow descriptor of `validation/tests`. Every such test runs in a `python:3.12-slim` container with `--network none`, a read-only root, all capabilities dropped, docker under `env -i` and only `HOME`, `TMPDIR` and `BEHAVIOURAL_SMOKE_SANDBOXED=1` set, with a `git archive` of `HEAD` and no `.git`; an unavailable sandbox reports TAP `ok ... # SKIP` and never runs the test on the host. `VALIDATION_SYNTH_SANDBOX_TIMEOUT_SECS`, default `300`, bounds each run and each image inspect or pull (a timed-out run's partial output is commented out and reported as `# SKIP`); generated wrappers run their body only when `BEHAVIOURAL_SMOKE_SANDBOXED=1`, the script runs from the sandbox mount `/synth/test.sh`, `/sys/class/net` lists only `lo`, and no pipeline credential variable is set; otherwise they log `BEHAVIOURAL_SMOKE_PRESENT_INCONCLUSIVE ... reason=not_sandboxed detail=env|path|network|credentials`. The synthesiser also drops a body that names a pipeline credential or starts a network client (`curl`, `wget`, `nc`, `ssh`, ..., or `/dev/tcp/`). The driver unsets an inherited `BEHAVIOURAL_SMOKE_SANDBOXED` and never picks a `synth_round_*.sh` file as the canary; `validate_process.sh`'s fallback runner, which has no sandbox, reports synthesised tests as `# SKIP` with `outcome=skipped reason=fallback_driver`. `scripts/validate_process.sh` materialises only `synth_round_<n>_<slug>.sh` names whose round matches the manifest's and a `synth_round_<n>_manifest.json` copy under the manifest's own name, from non-symlink cache files; it creates each target with `O_EXCL|O_NOFOLLOW`, never overwrites a different file or follows a symlink (a byte-identical file counts as already materialised), and skips duplicate names. It launches the harness without `GH_TOKEN`, `GITHUB_TOKEN`, `GH_PAT`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`, `CHECK_TRIAGE_ISSUES_TOKEN`, the Actions OIDC request and runtime variables, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `BEHAVIOURAL_SMOKE_SANDBOXED`, or any other variable whose name starts with `GH_`, `OPENROUTER_`, `TG_BOT_`, `CHECK_TRIAGE_`, `ACTIONS_ID_TOKEN_`, `ACTIONS_RUNTIME_`, `ACTIONS_CACHE_`, `ACTIONS_RESULTS_`, `CLAUDE_`, `ANTHROPIC_` or `GIT_CONFIG_`, or is `GIT_ASKPASS`, `SSH_ASKPASS` or `SSH_AUTH_SOCK`, or ends with `_TOKEN`, `_SECRET`, `_KEY`, `_API_KEY`, `_PAT`, `_PRIVATE_KEY`, `_PASSWORD`, `_PASSWD`, `_CREDENTIAL(S)` or `_COOKIE` (`VALIDATION_TEST_*` and `TEST_*` fixtures are kept). The generated wrapper refuses to run its body while any non-empty variable matches the same `CREDENTIAL_NAME_POLICY`, except the image's public `GPG_KEY`. Findings `smoke-synth-credentialed-test-exec`, `smoke-synth-unsandboxed-driver`, `smoke-synth-fallback-driver-host-exec`, `smoke-manifest-canary-overwrite`, `validation-harness-credential-inheritance`.)
- `REISSUE_BASELINE_PRESERVED`
- `REISSUE_BASELINE_DISCARDED`
- `REISSUE_MODE`
- `REISSUE_FILES_TOUCHED_UNION`
- `REISSUE_FILES_TOUCHED_NEW_OUTPUTS`
- `REVIEW_BLOCKED_FIX_SCOPE_ACCEPTED`
- `REVIEW_BLOCKED_FIX_SCOPE_REJECTED`
- `REVIEW_BLOCKED_FIX_TARGET_VERIFIED`
- `REVIEW_BLOCKED_FIX_TARGET_REJECTED`
- `REVIEW_BLOCKED_HEAD_IDENTITY_REJECTED`
- `REISSUE_ORCHESTRATOR_METADATA_CARRIED`
- `REISSUE_ORCHESTRATOR_METADATA_ABSENT`
- `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1`
- `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1`
- `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1`
- `FINGERPRINT_STATE_SELFHEAL_V1`
- `FINAL_MERGE_INELIGIBILITY_ALERT_SENT`
- `EAGER_DRAFT_PR_CREATED`
- `APPLY_ANALYSIS_SKIPPED`
- `APPLY_ANALYSIS_DISPATCHED`
- `APPLY_ANALYSIS_DOC_GONE`
- `APPLY_ANALYSIS_IN_FLIGHT_DOCS`
- `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED`
- `APPLY_ANALYSIS_CANDIDATES`
- `PROMOTE_CYCLE_SKIPPED`
- `PROMOTE_CYCLE_FAILED`
- `PROMOTE_CYCLE_DISPATCHED`
- `COMPREHENSIVE_VERIFICATION_DISPATCHED`
- `COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED`
- `COMPREHENSIVE_PROMOTION_DISPATCHED`
- `COMPREHENSIVE_PROMOTION_DEFERRED`
- `COMPREHENSIVE_PROMOTION_HOLD`
- `COMPREHENSIVE_PROMOTION_DONE`
- `COMPREHENSIVE_PROMOTION_FAILED`
- `COMPREHENSIVE_PROMOTION_SKIPPED`
- `COMPREHENSIVE_VERIFYING_COMPLETE`
- `COMPREHENSIVE_MARKER_UNTRUSTED`
- `TRACKING_ISSUE_LABEL_APPLIED`
- `TRACKING_ISSUE_BINDING_FAILED`
- `RELEASE_STALE_TIP`
- `RELEASE_UNTESTED_HEAD`
- `RELEASE_CI_GATE` (`scripts/release_ci_gate.sh`: `repo= sha= outcome=pass|waiting|fail reason= check_run_id= status= conclusion= waited_secs=`)
- `TRACKING_ISSUE_COMMENT_POSTED`
- `AUTO_RELEASE_SKIPPED`
- `AUTO_RELEASE_DISPATCHED`
- `IMPLEMENT_BLOCKED_TERMINALIZED`
- `EAGER_DRAFT_PR_PROMOTED`
- `INTEGRATION_STALE_ALERT_SENT`
- `STALL_INFLIGHT_DIRECT_CHECK`
- `HARNESS_ERROR_DETECTED`
- `FORCE_MERGE_BYPASS`
- `BACKPRESSURE_TRIGGERED`
- `BACKPRESSURE_CLEARED`
- `RECOVERY_BUDGET_ACCOUNTING`
- `VALIDATION_DISCOVERY_STARTED`
- `VALIDATION_DISCOVERY_AGREE`
- `VALIDATION_DISCOVERY_DISAGREE`
- `VALIDATION_DISCOVERY_PR_OPENED`
- `VALIDATION_DISCOVERY_PR_REUSED`
- `VALIDATION_DISCOVERY_FAILED`
- `VALIDATION_DISCOVERY_SKIPPED_DEDUP`
- `VALIDATION_DISCOVERY_SKIPPED_DISABLED`
- `VALIDATION_DISCOVERY_SKIPPED_BUDGET`
- `VALIDATION_DISCOVERY_DRY_RUN`
- `VALIDATE_TRUSTED_TEMPLATE_OVERRIDE`
- `VALIDATE_TRUSTED_TEMPLATES`
- `VALIDATE_TRUSTED_RENDERER_OVERRIDE`
- `VALIDATE_TRUSTED_RENDERER`
- `VALIDATE_TRUSTED_SUPPORT_OVERRIDE`
- `VALIDATE_TRUSTED_SUPPORT`
- `VALIDATION_HARNESS_SANDBOX_DAILY`
- `REVIEWER_RISK_TIER`
- `REVIEWER_FILTER_SKIP`
- `REVIEWER_FAILBACK`
- `REVIEWER_FAILBACK_UNMAPPED`
- `REVIEWER_HEALTH`
- `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED`
- `EDITOR_REVIEWER_CHECKSUM_SUMMARY`
- `RE_REVIEW_SKIP`
- `CONTEXT_BUDGET_WARN`
- `CODEX_HEARTBEAT`
- `BREAK_GLASS`
- `WRITE_GUARD_BLOCK`
- `WRITE_GUARD_CONFIG_ERROR`
- `WRITE_GUARD_BYPASS_ENV`
- `DRIFT_SCAN_START`
- `DRIFT_SCAN_RETRY`
- `DRIFT_SCAN_DIFF`
- `DRIFT_SCAN_OK`
- `DRIFT_SCAN_ERROR`
- `SECURITY_PASS_STARTED`
- `SECURITY_PASS_FINAL_PR_SYNC`
- `SMOKE_FIXTURE_PROJECT`
- `TG_NOTIFY_SMOKE_SILENCED`
- `ORCHESTRATOR_STATE_AUTHOR_FILTER` (`tracking_issue= outcome=identity_unavailable|filtered ignored=<count>` when filtered)
- `SECURITY_PASS_SCOPE`
- `SECURITY_PASS_CLEAN`
- `SECURITY_PASS_BLOCKED`
- `SECURITY_PASS_FIX_ISSUE_CREATED`
- `SECURITY_PASS_FIX_ISSUE_REISSUED`
- `SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED`
- `SECURITY_PASS_FIX_MERGED_EVIDENCE`
- `SECURITY_PASS_CYCLE_BUDGET_RESET`
- `SECURITY_PASS_FAILED`
- `SECURITY_PASS_SKIPPED_DISABLED`
- `SECURITY_PASS_JUDGE_SKIPPED`
- `SECURITY_PASS_JUDGE_FAILED`
- `SECURITY_PASS_JUDGE_DECIDED`
- `SECURITY_PASS_WAIVED`
- `SECURITY_PASS_WAIVE_REJECTED`
- `SECURITY_PASS_WAIVED_SUPPRESSED`
- `SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED`
- `SECURITY_PASS_ADVISORY_FOLLOWUP_DEFERRED`
- `SECURITY_PASS_ADVISORY_FOLLOWUPS_FILED`
- `SECURITY_PASS_AUTO_RESET`
- `SECURITY_PASS_AUTO_RESET_SKIPPED`
- `STAGED_SUPPORT_LATCH_RELEASED`
- `STAGED_SUPPORT_LATCH_SKIP`
- `STAGED_SUPPORT_LATCH_RELEASE_SKIPPED`
- `ORCHESTRATOR_ENGINE_SHA`
- `SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED`
- `SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED`
- `VALIDATION_RUN_ATTRIBUTION`
- `CI_CANCELLED_RERUN`

- `SEMBLE_QUERY`
- `SEMBLE_FALLBACK`
- `SEMBLE_BOOTSTRAP`
- `SERENA_QUERY`
- `SERENA_FALLBACK`
- `SERENA_PROBE`
- `TASK_STATE_UNBLOCK`
- `TASK_STATE_WRITE_FAIL`
- `EVENTS_EMIT`
- `EVENTS_EMIT_FAIL`
- `NAG_REMINDER_LOAD_FAIL`
- `TRANSCRIPT_ARCHIVE_FAIL`
- `IDENTITY_REINJECT_PARSE_FAIL`
- `drift-audit:`
- `CHECK_TRIAGE`
- `WORKFLOW_HEAL_REPORT`
- `WORKFLOW_HEAL_AUTOFIX_REPORT`
- `WORKFLOW_HEAL_PHASE_REPORT`
- `WORKFLOW_HEAL_PR_RECONCILE`
- `WORKFLOW_HEAL`
- `WORKFLOW_LOG_FORK_EXCLUDED` (`scripts/collect_workflow_logs.py`: `repository= run_id= reason=fork|origin_unknown`; a fork-origin or unknown-origin pull-request run keeps its counts and conclusion but its log text, cost telemetry, job/step names, workflow name/path and head-repository name are dropped (a family outside the fixed vocabulary becomes `other`), and neither the collector (including the PAT-budget report) nor `scripts/summarize_unselected_runs.py` fetches its log archive, so the workflow-log-analysis model never sees fork-written text; `log_download_status=excluded_untrusted_origin`)
- `HEAL_ISOLATED_EDITOR`
- `HEAL_SCOPE_REFUSED`
- `WORKFLOW_HEAL_EVIDENCE`
- `AUTOFIX_FINGERPRINT`
- `AUTOFIX_FINGERPRINT_CAP_TRIPPED`
- `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED`
- `AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED`
- `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP`
- `NOOP_RECOVERY_FINGERPRINT_CAP_STALE_SUPPORT`
- `REVIEW_EDITOR_PREFLIGHT`
- `IMPLEMENT_AUTOMATION_PATH_GUARD`
- `STAGE_MAIN_PINNED_DIVERGENCE`
- `WORKTREE_REGISTER`
- `WORKTREE_DEREGISTER`
- `WORKTREE_GC`
- `WORKTREE_REGISTRY_REBUILD`
- `WORKTREE_REGISTER_INVALID_NAME`
- `WORKTREE_REGISTER_FAIL`
- `WORKTREE_DEREGISTER_FAIL`
- `opencode_agent_failure`
- `AUTOFIX_FAILURE_HEADLINE`
- `MODEL_CATALOG_BACKFILL`
- `CLAUDE_FIXER_AUTO_MERGE`
- `RB_JUDGE_ISOLATION`
- `CONSOLIDATOR_ISOLATION`
- `REVIEW_SANDBOX_CLEANUP` (`scripts/review_untrusted_sandbox.sh cleanup`, run after `Commit changes`: `reason=root_pattern_mismatch|root_outside_runner_temp|image_marker_missing|baseline_missing|remove_permission_repaired|remove_failed cause=permission_denied|not_empty|busy|other`; path-free, teed into `editor_stage_stderr.txt`)
- `REVIEW_SANDBOX_BUILD` (`scripts/review_untrusted_sandbox.sh prepare` / `prepare-ephemeral` image build: `attempt=<n> outcome=ok|retry|fail rc=<exit>`; `retry` only for registry or network errors, up to `REVIEW_SANDBOX_BUILD_ATTEMPTS`; kept in `sandbox_prepare_stderr.txt` and quoted in `sandbox_prepare_failure_evidence.txt`)
- `JUDGE_ISOLATION`
- `JUDGE_ENGINE_LABELS` (`scripts/orchestrate_poll_process.sh`: `role= outcome=forced_codex reason=issue_labels_unavailable` when a per-issue label snapshot cannot be verified).
- `SECURITY_AUDIT_TARGET`
- `INTEGRATION_JUDGE_SCOPE`
- `WORKFLOW_OVERLAY_SOURCE`
- `WORKFLOW_OVERLAY_REPLACE_REJECTED`
- `PROVIDER_OUTAGE` (`scripts/provider_outage.py`: `op=classify|open|close|mark_label|sweep|resume outcome= …`)
- `GH_API_RETRY` (`gh_api_retry` in `scripts/gh_helpers.sh` and `scripts/gh_api_retry.py`: `outcome=retry|gave_up|permanent|skipped kind=primary|secondary|transient|permanent|breaker bucket= status= attempt=<i>/<n> wait_secs= endpoint=`)
- `REVIEW_RESOLVER_PATH_REJECTED` (`scripts/review_untrusted_workspace.py check-paths`, re-emitted by `scripts/review_conflict_resolve.sh`: `reason=unsafe_name|operator_input|excluded_component|live_safety_hook|dot_directory|unsupported_type|unsafe_file|symlink|symlink_in_path|special_file|unreadable path=<path|redacted>`; the per-path report writes unsafe entries as `unsafe<TAB><category><TAB><1|2|3+>`)
- `REVIEW_RESOLVER_PAIRED_LIVE` (`scripts/review_conflict_resolve.sh`: `live= template= outcome=paired`, `outcome=mirrored`, `outcome=skipped reason=template_markers`)
- `VALIDATION_HARNESS_SANDBOX`
- `RELEASE_MANIFEST` (`scripts/release_manifest.py`: `outcome=written files=`, `error reason=`; `scripts/release_manifest_publish.sh` in the stable release jobs: `outcome=built|uploaded|failed reason= tag=`)
- `UPDATER_MANIFEST_VERIFY` (`scripts/verify_release_manifest.py`: `outcome=ok|rejected reason= count= [path=]`; the "Verify attested release manifest" step of `update_workflows.yml`, gated by `UPDATER_VERIFY_RELEASE_MANIFEST` (default `true`): `outcome=skip reason=disabled`, `outcome=rejected reason= stage=`, `outcome=ok reason=ok tag=`)
- `UPDATER_PR_DELIVERY` (`update_workflows.yml` "Commit and push updates", gated by `UPDATER_PR_DELIVERY_ENABLED` (default `true`): `outcome=opened|refreshed|skipped|failed reason= branch= pr= auto_merge=`)
- `UPDATER_PR_VERIFY` (`update_workflows.yml` job `verify`, runs only after the PR path opened or refreshed a pull request: `outcome=passed reason=ok pr= head= auto_merge=enabled|failed|skipped_unverified`, `outcome=rejected reason= stage=`)
- `VALIDATE_CHANGED_FILES_SYNTAX`
- `VALIDATION_DISPATCH_CAP` (`scripts/orchestrate_poll_process.sh`: `outcome=redispatch|phase_cap_blocked|exhausted`, `issue=`, `cycle=`, `dispatches=`, `max=`; `exhausted` adds `last_conclusion=`)
- `STALL_RETIRED_CONFLICT_REISSUE` (`scripts/orchestrate_poll_process.sh`: `issue= pr= outcome=reissue|skip reason= retired_paths=`)
- `AUTOFIX_FORCE_RB_JUDGE_CONFLICT_SKIP` (`review_autofix.yml` gate: `pr= head= outcome=skip|run reason=host_only_marker|disabled|head_sha_unavailable|helper_missing|<comment fetch state>|parse_error|no_trusted_marker|reason_mismatch|newest_reason_mismatch marker_reason=`)
- `CONFLICT_RESOLVER_HOST_ONLY_SCOPE` (`scripts/review_conflict_prepare.sh`: `pr= outcome=classified|in_scope|unknown|skip reason= host_only= in_scope=`)
- `CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE` (`scripts/review_conflict_prepare.sh`: `pr= path= base= stages=`, `outcome=skip reason=stage_shape`)
- `CONFLICT_RESOLVER_HOST_ONLY_REISSUE` (`scripts/review_conflict_resolve.sh`: `pr= issue= paths= outcome=reissued|skip|create_failed|close_failed|untrusted_closed|metadata_unsafe reason=`)
- `STABLE_RELEASE_SWITCH` (`mark-stable.yml` and `test-and-mark-stable.yml` release job, step `Resolve stable release switch`: `enabled=true|false`; unset, empty or `true` (any letter case) publishes, `false` turns it off, any other value turns it off with a warning)
- `STABLE_RELEASE_CI_GATE` (same release jobs, step `Require successful CI lint check-run`: `outcome=pass|wait|fail reason=missing|incomplete|failed|unreadable|invalid_sha sha= runs=`; the newest github-actions `lint` check-run of a `ci.yml` run for the tested commit decides; `wait` adds `polls_left=`)
- `STABLE_RELEASE_DISABLED` (same release jobs, step `Release disabled summary`: `version= sha=`; the gate passed but `STABLE_RELEASE_ENABLED` is off, so nothing was pushed, tagged, released or dispatched)
- `AGENTS_MD_DIRECT_EDIT` (`scripts/check_agents_md_direct_edits.py`: `outcome=ok|violation|error`, `net_added=`, `count=`)
- `INTEGRATION_JUDGE_INVALID` (`scripts/orchestrate_poll_process.sh`: `pr=`, `reason=exit_<rc>|unparseable|too_long|schema`, `rc=`, `output_bytes=`)
- `INTEGRATION_JUDGE_RETRY` (`scripts/orchestrate_poll_process.sh`, stderr like `INTEGRATION_JUDGE_INVALID`: `pr=`, `reason=`, `total_dispatches=`, `lifetime_max=`)
- `AUTOFIX_FINGERPRINT_CAP_REARMED`
- `AUTOFIX_COMMIT_SKIPPED` (`scripts/review_commit_changes.sh`: `pr= reason=updater_pr_verify_job_owns_branch`; the review editor's edits on an `auto/update-workflows-<sha12>` branch are discarded, not committed, and `AUTOFIX_UPDATER_COMMIT_WITHHELD=true` keeps the editor-changes-lost detector from reporting them as lost)
- `AUTOFIX_MERGE_CONFLICT_SKIPPED` (`scripts/review_autofix_step_detect_merge_conflicts.sh`: `pr= reason=updater_pr_verify_job_owns_branch`; no conflict resolver runs on a consumer-updater PR)
- `AUTOFIX_PUSH_SKIPPED` (`review_autofix.yml` "Push all pending commits": `pr= reason=updater_pr_verify_job_owns_branch`; no review commit is pushed to a consumer-updater branch)
- `AGENTS_ASSEMBLE_V1` (`scripts/assemble_agents.py`: `outcome=assembled|noop|ok|invalid file= folded= skipped= dry_run=`, `skipped=<file> reason=<r>` per fragment that cannot fold, `error=agents_unreadable`)
- `MERGE_BASE_SYNC_MARKER` (`_pr_base_sync_marker_post` in `scripts/pr_checks_lib.sh`: `pr= head_sha= sync= outcome=posted|failed`, `outcome=skipped reason=short_head_sha`; the `ai:merge-base-sync:v1` marker comment after an accepted freshness update)
- `VALIDATE_SUPPORT_REF` (`.github/workflows/validate.yml` "Fetch workflow support files": `ref=<sha|stable|main> workflow_ref=<job workflow_ref|unknown>`)
- `SUPPORT_REF` (the "Resolve workflow support ref" steps of `plan.yml`, `clarify.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, `check_failure_triage.yml`, `cancel_on_pr_close.yml`, `sync_ai_labels.yml` and `workflow_failure_heal.yml`, consumer runs only: `ref=<40-hex pin|stable> workflow_ref=<job workflow_ref|unknown>`; the pin is used only when `toJSON(job).workflow_ref` is `shubhodeep1/coding-workflows/.github/workflows/<this file>@<40-hex>`, every other identity keeps `stable`)
- `REVIEWER_POOL_REJECTED_OUTPUT` (`run_pool_reviewer` in `scripts/review_run_reviewers.sh`: `slot= pass= attempt= model= bytes= reason= kept_bytes= truncated=true|false`; `bytes=` is the full output size and `kept_bytes=` the size of the kept file as written (at most 65536; 0 when the copy failed), one line per unsuccessful Claude pool reviewer attempt; the kept output is `rejected_<pass>_<slot>_attempt<N>.txt`)
- `SECURITY_AUDIT_CLAUDE_FALLBACK` (`scripts/security_audit.sh`: `reason=`; stdout copy of the `AI_ENGINE_FALLBACK role=SECURITY_AUDIT` reason)
- `SECURITY_AUDIT_CLAUDE_EXTRACTED` (`scripts/security_audit.sh`: `items= leading_chars= trailing_chars=`; Claude's findings array was taken from a reply with text around it)
- `SECURITY_AUDIT_CLAUDE_REJECTED` (`scripts/security_audit.sh`: `reason= bytes= chars= kept=<path|none|write_failed>`)
- `STANDALONE_CONFLICT_STUCK` (`scripts/orchestrate_poll_process.sh`: `pr= head= attempts= threshold= dispatch_rc= outcome=counted|alerted|restart|skip|write_failed|send_failed|confirmation_failed reason=`; failed writes or sends leave the marker retryable, while a failed confirmation can duplicate a page)
- `NOOP_FORCE_MERGE_GATE_ALERT` (`scripts/orchestrate_poll_process.sh`: `pr= head= gate= outcome=alerted|skip reason=`)
- `COMPREHENSIVE_RELEASE_DISPATCH_FAILED` (`scripts/orchestrate_poll_process.sh`: `tracking_issue= alerted=`)
- `INTEGRATION_CONFLICT_EPISODE_RESOLVED` (`scripts/orchestrate_poll_process.sh`: `tracking_issue=`, `base_sha=`, `previous_total=`)
- `INTEGRATION_FAILED_RECOVERY` (`scripts/orchestrate_poll_process.sh`: `tracking_issue=`, `pr=`, `outcome=recovered|skip`, `reason=final_pr_mergeable|final_pr_merged|no_final_pr|no_integration_branch|pr_unreadable|final_pr_wrong_base|final_pr_not_open|not_mergeable|fetch_failed|merge_not_clean|prior_status_unknown|state_write_failed`, `restored_status=`)
- `PLAN_REF_MISMATCH_RETRY` (`scripts/run_plan_codex.sh`: `attempt= outcome=retry|kept planning_ref_mismatch=false`)
- `POLL_PROGRESS` (`scripts/orchestrate_poll_process.sh`: `tracking_issue=`, `outcome=advanced`)
- `POLL_FOLLOW_UP` (`.github/workflows/orchestrate_poll.yml`: `outcome=request|skip`, `reason=no_progress|disabled|invalid_switch|cooldown_unavailable|invalid_tracking_issue|helper_missing`, `tracking_issue=`, `workflow=`)
- `VALIDATION_REFRESH_CLONE_RETRY` (`scripts/validation_refresh_runner.py`: `repository=`, `attempt=`, `max=`, `returncode=`, `delay_secs=`)
- `CI_TRIAGE_PR_BRANCH_ROUTING` (`scripts/resolve_integration_ref.sh`: `issue= pr= outcome=routed branch=` on stderr, `::error::... outcome=refused reason=label_missing|marker_invalid|not_an_issue|author_untrusted|pr_association_mismatch|pr_lookup_failed|pr_not_open|pr_cross_repository|head_ref_invalid|branch_missing|branch_lookup_failed|issue_read_failed|issue_parse_failed|issue_json_invalid`, exit 3)
- `CHECK_TRIAGE_BRANCH_GUARD` (`plan.yml` "Check-triage target-branch guard": `issue= enabled= check_triage=true|false|unknown resolver_status=resolved|routed|none|failed|unavailable|unknown outcome=skip|pass|blocked reason=disabled|not_check_triage|metadata_present|pr_head_routed|no_target_branch_metadata|target_branch_unverified|labels_unavailable`)
- `HISTORICAL_TRIAGE` (`.github/workflows/check-failure-triage-historical.yml`: `skip reason=already_filed|failed_run_cap`, `run run= job= log_available= head_sha=`, `tested_tree= tested_sha=`, `extracted steps= group=`, `step= kind= rc=`)
- `CHECK_TRIAGE_RESOLVED` (`scripts/check_triage_resolution.py`: `issue= pr= outcome=not_triage|disabled|already_resolved|resolved|unverified|write_failed reason= evidence_sha= traceback_jobs=`)
- `SECURITY_PASS_AUDIT_TREE` (`scripts/orchestrate_poll_process.sh` `_security_pass_prepare_audit_tree` / `_security_pass_remove_audit_tree`: `tracking_issue= head_sha= outcome=prepared|failed reason=worktree_add_failed|head_mismatch|dirty_tree|runtime_dir_unavailable`, `tracking_issue= outcome=removed|remove_failed`)
- `SYNC_MANIFEST` (`scripts/build_sync_manifest.py` and the release/updater steps: `outcome=built|uploaded|upload_failed|verified|rejected reason= version= commit= files=`)
- `SYNC_DELIVERY` (`update_workflows.yml` "Commit and push updates": `outcome=pushed|merged|auto_merge_enabled|awaiting_merge|replaced_stale_pr|failed pr= branch=ai-sync/stable version= reused=`)
- `AUTOFIX_GATE_WORKFLOW_SYNC_SKIP` (`review_autofix.yml` gate: `pr= head_ref= outcome=skipped|review files= reason=`)
- `PREPUSH_TEST_GATE` (`workflow-templates/.claude/hooks/prepush_affected_tests.py`: `phase=run outcome=pass|fail|unverified|skipped selected= ran= failures= inherited= unverified=`, `phase=config outcome=config_invalid name= fallback=`; `scripts/prepush_test_gate_run.sh`: `phase=wrapper outcome=pass|fail|unverified|skipped|error|config_invalid|warning reason=`)
- `VALIDATION_STALE_PIN_RECOVERY` (`scripts/orchestrate_poll_process.sh`: `tracking_issue= outcome=recovered|skip|label_restore_failed reason=stale_integration_pin|disabled|validation_disabled|not_harness_error|already_recovered|marker_present|no_integration_branch|default_branch_unavailable|default_branch_project|run_used_default_branch|no_evidence|default_sha_unavailable|label_write_failed|state_write_failed`)
- `CLARIFY_BASE_IMAGE_PRESEED` (`scripts/clarify_base_image_preseed.sh`: `image= outcome=present|seeded|mirror_failed|mirror_skipped|unavailable|skip reason=dockerfile_missing|unsupported_ref|invalid_mirror|helper_missing source= rc= detail= mirrors=`)
- `PLAN_SUPERSEDED_MERGED_PR` (`plan.yml` "Close superseded CI-repair issue (merged PR)" and `scripts/plan_superseded_merged_pr.py`: `issue= source= pr= head= check= outcome=superseded|rerouted|plan|skip reason=not_candidate|no_source|origin_unverified|untrusted_origin|mixed_lineage|ambiguous_source|identity_unavailable|pr_read_failed|pr_open|pr_closed_unmerged|fork_head|check_unknown|merged_check_not_passed|helper_missing|classifier_failed|comment_failed|reroute_failed|disabled label=applied|failed close=done|failed comment=posted|present`)
- `AI_ENGINE_CAPACITY_WAIT` (`scripts/ai_engine.sh` `ai_engine_fallback`: `role= reason=`; a capacity fallback with `CLAUDE_POOL_CAPACITY_WAIT_ENABLED=true`, exit 80, codex does not run; the report step adds `AI_ENGINE_FALLBACK_REPORT wait item=<kind>:<n|none> roles= label=applied|failed|skipped marker=posted|present|failed|skipped tracker=<n|none> tracker_marker=posted|present|failed alert=sent|none|unavailable` or `wait outcome=skip reason=identity_unavailable|missing_context|tracker_helper_missing`, and a trailing `waits=<n>` on its summary line)
- `STANDALONE_STALL_PROBE` (`.github/workflows/orchestrate_poll.yml` "Find active tracking issues", only when no tracking issue is open: `outcome=candidates|none labeled= state_marker= clarify_marker=`, `outcome=skip reason=disabled`, `outcome=probe_failed reason=graphql_failed|malformed`)
- `WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL` (`scripts/workflow_failure_heal_non_retryable_backfill.sh`: `pr= head= failure= run= outcome=dispatched|skip|failed reason=` (`marker=posted|failed` on dispatch; skip reasons include `head_moved`, `head_unreadable`, `pr_read_failed`, `tick_cap`, `dry_run`), `pr= outcome=skip reason=<selector reason>`, `outcome=warn reason=listing_truncated fetched= max=`, `outcome=info reason=window_rotated window= windows= total_open=`, `outcome=skip reason=disabled|invalid_switch|workflow_heal_disabled|gh_pat_missing|identity_unavailable|pr_listing_unavailable|selector_failed`, final `outcome=complete scanned= candidates= dispatched= dry_run=`)
- `FRESHNESS_CI_FAILED` (`scripts/review_freshness_ci_failed.sh`: `pr=`, `head=`, `outcome=redispatched|disabled|invalid_input|marker_failed|dispatch_failed`, `workflow=`)
- `SECURITY_PASS_AUDIT_TREE` (`scripts/orchestrate_poll_process.sh`: `tracking_issue= head_sha= outcome=prepared|failed|skipped reason=invalid_input|stale_path|worktree_add_failed|head_mismatch|worktree_not_clean|disabled`, and `judge_workspace=audit_worktree` when the exhaustion judge reads the worktree)
- `PIPELINE_PLAN_GRANT` (`.github/workflows/plan.yml` "Auto-approve clear plan" and `scripts/orchestrate_poll_process.sh` `_plan_auto_approval_gate`: `issue=`, `source=plan|poller`, `outcome=written|failed`, `reason=issue_body_write_failed|grant_body_unavailable|issue_read_failed|issue_body_changed`)
- `JUDGE_CONTEXT_CHECKOUT` (`scripts/orchestrate_poll_process.sh` `prepare_tracking_judge_checkout`, forced-checkout fallback and security-pass checkouts: `tracking=`, `outcome=forced|forced_overlay_kept|plain|trusted_refresh_failed`, `kept_target=` count of tracked files the support overlay had replaced, `removed_stale=` count of restored paths the target deleted on `outcome=forced`, `reason=trusted_support|checkout_failed` on `forced_overlay_kept` and `reason=trusted_support` on `plain`, `trusted_refreshed=` count of support files re-copied from the verified support checkout, `failed=` count of support paths that could not be replaced on `trusted_refresh_failed`, plus `reason=no_trusted_root` there when no usable verified support checkout exists)
- `PROVIDER_OUTAGE` gains `op=capacity_resume kind= item= run= outcome=dispatched|replayed|rerun|not_rerun|superseded|done|failed` and a final `outcome=idle|waiting|partial|closed|failed`, plus `op=close kind=capacity outcome=pending_waits` (issue #7370).
- `AI_ENGINE_FALLBACK_REPORT wait item=none:none` for kind `none`; `CLAUDE_POOL_HEALTH ... waiting=<n>`; `CLAUDE_CAPACITY_WAIT_TRACKER capacity_wait=true|false` (`orchestrate_poll.yml` `find_tracking`); `JUDGE_ISOLATION ... reason=claude_capacity_wait counted=false`; `AI_PHASE_GATE_V1 phase=clarify gate=capacity_wait_replay reason=verified|author_mismatch|identity_unavailable|source_unverified|verified_source`.
- `STATE_PRUNE` (`scripts/orchestrate_poll_process.sh` `prune_state_comment_chains`: `tracking_issue= deleted= failed= remaining= outcome=done|budget_exhausted|skip [reason=comments_unavailable|login_unavailable|plan_failed]`; `trigger=comment_cap` when a state write met GitHub's 2500-comment cap)
- `TRACKING_COMMENT_REPEAT` (`post_tracking_comment`: `tracking_issue= comment= count= outcome=updated|update_failed` when a repeat of a recent pipeline comment updates its count instead of posting)
- `FINGERPRINT_RESOLVER_UNION_MERGE_V1`
- `BRANCH_REBUILD_THRESHOLD_DECISION` (`scripts/orchestrate_poll_process.sh` `_attempt_branch_rebuild_after_escalation`: `tracking_issue= skip_reason=<reason|none> last_rebuild_at=<ts|none>`; one line per threshold check on an escalated integration-conflict head; `skip_reason=none` means only that the threshold and cooldown checks passed (later preflight checks, such as replay metadata, the default-branch head SHA or branch-protection metadata, can still skip the rebuild and record it in the branch-rebuild audit), `last_rebuild_at=none` means no earlier rebuild timestamp was accepted: the audit records none, the check skipped before reading the audit (`disabled`, `unsupported_branch`, `missing_escalated_at`, `invalid_current_time`, `invalid_escalated_at`, `threshold_not_met`), the audit read was attempted but returned nothing usable because memory is disabled, the read failed or it carried a warning (`audit_unavailable`), or the audit was read but its `last_rebuild_at` could not be parsed (`invalid_audit_timestamp`))
- `RB_JUDGE_STALE_HEAD` (`scripts/review_rb_judge.sh`: `pr= judged_head= live_head= action=`; the live PR head is neither the judged head nor the run's checked-out `INITIAL_HEAD_SHA` that is an ancestor of it (a push or force-push during the judge call), so the judge skipped with `judge_skip_reason=stale_head` before any comment, label, status or close. An unreadable live head refuses `fix` / `close_and_reissue` (also a merge the security severity block converts to `fix`) with `judge_skip_reason=head_unverified`, and the spot-fix reissue baseline uses `RB_JUDGED_HEAD_SHA`, or the live head when that exemption applied, because `implement.yml` accepts only a baseline equal to the closed PR's `headRefOid`)
- `CONSUMER_HEALTH` (`scripts/consumer_health_check.sh`: `skip reason=disabled|invalid_switch`; `enable repo= workflow= outcome=enabled|failed`; `issue repo= problem= outcome=opened|open_failed|closed|comment_failed|close_failed number=`; `outcome=done repos= problems= opened= closed= enabled= write_failures=` (a failed issue create or close exits 1 so the day is retried; `comment_failed` means the issue closed but its comment failed); `outcome=failed reason=templates_missing|consumers_unreadable|pipeline_login_unavailable|open_issues_unavailable|evaluate_failed`)
- `REVIEW_ISOLATION_SNAPSHOT` (`scripts/review_untrusted_workspace.py snapshot`: `oversized_skipped=<n> max_file_bytes=<bytes>`)
- `CLAUDE_POOL_MODULE_PIN` (`plan.yml` and `clarify.yml` "Pin claude-pool-token action module type", #7434 / #7476: `outcome=written`, `outcome=skip reason=present|action_dir_unavailable`, `outcome=failed reason=write_failed`; writes `{"type":"commonjs"}` as `package.json` into the support checkout's `.github/actions/claude-pool-token/` so a consumer root `package.json` with `"type": "module"` cannot make Node load the action's CommonJS `main.js` / `post.js` as ES modules; never overwrites an existing file or follows a symlink; since #7432 the action ships its own `package.json`, so the step normally logs `outcome=skip reason=present` and only writes for a staged action copy that lacks the file)
- `HEAL_SCOPE_PIPELINE_EDIT_ACCEPTED` (`scripts/implement_heal_preflight.sh`: `issue= edits=`; an edited `ai:workflow-heal` body whose whole edit history was written by the `GH_PAT` login with an unchanged scope marker was accepted)
- `PLAN_SPLIT` (`scripts/plan_split_dispatch.sh`: `issue=`, `outcome=dispatched|dispatch_unconfirmed|deduplicated|skipped`, `reason=disabled|orchestrator_managed|already_split|identity_unavailable|comments_unavailable|orchestrator_wrapper_inactive|orchestrator_wrapper_missing|default_branch_unavailable|description_too_large|label_failed|marker_failed|concurrent_split|concurrent_split_marker_failed|wrapper_without_tracking_comment|dispatch_failed|issue_json_missing|plan_missing`, `wrapper=`, `paths=`)
- `PLAN_SPLIT_LINKED` (`.github/workflows/orchestrate.yml` "Create tracking issue": `source_issue=`, `tracking_issue=`)
- `PLAN_SPLIT_RELEASED` (`.github/workflows/orchestrate.yml` "Release plan-split source issue": `source_issue=`)
- `PLAN_SPLIT_SOURCE` (`scripts/orchestrate_poll_process.sh` `close_plan_split_source_if_needed`: `tracking_issue=`, `source_issue=`, `outcome=closed|already_closed|not_split|deferred`, `reason=source_lookup_failed|close_failed`)
- `SECURITY_PASS_AUDIT_STDERR` (`scripts/orchestrate_poll_process.sh`: `tracking_issue=`, `rc=`, `lines=`; followed by the findings-JSON audit's last 40 stderr lines, each prefixed `| ` with `##[` escaped)

When `EVENTS_JSONL_ENABLED=true`, `scripts/emit_event.sh` and
`scripts/emit_event.py` append a fail-open JSONL mirror to
`.events/run-<run_id>.jsonl` after the original stable text line (or
`AI_PHASE_FAILURE_V1` comment marker) emits. The legacy text stream remains
authoritative for current grep-based tooling, and helper-internal
`EVENTS_EMIT*` diagnostics are not mirrored recursively. Phase D currently
emits only `EVENTS_EMIT_FAIL` on helper-write problems; `EVENTS_EMIT` is a
reserved additive success-path prefix for future use.

When `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`,
`scripts/transcript_archive.sh` writes a fail-open JSON envelope under
`.transcripts/<sanitized-run_id>-<sanitized-phase>-<ts>.json` from already-captured success-path
output files. Archive helper problems never fail the caller; the helper emits
only `TRANSCRIPT_ARCHIVE_FAIL` on mkdir/read/write/JSON-encode failures.

When wrapper-level nag reminders are enabled,
`scripts/nag_reminder.sh` loads phase-specific reminder text from
`prompts/_nag_reminders.txt`. Reminder-asset lookup problems fail open: the
caller continues without injection and the helper emits only
`NAG_REMINDER_LOAD_FAIL` when the prompt fragment is missing, unreadable, or
missing the requested phase key.

LOG_PREFIX.name=CODEX_ISOLATION
LOG_PREFIX.name=DEPENDENCY_PROXY
LOG_PREFIX.name=LABEL_REPAIR
LOG_PREFIX.name=LABEL_REPAIR_DIFF
LOG_PREFIX.name=LABEL_SYNC_CREATED
LOG_PREFIX.name=LABEL_SYNC_UPDATED
LOG_PREFIX.name=LABEL_SYNC_UNCHANGED
LOG_PREFIX.name=LABEL_SYNC_ERROR
LOG_PREFIX.name=LABEL_SYNC_DELETED
LOG_PREFIX.name=LABEL_SYNC_RETIRED_ABSENT
LOG_PREFIX.name=RETIRED_FILE_REMOVED
LOG_PREFIX.name=RETIRED_FILE_KEPT_MODIFIED
LOG_PREFIX.name=AUTOFIX_PEER_CHECK
LOG_PREFIX.name=AUTOFIX_DISPATCH_SKIPPED
LOG_PREFIX.name=AUTOFIX_DISPATCH_ISSUED
LOG_PREFIX.name=AUTOFIX_GATE_SKIP
LOG_PREFIX.name=REVIEW_HEAD_GATE
LOG_PREFIX.name=AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD
LOG_PREFIX.name=AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED
LOG_PREFIX.name=AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE
LOG_PREFIX.name=AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED
LOG_PREFIX.name=AI_PHASE_FAILURE_V1
LOG_PREFIX.name=AI_PHASE_GATE_V1
LOG_PREFIX.name=WORKFLOW_SCENARIO_TRACE_WRITTEN
LOG_PREFIX.name=WORKFLOW_SCENARIO_TRACE_PARSE_FAIL
LOG_PREFIX.name=RETARGET_MERGED_BASE
LOG_PREFIX.name=STANDALONE_AUTO_DECIDE
LOG_PREFIX.name=AI_ENGINE_SELECTED
LOG_PREFIX.name=AI_ENGINE_FALLBACK
LOG_PREFIX.name=AI_ENGINE_FALLBACK_REFUSED
LOG_PREFIX.name=AI_ENGINE_FALLBACK_REPORT
LOG_PREFIX.name=REVIEW_UTILITY_ISOLATION
LOG_PREFIX.name=NEEDS_HUMAN
LOG_PREFIX.name=OPERATOR_STEP_TICK
LOG_PREFIX.name=CLAUDE_POOL
LOG_PREFIX.name=CLAUDE_POOL_HEALTH
LOG_PREFIX.name=SANDBOX_IMAGE
LOG_PREFIX.name=AI_ENGINE_PROJECT_LABEL
LOG_PREFIX.name=AI_ENGINE_PR_LABEL
LOG_PREFIX.name=SINGLE_ISSUE_SECURITY_PASS
LOG_PREFIX.name=SECURITY_HOLD_SWEEP
LOG_PREFIX.name=RECLARIFY_UNROUTED
LOG_PREFIX.name=ACTIVATION_VERIFY
LOG_PREFIX.name=UNBLOCK_SCAN
LOG_PREFIX.name=UNBLOCK_JUDGE
LOG_PREFIX.name=UNBLOCK_PROJECT
LOG_PREFIX.name=UNBLOCK_HANDOVER
LOG_PREFIX.name=UNBLOCK_BULK_DELETE_OVERRIDE
LOG_PREFIX.name=JUDGE_INTERIM_PASS_OK
LOG_PREFIX.name=JUDGE_INTERIM_PASS_FAIL
LOG_PREFIX.name=JUDGE_INTERIM_PRIORS_MERGED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_SYNTHESISED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_PRESENT_FAILED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_PRESENT_PASSED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_SANDBOX
LOG_PREFIX.name=REISSUE_BASELINE_PRESERVED
LOG_PREFIX.name=REISSUE_BASELINE_DISCARDED
LOG_PREFIX.name=REISSUE_MODE
LOG_PREFIX.name=REISSUE_FILES_TOUCHED_UNION
LOG_PREFIX.name=REISSUE_FILES_TOUCHED_NEW_OUTPUTS
LOG_PREFIX.name=REVIEW_BLOCKED_FIX_SCOPE_ACCEPTED
LOG_PREFIX.name=REVIEW_BLOCKED_FIX_SCOPE_REJECTED
LOG_PREFIX.name=REVIEW_BLOCKED_FIX_TARGET_VERIFIED
LOG_PREFIX.name=REVIEW_BLOCKED_FIX_TARGET_REJECTED
LOG_PREFIX.name=REVIEW_BLOCKED_HEAD_IDENTITY_REJECTED
LOG_PREFIX.name=REISSUE_ORCHESTRATOR_METADATA_CARRIED
LOG_PREFIX.name=REISSUE_ORCHESTRATOR_METADATA_ABSENT
LOG_PREFIX.name=FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_STATE_SELFHEAL_V1
LOG_PREFIX.name=FINAL_MERGE_INELIGIBILITY_ALERT_SENT
LOG_PREFIX.name=EAGER_DRAFT_PR_CREATED
LOG_PREFIX.name=APPLY_ANALYSIS_SKIPPED
LOG_PREFIX.name=APPLY_ANALYSIS_DISPATCHED
LOG_PREFIX.name=APPLY_ANALYSIS_DOC_GONE
LOG_PREFIX.name=APPLY_ANALYSIS_IN_FLIGHT_DOCS
LOG_PREFIX.name=WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED
LOG_PREFIX.name=APPLY_ANALYSIS_CANDIDATES
LOG_PREFIX.name=PROMOTE_CYCLE_SKIPPED
LOG_PREFIX.name=PROMOTE_CYCLE_FAILED
LOG_PREFIX.name=PROMOTE_CYCLE_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_VERIFICATION_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_DEFERRED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_HOLD
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_DONE
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_FAILED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_SKIPPED
LOG_PREFIX.name=COMPREHENSIVE_VERIFYING_COMPLETE
LOG_PREFIX.name=COMPREHENSIVE_MARKER_UNTRUSTED
LOG_PREFIX.name=TRACKING_ISSUE_LABEL_APPLIED
LOG_PREFIX.name=TRACKING_ISSUE_BINDING_FAILED
LOG_PREFIX.name=RELEASE_STALE_TIP
LOG_PREFIX.name=RELEASE_UNTESTED_HEAD
LOG_PREFIX.name=RELEASE_CI_GATE
LOG_PREFIX.name=TRACKING_ISSUE_COMMENT_POSTED
LOG_PREFIX.name=AUTO_RELEASE_SKIPPED
LOG_PREFIX.name=AUTO_RELEASE_DISPATCHED
LOG_PREFIX.name=IMPLEMENT_BLOCKED_TERMINALIZED
LOG_PREFIX.name=EAGER_DRAFT_PR_PROMOTED
LOG_PREFIX.name=INTEGRATION_STALE_ALERT_SENT
LOG_PREFIX.name=STALL_INFLIGHT_DIRECT_CHECK
LOG_PREFIX.name=HARNESS_ERROR_DETECTED
LOG_PREFIX.name=FORCE_MERGE_BYPASS
LOG_PREFIX.name=BACKPRESSURE_TRIGGERED
LOG_PREFIX.name=BACKPRESSURE_CLEARED
LOG_PREFIX.name=RECOVERY_BUDGET_ACCOUNTING
LOG_PREFIX.name=VALIDATION_DISCOVERY_STARTED
LOG_PREFIX.name=VALIDATION_DISCOVERY_AGREE
LOG_PREFIX.name=VALIDATION_DISCOVERY_DISAGREE
LOG_PREFIX.name=VALIDATION_DISCOVERY_PR_OPENED
LOG_PREFIX.name=VALIDATION_DISCOVERY_PR_REUSED
LOG_PREFIX.name=VALIDATION_DISCOVERY_FAILED
LOG_PREFIX.name=VALIDATION_DISCOVERY_SKIPPED_DEDUP
LOG_PREFIX.name=VALIDATION_DISCOVERY_SKIPPED_DISABLED
LOG_PREFIX.name=VALIDATION_DISCOVERY_SKIPPED_BUDGET
LOG_PREFIX.name=VALIDATION_DISCOVERY_DRY_RUN
LOG_PREFIX.name=VALIDATE_TRUSTED_TEMPLATE_OVERRIDE
LOG_PREFIX.name=VALIDATE_TRUSTED_TEMPLATES
LOG_PREFIX.name=VALIDATE_TRUSTED_RENDERER_OVERRIDE
LOG_PREFIX.name=VALIDATE_TRUSTED_RENDERER
LOG_PREFIX.name=VALIDATE_TRUSTED_SUPPORT_OVERRIDE
LOG_PREFIX.name=VALIDATE_TRUSTED_SUPPORT
LOG_PREFIX.name=VALIDATION_HARNESS_SANDBOX_DAILY
LOG_PREFIX.name=REVIEWER_RISK_TIER
LOG_PREFIX.name=REVIEWER_FILTER_SKIP
LOG_PREFIX.name=REVIEWER_FAILBACK
LOG_PREFIX.name=REVIEWER_FAILBACK_UNMAPPED
LOG_PREFIX.name=REVIEWER_HEALTH
LOG_PREFIX.name=EDITOR_REVIEWER_CHECKSUM_UNVERIFIED
LOG_PREFIX.name=EDITOR_REVIEWER_CHECKSUM_SUMMARY
LOG_PREFIX.name=RE_REVIEW_SKIP
LOG_PREFIX.name=CONTEXT_BUDGET_WARN
LOG_PREFIX.name=CODEX_HEARTBEAT
LOG_PREFIX.name=BREAK_GLASS
LOG_PREFIX.name=WRITE_GUARD_BLOCK
LOG_PREFIX.name=WRITE_GUARD_CONFIG_ERROR
LOG_PREFIX.name=WRITE_GUARD_BYPASS_ENV
LOG_PREFIX.name=DRIFT_SCAN_START
LOG_PREFIX.name=DRIFT_SCAN_RETRY
LOG_PREFIX.name=DRIFT_SCAN_DIFF
LOG_PREFIX.name=DRIFT_SCAN_OK
LOG_PREFIX.name=DRIFT_SCAN_ERROR
LOG_PREFIX.name=SECURITY_PASS_STARTED
LOG_PREFIX.name=SECURITY_PASS_FINAL_PR_SYNC
LOG_PREFIX.name=SMOKE_FIXTURE_PROJECT
LOG_PREFIX.name=TG_NOTIFY_SMOKE_SILENCED
LOG_PREFIX.name=ORCHESTRATOR_STATE_AUTHOR_FILTER
LOG_PREFIX.name=SECURITY_PASS_SCOPE
LOG_PREFIX.name=SECURITY_PASS_CLEAN
LOG_PREFIX.name=SECURITY_PASS_BLOCKED
LOG_PREFIX.name=SECURITY_PASS_FIX_ISSUE_CREATED
LOG_PREFIX.name=SECURITY_PASS_FIX_ISSUE_REISSUED
LOG_PREFIX.name=SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED
LOG_PREFIX.name=SECURITY_PASS_FIX_MERGED_EVIDENCE
LOG_PREFIX.name=SECURITY_PASS_CYCLE_BUDGET_RESET
LOG_PREFIX.name=SECURITY_PASS_FAILED
LOG_PREFIX.name=SECURITY_PASS_SKIPPED_DISABLED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_SKIPPED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_FAILED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_DECIDED
LOG_PREFIX.name=SECURITY_PASS_WAIVED
LOG_PREFIX.name=SECURITY_PASS_WAIVE_REJECTED
LOG_PREFIX.name=SECURITY_PASS_WAIVED_SUPPRESSED
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUP_DEFERRED
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUPS_FILED
LOG_PREFIX.name=SECURITY_PASS_AUTO_RESET
LOG_PREFIX.name=SECURITY_PASS_AUTO_RESET_SKIPPED
LOG_PREFIX.name=STAGED_SUPPORT_LATCH_RELEASED
LOG_PREFIX.name=STAGED_SUPPORT_LATCH_SKIP
LOG_PREFIX.name=STAGED_SUPPORT_LATCH_RELEASE_SKIPPED
LOG_PREFIX.name=ORCHESTRATOR_ENGINE_SHA
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED
LOG_PREFIX.name=VALIDATION_RUN_ATTRIBUTION
LOG_PREFIX.name=CI_CANCELLED_RERUN
LOG_PREFIX.name=SEMBLE_QUERY
LOG_PREFIX.name=SEMBLE_FALLBACK
LOG_PREFIX.name=SEMBLE_BOOTSTRAP
LOG_PREFIX.name=SERENA_QUERY
LOG_PREFIX.name=SERENA_FALLBACK
LOG_PREFIX.name=SERENA_PROBE
LOG_PREFIX.name=TASK_STATE_UNBLOCK
LOG_PREFIX.name=TASK_STATE_WRITE_FAIL
LOG_PREFIX.name=EVENTS_EMIT
LOG_PREFIX.name=EVENTS_EMIT_FAIL
LOG_PREFIX.name=NAG_REMINDER_LOAD_FAIL
LOG_PREFIX.name=TRANSCRIPT_ARCHIVE_FAIL
LOG_PREFIX.name=IDENTITY_REINJECT_PARSE_FAIL
LOG_PREFIX.name=drift-audit:
LOG_PREFIX.name=CHECK_TRIAGE
LOG_PREFIX.name=WORKFLOW_HEAL_REPORT
LOG_PREFIX.name=WORKFLOW_HEAL_AUTOFIX_REPORT
LOG_PREFIX.name=WORKFLOW_HEAL_PHASE_REPORT
LOG_PREFIX.name=WORKFLOW_HEAL_PR_RECONCILE
LOG_PREFIX.name=WORKFLOW_HEAL
LOG_PREFIX.name=WORKFLOW_LOG_FORK_EXCLUDED
LOG_PREFIX.name=HEAL_ISOLATED_EDITOR
LOG_PREFIX.name=HEAL_SCOPE_REFUSED
LOG_PREFIX.name=WORKFLOW_HEAL_EVIDENCE
LOG_PREFIX.name=AUTOFIX_FINGERPRINT
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_TRIPPED
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED
LOG_PREFIX.name=NOOP_RECOVERY_SKIP_FINGERPRINT_CAP
LOG_PREFIX.name=NOOP_RECOVERY_FINGERPRINT_CAP_STALE_SUPPORT
LOG_PREFIX.name=REVIEW_EDITOR_PREFLIGHT
LOG_PREFIX.name=IMPLEMENT_AUTOMATION_PATH_GUARD
LOG_PREFIX.name=STAGE_MAIN_PINNED_DIVERGENCE
LOG_PREFIX.name=WORKTREE_REGISTER
LOG_PREFIX.name=WORKTREE_DEREGISTER
LOG_PREFIX.name=WORKTREE_GC
LOG_PREFIX.name=WORKTREE_REGISTRY_REBUILD
LOG_PREFIX.name=WORKTREE_REGISTER_INVALID_NAME
LOG_PREFIX.name=WORKTREE_REGISTER_FAIL
LOG_PREFIX.name=WORKTREE_DEREGISTER_FAIL
LOG_PREFIX.name=opencode_agent_failure
LOG_PREFIX.name=MODEL_CATALOG_BACKFILL
LOG_PREFIX.name=AUTOFIX_FAILURE_HEADLINE
LOG_PREFIX.name=CLAUDE_FIXER_AUTO_MERGE
LOG_PREFIX.name=RB_JUDGE_ISOLATION
LOG_PREFIX.name=CONSOLIDATOR_ISOLATION
LOG_PREFIX.name=REVIEW_SANDBOX_CLEANUP
LOG_PREFIX.name=REVIEW_SANDBOX_BUILD
LOG_PREFIX.name=JUDGE_ISOLATION
LOG_PREFIX.name=JUDGE_ENGINE_LABELS
LOG_PREFIX.name=SECURITY_AUDIT_TARGET
LOG_PREFIX.name=INTEGRATION_JUDGE_SCOPE
LOG_PREFIX.name=WORKFLOW_OVERLAY_SOURCE
LOG_PREFIX.name=WORKFLOW_OVERLAY_REPLACE_REJECTED
LOG_PREFIX.name=PROVIDER_OUTAGE
LOG_PREFIX.name=GH_API_RETRY
LOG_PREFIX.name=REVIEW_RESOLVER_PATH_REJECTED
LOG_PREFIX.name=REVIEW_RESOLVER_PAIRED_LIVE
LOG_PREFIX.name=VALIDATION_HARNESS_SANDBOX
LOG_PREFIX.name=RELEASE_MANIFEST
LOG_PREFIX.name=UPDATER_MANIFEST_VERIFY
LOG_PREFIX.name=UPDATER_PR_DELIVERY
LOG_PREFIX.name=UPDATER_PR_VERIFY
LOG_PREFIX.name=VALIDATE_CHANGED_FILES_SYNTAX
LOG_PREFIX.name=VALIDATION_DISPATCH_CAP
LOG_PREFIX.name=STALL_RETIRED_CONFLICT_REISSUE
LOG_PREFIX.name=AUTOFIX_FORCE_RB_JUDGE_CONFLICT_SKIP
LOG_PREFIX.name=CONFLICT_RESOLVER_HOST_ONLY_SCOPE
LOG_PREFIX.name=CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE
LOG_PREFIX.name=CONFLICT_RESOLVER_HOST_ONLY_REISSUE
LOG_PREFIX.name=STABLE_RELEASE_SWITCH
LOG_PREFIX.name=STABLE_RELEASE_CI_GATE
LOG_PREFIX.name=STABLE_RELEASE_DISABLED
LOG_PREFIX.name=AGENTS_MD_DIRECT_EDIT
LOG_PREFIX.name=INTEGRATION_JUDGE_INVALID
LOG_PREFIX.name=INTEGRATION_JUDGE_RETRY
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_REARMED
LOG_PREFIX.name=AUTOFIX_COMMIT_SKIPPED
LOG_PREFIX.name=AUTOFIX_MERGE_CONFLICT_SKIPPED
LOG_PREFIX.name=AUTOFIX_PUSH_SKIPPED
LOG_PREFIX.name=AGENTS_ASSEMBLE_V1
LOG_PREFIX.name=MERGE_BASE_SYNC_MARKER
LOG_PREFIX.name=VALIDATE_SUPPORT_REF
LOG_PREFIX.name=SUPPORT_REF
LOG_PREFIX.name=REVIEWER_POOL_REJECTED_OUTPUT
LOG_PREFIX.name=SECURITY_AUDIT_CLAUDE_FALLBACK
LOG_PREFIX.name=SECURITY_AUDIT_CLAUDE_EXTRACTED
LOG_PREFIX.name=SECURITY_AUDIT_CLAUDE_REJECTED
LOG_PREFIX.name=STANDALONE_CONFLICT_STUCK
LOG_PREFIX.name=NOOP_FORCE_MERGE_GATE_ALERT
LOG_PREFIX.name=COMPREHENSIVE_RELEASE_DISPATCH_FAILED
LOG_PREFIX.name=INTEGRATION_CONFLICT_EPISODE_RESOLVED
LOG_PREFIX.name=INTEGRATION_FAILED_RECOVERY
LOG_PREFIX.name=PLAN_REF_MISMATCH_RETRY
LOG_PREFIX.name=POLL_PROGRESS
LOG_PREFIX.name=POLL_FOLLOW_UP
LOG_PREFIX.name=VALIDATION_REFRESH_CLONE_RETRY
LOG_PREFIX.name=CI_TRIAGE_PR_BRANCH_ROUTING
LOG_PREFIX.name=CHECK_TRIAGE_BRANCH_GUARD
LOG_PREFIX.name=HISTORICAL_TRIAGE
LOG_PREFIX.name=CHECK_TRIAGE_RESOLVED
LOG_PREFIX.name=SECURITY_PASS_AUDIT_TREE
LOG_PREFIX.name=SYNC_MANIFEST
LOG_PREFIX.name=SYNC_DELIVERY
LOG_PREFIX.name=AUTOFIX_GATE_WORKFLOW_SYNC_SKIP
LOG_PREFIX.name=PREPUSH_TEST_GATE
LOG_PREFIX.name=VALIDATION_STALE_PIN_RECOVERY
LOG_PREFIX.name=CLARIFY_BASE_IMAGE_PRESEED
LOG_PREFIX.name=PLAN_SUPERSEDED_MERGED_PR
LOG_PREFIX.name=AI_ENGINE_CAPACITY_WAIT
LOG_PREFIX.name=STANDALONE_STALL_PROBE
LOG_PREFIX.name=WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL
LOG_PREFIX.name=FRESHNESS_CI_FAILED
LOG_PREFIX.name=SECURITY_PASS_AUDIT_TREE
LOG_PREFIX.name=PIPELINE_PLAN_GRANT
LOG_PREFIX.name=JUDGE_CONTEXT_CHECKOUT
LOG_PREFIX.name=CLAUDE_CAPACITY_WAIT_TRACKER
LOG_PREFIX.name=STATE_PRUNE
LOG_PREFIX.name=TRACKING_COMMENT_REPEAT
LOG_PREFIX.name=FINGERPRINT_RESOLVER_UNION_MERGE_V1
LOG_PREFIX.name=BRANCH_REBUILD_THRESHOLD_DECISION
LOG_PREFIX.name=RB_JUDGE_STALE_HEAD
LOG_PREFIX.name=CONSUMER_HEALTH
LOG_PREFIX.name=REVIEW_ISOLATION_SNAPSHOT
LOG_PREFIX.name=CLAUDE_POOL_MODULE_PIN
LOG_PREFIX.name=HEAL_SCOPE_PIPELINE_EDIT_ACCEPTED
LOG_PREFIX.name=PLAN_SPLIT
LOG_PREFIX.name=PLAN_SPLIT_LINKED
LOG_PREFIX.name=PLAN_SPLIT_RELEASED
LOG_PREFIX.name=PLAN_SPLIT_SOURCE
LOG_PREFIX.name=SECURITY_PASS_AUDIT_STDERR

---

## Label-repair contradiction policy (current branch)

The active poller loop uses `reconcile_managed_issue_labels` for current-wave
managed issues and logs `LABEL_REPAIR*` diagnostics. The richer
contradiction-evidence helpers in `scripts/orchestrate_lib.py`
(`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`,
`resolve_label_repair_evidence`) are contract/reserved and not yet wired
into poller reconciliation.

---

## DigitalOcean resources

Registry of DigitalOcean resource IDs relevant to this repo, per CLAUDE.md
§22.C. Interactive Claude Code sessions read IDs from this table instead of
asking the user; when a needed ID is missing, the session asks once and then
records it here in the same PR/commit. Consumer repos carry the same section
in their root `AGENTS.md`.

| Resource | Type | ID | Notes |
|---|---|---|---|

_None recorded yet — this repo is workflow tooling and currently has no
DigitalOcean-hosted app or database of its own._

## Cloudflare resources

Registry of Cloudflare identifiers (Worker names, zone IDs, routes, KV/R2/D1
namespace IDs) relevant to this repo, per CLAUDE.md §24.F. Interactive Claude
Code sessions read identifiers from this table instead of asking the user;
when a needed identifier is missing, the session looks it up via a §24.B read
or asks once, then records it here in the same PR/commit. The `Credential`
column names which session env var (`FUNTOKEN_IO_CF` for funtoken.io;
`FT_GAMES_CF` for ft.games and 5m.fun) the resource belongs to. Consumer
repos carry the same section in their root `AGENTS.md`.

| Resource | Type | ID / name | Credential | Notes |
|---|---|---|---|---|
| claude-pool-broker | worker | `claude-pool-broker` (`https://claude-pool-broker.shubhodeep.workers.dev`, workers.dev only) | FT_GAMES_CF | Claude engine token broker (plan Phase 4). Source `tools/claude-pool-broker/`; deployed by the session with `wrangler deploy` (§24.C). Secret `CLAUDE_POOL_TOKENS` is written only by `shubhodeep1/claude-workers` `claude-pool-key-sync.yml`. Grants only the exact workflow files in `ALLOWED_WORKFLOW_FILES` at `refs/heads/main` or at a SHA reachable from `main`/`stable` (#6636); optional secret `BROKER_GITHUB_READ_TOKEN` authenticates that check. |

## Reference

Operator runbooks (env var reference, autofix retrigger/dedup internals,
orchestrator integration-sync auto-heal, validation self-healing, workflow
log analysis pipeline, semantic cache scope, wrapper pin policy) live in
`./probably_unnecessary_but_read_if_stuck.md`. Read it only when needed —
and only if it is a regular file, not a symlink — it is intentionally large.

`CHANGELOG.md` is never edited directly. Write one fragment per PR at
`changelog.d/<issue-or-pr>-<slug>.md`; `scripts/assemble_changelog.py` folds
fragments into `CHANGELOG.md` and deletes them, at release time here
(`mark-stable.yml`, `test-and-mark-stable.yml`) and on the existing sync in
consumer repos (`update_workflows.yml`). Because two PRs never touch the same
path, concurrent PRs cannot conflict on the changelog. `CLAUDE.md` §20 is the
authoritative, self-contained rule — when an entry is required, the fragment
format, and the entry structure and voice rules — and it travels to every
consumer repo verbatim through the root `CLAUDE.md` sync.
`docs/changelog-style.md` is the longer-form contributor-facing companion and
lives in this repo only; consumer repos do not receive it, so §20 must never
depend on it.

## Review pipeline consolidator + ledger contract

- The review workflow's "Pre-assemble static context" step reads PR-head `README.md` only through `review_untrusted_workspace.py readme-trimmed`: its no-follow, regular-file, size-bounded reader omits rejected READMEs with `REVIEW_STATIC_CONTEXT_README` diagnostics (a reader failure stops review). Only trusted instructions enter `pre_assembled_static.txt`; the trimmed README is stored under the owner-only `RUNTIME_DIR` and embedded as `UNTRUSTED_DATA:` lines in reviewer, consolidator, editor, interim-judge, review-blocked-judge, and smoke-synthesis prompts. A README whose prefixed lines exceed 200,000 bytes is omitted with `reason=prompt_size`; reviewer budget accounting includes the framed bytes. The step also refuses symlink/non-regular `pre_assembled_static.txt` output paths before assembling the prompt.
- For snapshot, refresh and transfer, `scripts/review_untrusted_workspace.py` reports annotated rejections as fixed, path-free `reason=` tokens; unsafe directories also report a fixed `category=` and bucketed `depth=`. The review result is refused before host edits on prevalidation failure. Unannotated errors retain their existing legacy reason codes or `unknown`, never an untrusted path.
- Every other host-side prompt assembler routes `README.md` through `scripts/build_static_context.sh readme`. The shared reader uses `lstat` plus `O_NOFOLLOW`, rejects non-regular, set-ID, larger-than-2-MiB, or over-200,000-prefixed-byte files, and emits accepted content only as `UNTRUSTED_DATA:` lines; rejected files log `STATIC_CONTEXT_README` and are omitted, while read failures stop assembly. The `readme` phase requires only `README.md`; the clarify/plan helper and both orchestrate workflow assemblers refuse a symlink or non-regular output path before writing static context.
- Review-pipeline helper stages are fail-open by contract. Floor rules, consolidator, parser, and ledger failures degrade to empty/advisory local artifacts and do not block the editor or reviewer loop.
- `reviewer_bundle.txt` is the authoritative findings source. `review_issues.txt` and `ledger_status.txt` are advisory only and may not suppress valid raw-bundle findings.
- `floor_tags.txt` is the only non-skippable advisory channel: findings promoted there must be fixed or explicitly rejected with reason.
- The consolidator never gates. Empty `consolidator_raw.txt`, parser failure, uncovered anchors, or malformed prior-ledger state must not stop review/autofix.
- Editor prompts use the grep-friendly override convention `CONSOLIDATOR_OVERRIDDEN: <issue_id> — <reason>` inside the "Ignored suggestions" section when the editor intentionally rejects advisory consolidator guidance. Use `no-issue-id` when the parsed advisory issue has no stable id.
- Ledger identity is per-PR and stable across iterations via `REVIEW_LEDGER_PATH`. Status contract: `NEW`, `PERSISTING`, `FIXED`, `RESURGENT`, `accepted-residual`.
- `REVIEW_LEDGER_PERSIST_LIMIT` controls the `PERSISTING -> accepted-residual` transition. Once the threshold is reached, `review_issues.txt` is rewritten to residual stubs while the durable ledger retains the full history.
- Review-ledger cache restore/save steps use the run-independent `${RUNNER_TEMP}/review-ledger-cache/<owner>/<repo>/pr-<N>/` staging root and copy to/from the active workspace. The repository/PR scope is part of the cache path-version contract: it must stay byte-identical between both review saves and the validate behavioural-smoke restore while isolating concurrent PRs on persistent self-hosted runners.
- Validate-hints caching uses a separate run-independent `${RUNNER_TEMP}/validate-hints-cache/<owner>/<repo>/<discovery-fingerprint>/` staging root. `validate.yml` stages restored hints into the active workspace before validation and stages generated hints back after `validate_process.sh`, preserving exact-key invalidation without embedding the per-run workspace in the cache version.
- The ≥2-reviewer floor rule is non-overridable at classification time: `scripts/review_floor_rules.sh` promotes same-file, nearby findings from distinct reviewers into `FLOOR_MULTI_REVIEWER`, and those tags remain non-skippable even if the consolidator down-ranks the issue.
- The review-autofix reviewer pass remains model-diversity-first. The consolidator's seven lenses are this repo's equivalent of Cloudflare's seven specialised review sub-agents; the pipeline does not run one fixed model per lens.
- Additive Phase M note: `prompts/review-consolidator.txt` now appends an eighth `DOCS COVERAGE (DIATAXIS)` lens after those original seven. The first seven lens names and order stay byte-for-byte stable; the new lens is advisory-only (`SEVERITY: low`, normally `CLASSIFICATION: nice-to-have`), is grounded in reviewer evidence plus touched files for user-visible changes, and names only still-missing `Reference` / `How-to` / `Tutorial` / `Explanation` updates (or `Docs coverage: complete` when already covered).
- Reviewer prompts now carry explicit anti-rules in both `prompts/review-reviewer-checklist.txt` (`WHAT NOT TO FLAG` under each lens) and the shared `COMMON ANTI-RULES` block rendered by `scripts/review_run_reviewers.sh`.
- `scripts/review_run_reviewers.sh` also carries the `lite | standard | full` review-tier resolver, on by default (`REVIEW_TIER_RESOLVER_ENABLED=true`). `lite` (1 reviewer) is any diff of at most `REVIEW_TIER_LITE_MAX_LOC` lines that touches no protected path; `standard` (4 reviewers) is any diff of at most `REVIEW_TIER_STANDARD_MAX_LOC` lines in any folder, including small protected diffs; `full` is everything larger plus the force-review and fail-open tier. Protected paths are the deterministic skip gate's list (`PROTECTED_SKIP_SUPPRESSED` in `review_autofix.yml`), checked on both sides of renames; `tests/test_review_autofix_review_pipeline_contract.py` keeps the two lists identical. With `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` and `REVIEW_TIER_LITE_REVIEWER_SLUG` both empty (the defaults), `standard` draws four reviewers and `lite` draws one from the whole `REVIEWER_MODELS` panel by the lowest `sha256("<PR number>:<model>")`, so every panel model can run on any tier and a PR keeps the same reviewers across rounds and reruns. A repo that sets the standard list pins `standard` to it, and an unpinned `lite` then draws from that list. Set either variable to pin reviewers. `AUTOFIX_SKIP_*` fast paths stay authoritative, `[force-review]` / `force-review` still force full review, a full panel forced by the risk-tier resolver below (`REVIEWER_RISK_TIER_FORCED_FULL`) is kept (`reason=risk_tier_forced_full`), a random pick that returns too few reviewers fails open to the full panel (`reason=random_reviewer_pick_failed`), and `lite` reuses `REVIEW_CONSOLIDATOR_ENABLED=0` to skip the consolidator.
- `scripts/review_run_reviewers.sh` can classify a PR into `trivial | lite | full` reviewer tiers from reviewer-visible diff LOC/file counts, with `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` forcing `full` on sensitive paths. Default tier fan-out follows the live `REVIEWER_MODELS` order from `.github/workflows/review_autofix.yml`: trivial = first reviewer, lite = first two reviewers, full = the complete configured set.
- A sole active reviewer must succeed: a skipped Mistral slot or context overflow retries with the live `openai/gpt-6-luna` slot when available, then fails closed if no reviewer succeeds. This applies even with size tiers disabled and a single reviewer selected by the risk tier; the skip-only fail-open path remains for multi-reviewer runs.
- `scripts/review_filter_uninteresting_files.sh` strips low-signal lock/generated/minified paths before reviewer fan-out and emits `REVIEWER_FILTER_SKIP: <path> <reason>` for each skipped file. Default exemptions remain `db/contracts/**`, `**/migrations/**`, and `**/migrate/**`.
- `.github/workflows/review_autofix.yml` now runs a fail-open local slop-scan preflight (gated by `SLOP_SCAN_ENABLED`, default `true`) on PR-changed `scripts/*.py`, `scripts/*.sh`, and `validation/**/*.sh` Python heredocs. It writes `.ai/slop_scan/findings.json`, feeds that JSON to reviewer and consolidator prompts as advisory untrusted context, and removes the runtime artifact before commit-producing steps so it cannot leak into staged changes.
- Consumer-repo review commits snapshot untracked paths before the editor runs in `PRE_EDITOR_UNTRACKED_FILE`. `scripts/review_commit_changes.sh` removes paths that were already untracked plus pipeline-owned artifacts, records removals in `REVIEW_REMOVED_NEW_FILES_FILE`, and preserves other editor-created files for the existing staging and write-guard path; a missing snapshot retains the legacy delete-all fallback.
- `scripts/review_agents_md_materiality.sh` is deterministic-path-glob v1: it writes a JSON result payload plus a non-blocking PR comment headed `## AI Materiality Advisory` when materiality is `high` or `medium` and root `agents.md` is unchanged. `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` is reserved only; enabling it still does not trigger a model call in the current shipped script.
- When `REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED=true`, `scripts/review_consolidate.sh` feeds that helper JSON into the consolidator prompt as advisory untrusted context. This is the Lens 7 companion to the separate advisory comment path controlled by `AGENTS_MD_MATERIALITY_ENABLED`. Lens 7 (`NAMING / BACKWARD COMPATIBILITY`) may then emit a default-`high` `AGENTS.md materiality` finding when operator-visible structural changes leave root `agents.md` unchanged, but downgrades or omits it when equivalent touched docs already cover the behavior.
- The deterministic review skip requires a complete paginated `/pulls/{n}/files` list for both small-diff and doc-only candidates, matched against the existing PR-details `changed_files` count. Missing/malformed/empty/partial responses and GitHub's 3,000-file ceiling route to review. Both names of a rename are checked; nested or case-variant agent instructions and automation paths (`.github/`, `.claude/`, `scripts/`, `prompts/`, `workflow-templates/`, `validation/`, `ai-memory/`, `db/contracts/`) plus root build/dependency/lint config suppress skip independently of `AGENTS_MD_MATERIALITY_ENABLED`. Benign docs and small code changes still qualify when evidence is complete; the head-bound merge check remains in place.
- `REVIEW_LEDGER_REREVIEW_ENABLED` gates consolidator-side suppression of repeated `accepted-residual` / `won't-fix` findings from the existing review ledger and the review-blocked judge's ledger-fed prior-round decision input. `scripts/review_rb_judge.sh` renders that `=== BEGIN PRIOR ROUND DECISIONS ===` block via `render_review_rb_prior_round_decisions_file`, and `prompts/mode-judge-review-blocked.txt` treats it as advisory history rather than fresh reviewer evidence.
- `REVIEWER_CIRCUIT_BREAKER_ENABLED` persists reviewer health under `.ai/review_runtime/pr-<PR>/reviewer_health_state.json`. Retryable reviewer failures first retry with cheaper reasoning, then consult `scripts/reviewer_failback_chains.json`; unmapped reviewers fail open via `REVIEWER_FAILBACK_UNMAPPED`. The `anthropic/claude-sonnet-5.5` slot has no chain entry because it never runs on OpenRouter: it runs on the Claude account pool (`PANEL_REVIEWER`), retries once on `REVIEWER_POOL_FALLBACK_MODEL` (default `claude-haiku-5-5`) in the pool, and ends `skipped_pool` when the pool is unavailable. `mistralai/mistral-small-2603` (262K-token window, on the panel from PR #6438 until the 2026-10 panel refresh gave its slot to `anthropic/claude-sonnet-5.5`) still has no same-family failback chain. When any slot other than `openai/gpt-6-luna` is the sole active reviewer, a `skipped_unmapped`, `skipped_open` or `skipped_pool` slot, or a reported context overflow, retries once with `openai/gpt-6-luna` if it is in the live roster and its circuit breaker has not marked it `skip_open`; a budget-skipped GPT retry after either case requests soft-deadline partial finalize, while other unsuccessful single-reviewer runs fail rather than passing with zero successes. This includes a sole reviewer selected by the risk tier when size tiers are disabled. Multi-reviewer runs retain the skip-only fail-open behavior; an overflowing slot fails while other reviewers can succeed. The live-roster mapping file covers `z-ai/glm-5.3-flash -> z-ai/glm-5.3-flashx` (the 2026-10 refresh gave it the slot `deepseek/deepseek-v4-pro` held), `google/gemini-3.8-flash -> google/gemini-3.1-flash-lite`, `minimax/minimax-m3 -> minimax/minimax-m2.5`, `openai/gpt-6-luna -> openai/gpt-5.6-luna`, `qwen/qwen3.7-plus -> qwen/qwen3.6-plus`, and `z-ai/glm-5.2 -> z-ai/glm-5.3-flashx`; the Gemini, GPT, Qwen, and GLM failback targets keep 1M+ token windows because reviewer prompts regularly exceed 250K tokens, while the MiniMax (`minimax/minimax-m2.5`, 200K) target has a smaller window than the largest reviewer prompts. It also retains retired-roster / operator-override mappings `deepseek/deepseek-v4-pro -> deepseek/deepseek-v3.2` (128K), `google/gemini-3.1-flash-lite -> google/gemini-3-flash-preview`, `moonshotai/kimi-k3 -> moonshotai/kimi-k2.7-code`, `qwen/qwen3.6-plus -> qwen/qwen3-coder-plus`, `x-ai/grok-4.20 -> x-ai/grok-4.3`, and `x-ai/grok-4.6 -> x-ai/grok-4.20` (the former `x-ai/grok-4.20 -> x-ai/grok-4.1-fast` entry was dropped because OpenRouter no longer serves that slug). Every other live reviewer is mapped; `REVIEWER_FAILBACK_UNMAPPED` still governs any operator-supplied slug without a chain entry.
- Reviewer loop guards (`scripts/review_run_reviewers.sh` watchdog, review panel only; the judge, consolidator, consensus summariser, and smoke reviewer-role callers are not capped). Every 10 s poll reads the attempt's OpenCode `--format json` event stream. `REVIEWER_MAX_STEPS` (default `120`): once an attempt starts more than that many turns (`step_start` events), the watchdog kills it (`wd_reason=max_steps`, log line `killed by watchdog ... (turn limit N ...); not retried.`) and the slot fails as a non-retryable failure with no cheaper-reasoning retry and no failback; a fast loop may overshoot by the turns that start within one poll. `REVIEWER_TOOL_REPEAT_LIMIT` (default `10`, minimum `2`): once the last N completed tool calls are identical (same tool and same JSON input, so paged `read` calls with different offsets never match), the watchdog kills the attempt (`wd_reason=tool_repeat`) and it follows the normal retryable path (class `tool_repeat`: cheaper reasoning, then failback). Invalid values fall back to the defaults with a `::warning::`. OpenCode's own agent `steps` setting is not used: in 1.18.23 it only injects a "maximum steps reached" instruction and keeps offering tools, so a looping model continues. Background: `x-ai/grok-4.20` reviewer passes looped on one repeated tool call for 2,205 / 1,468 / 234 turns (runs 35949371968, 36483245451, 36522631293) while real passes peaked at 101 turns; it left the default roster on 2026-09-29.
- The reviewer and editor PR-state watchdogs share `scripts/gh_helpers.sh::gh_review_pr_state` within a job: an `open` result is cached for at most 120 seconds under `RUNNER_TEMP`, while `closed`/`merged` and failed reads are not cached. A reopened PR therefore requires a fresh read before the watchdog stops work; this cache never authorizes a merge.
- The `GH_PAT_BUDGET` start/end snapshots cover each PAT-using job in review, clarify (including its failure-only `heal-report` job), poller, sweep, workflow-failure heal intake and validation-improvements intake. Both intake and `heal-report` snapshots precede their first PAT checkout; end steps run on failure and report unknown when checkout or quota reads fail or staged support lacks `gh_pat_budget` (the sweep's start step also reports unknown in that case). Deltas from a shared account are estimates and only comparable within one reset window.
- The source repo's `workflow-log-analysis.yml` daily 06:00 UTC cron runs `collect_workflow_logs.py` in PAT-budget mode against only this repo with the job-scoped `github.token` (not the measured PAT). It splits the prior two-day created window into two queries per instrumented wrapper/workflow path (50 pages per query, fail-partial at the API's 1,000-result ceiling), reuses the existing archive fetch cache, and reads up to 1,500 eligible completed run archives. It ranks job-end quota estimates by UTC hour in `gh-pat-budget-day` and the job summary, reporting missing archives, unknown deltas, timestamp fallbacks and truncated coverage. Weekly retros and manually dispatched analysis retain their existing paths. No new consumer-repo workflow wrapper or credential is introduced.
- Failed trusted `/reclarify` runs write a `github.token` source marker before retrying the `ai:reclarify-requeue` label up to three times; after the first failed add, the same token attempts to create the label for repos that have not synced it yet. `orchestrate_poll.yml` invokes `orchestrate_poll_process.sh` in `RECLARIFY_REQUEUE_SWEEP_ONLY=true` mode on every tick, even without tracking work; only a labelled issue with a trusted original command and Actions marker is eligible once PAT core remaining reaches 500. A persistent label failure leaves a marker-only issue undiscoverable and fails the clarify job.
- `scripts/cost_audit.py` now parses additive review telemetry fields `cache_hit_rate`, `wall_clock_p50_ms`, `wall_clock_p99_ms`, `break_glass_count`, and `context_budget_warn_count`. `CONTEXT_BUDGET_WARN` is emitted pre-flight from review / consolidator / judge paths when a prompt exceeds the configured per-model context threshold.
- `scripts/codex_heartbeat.sh` wraps long-running `codex exec` calls in reviewer, consolidator, review-blocked judge, conflict-resolver, and validate/self-heal paths, emitting `CODEX_HEARTBEAT: phase=<phase> elapsed_secs=<n>` during silent periods.
- `REVIEW_APPROVAL_RUBRIC_ENABLED` lets the review-blocked judge emit logical `review_state` values (`APPROVE`, `APPROVE_WITH_COMMENTS`, `COMMENT`, `REQUEST_CHANGES`) that `scripts/post_review_comment.sh --review-state` maps to outbound PR reviews. With `REVIEW_BREAK_GLASS_ENABLED`, a human comment anchored as `@codex break-glass` downgrades only the outbound `REQUEST_CHANGES` event to comment-only and logs `BREAK_GLASS`, while preserving the judge's written review body.
- Every `gh pr merge` call owned by `review_autofix.yml` is bound with `--match-head-commit` to the head that authorized it. Deterministic skips use the gate's `head_sha`, normal review paths use the checked-out `INITIAL_HEAD_SHA` captured before read-only/fork exits, and `scripts/review_rb_judge.sh` uses the checked-out `RB_JUDGED_HEAD_SHA` embedded in the judge prompt. Unknown or moved heads fail closed, and linked issues do not advance to `ai:ready-to-merge` after a rejected bound merge.
- **CI job layout (#4707).** `.github/workflows/ci.yml` runs as parallel jobs, not one sequential job: `static-checks` (the shell-block guard, YAML lint, drift check, actionlint, Python syntax and ruff, schema and prompt checks, ShellCheck; budget 15 minutes), four test jobs that each take a contiguous slice of the old step order (`tests-hooks-and-orchestrator`, `tests-heal-plan-and-validation`, `tests-promote-stall-and-review`, `tests-release-and-log-analysis`; 20 minutes each), and the `orchestrate-poll` matrix (4 groups, 20 minutes each). The final job keeps the id `lint`, so the aggregate status is still `CI / lint`: it needs every other job, runs with `if: always()`, and fails unless every needed job's result is `success`. Without `always()` it would be skipped when a job fails, and GitHub counts a skipped required check as passing. The old single job took 40–45 minutes (its budget went from 45 to 60 minutes in #4706 as a stopgap); the first run of the split layout (run 36523765261, 2026-09-29) finished in 9.0 minutes, with `orchestrate-poll (0)` as the critical path at 8.7 minutes (it also carries the fast-fail subset and the three single-file modules) and `tests-promote-stall-and-review` the slowest test job at 8.2 minutes. `tests/test_ci_job_split_contract.py` pins the aggregate's `needs` list and failure behaviour, the budgets, and that no step runs in two jobs. Add a new CI step to one existing job; a new job must also be added to `lint`'s `needs`. The one exception is `budget-watch`, a push-to-main advisory job that needs the worker jobs and stays out of `lint`'s `needs` (it is skipped on PRs); `POST_AGGREGATE_ADVISORY_JOBS` in the contract test lists it. Every `tests/test_*.py` / `tests/test_*.sh` file must be named in some workflow: `tests/test_ci_wires_every_test_file.py` (step `CI test wiring guard` in `static-checks`) fails otherwise, unless the file sits in its `UNWIRED_ALLOWLIST` with the reason it cannot run in CI. On 2026-10-10, 66 files ran nowhere and seven of them had gone stale; they now run in the `Regression tests wired by the CI wiring guard (part 1)` / `(part 2)` steps. A new test file goes into an existing step of the job whose topic it covers.
- The `orchestrate-poll` matrix runs `tests/test_orchestrate_poll_process.py`, CI's largest module: most of its tests spawn the real poller as a bash subprocess inside a throwaway sandbox, costing seconds each rather than milliseconds, and run sequentially it took roughly 35 minutes on a 4-core box. Each test allocates its own tempdir sandbox in `_make_poller_sandbox`, so the module shards with no shared state. The split has two levels, both `NR % total == n`: each matrix group takes its slice of the post-fast-fail subset (group index and count from `strategy.job-index` / `strategy.job-total`, exported as `CI_POLL_TEST_GROUP_INDEX` / `CI_POLL_TEST_GROUP_COUNT`), then shards that slice across `CI_POLL_TEST_SHARDS` local workers (default 4). A group index outside the matrix fails the step instead of silently skipping tests. The fast-fail subset and the three single-file modules (`test_orchestrate_poll_noop_suspicious_recovery.py`, `test_state_snapshot.py`, `test_run_substate_ledger.py`) run in group 0 only. `tests/test_ci_poll_test_sharding.py` verifies that both levels and their composition are a true partition. A failing shard fails its group, and a shard whose exit code was never recorded counts as failed rather than passing silently. The release gates carry the same sharded step in one runner: the `validate-scripts` job in `mark-stable.yml` and `test-and-mark-stable.yml` shards the full module (no fast-fail subset, no groups) under the same `CI_POLL_TEST_SHARDS` var. Its budget is 60 minutes since #4707, because that job measured 37 minutes on test-and-mark-stable run 36374918973. It was 45 minutes after release v1.27.0 (run 33073743283) was lost to the serial module overrunning that job's previous 30-minute cap, where the cancelled job skipped `validate`/`release`. `tests/test_ci_poll_test_sharding.py` pins the ported step's partition expression, failure handling, and budget in both workflows.
- `scripts/review_resolve_review_threads.sh` closes the loop on PR review comments. The pipeline has always *read* them — `scripts/review_collect_pr_metadata.sh` fetches `issues/<pr>/comments` and `pulls/<pr>/comments` into `PR_ALL_COMMENTS_CONTEXT_FILE`, which both `review_run_reviewers.sh` and `review_apply_fixes.sh` inline, and the editor must audit each one under `PR comment audit:` — but nothing marked the thread resolved, so a fixed comment looked identical to an unread one. The `Resolve addressed PR review threads` step in `review_autofix.yml` now runs after the editor summary and its persisted-change/no-op safety checks, then resolves each validated audited thread; an `applied` disposition additionally requires a productive commit. Mapping is keyed on the comment **id** carried by the audited `entry[N]`, resolved through `PR_ALL_COMMENTS_CONTEXT_FILE`, never on a path/line pair: an entry the editor never listed, an index whose audited path disagrees with the real comment's path, a non-`review_comment` kind, and an already-resolved thread are all skipped. That is what keeps two contradictory comments at one file:line from resolving each other. `ignored` entries are resolved too, but only after the editor's stated reason is posted as a thread reply, so the reviewer sees the disagreement and can reopen. Thread lookup is one paginated GraphQL query per run (§15); GraphQL is used because REST exposes no resolve-review-thread endpoint, and the §21.D/§23.D REST preference addresses the Claude Code Web proxy in interactive sessions, not this Actions-side caller. Every failure path warns and exits 0.
- `.github/workflows/review_autofix_sweep.yml` skips a PR whose head ref already has a `queued` or `in_progress` review run, so a 30-minute tick cannot stomp a synchronize-fired run mid-edit. That guard now distinguishes the two states. `in_progress` suppresses indefinitely — the codex-agent job legitimately runs over an hour. `queued` suppresses only until `SWEEP_STALE_QUEUED_MINUTES` (default 120, above the ~94-minute longest observed legitimate concurrency wait), because GitHub can wedge a run in `queued` with zero jobs and then reject both `cancel` (409 `Cannot cancel a workflow run that has not been queued yet`) and `rerun` (403 `This workflow is already running`). With no cutoff such a run suppressed the sweep forever, and the sweep is the PR's only recovery path, so the guard deadlocked the mechanism it protects — PR #3841 sat unreviewed for 11+ hours behind run `32984498460`. Discounted runs are logged as `AUTOFIX_SWEEP_STALE_QUEUED`, never dropped silently; a run with a missing or unparseable `created_at` still counts as active, and `SWEEP_STALE_QUEUED_MINUTES=0` restores the previous behaviour exactly. The guard also counts `pending` runs — a duplicate dispatch held back by `review_autofix.yml`'s `cancel-in-progress: false` concurrency group reports `pending`, not `queued`, and the same is true for the poller's `_has_active_autofix_run` guard in `scripts/orchestrate_poll_process.sh`. `pending` suppresses indefinitely, like `in_progress` (it is bounded by the running peer's 240-minute job timeout), and is never subject to the stale-queued cutoff. The sweep also skips a PR labelled `ai:merge-queued` while `MERGE_TRAIN_ENABLED` is on (read the way `scripts/review_merge_train.sh` reads it), logging `AUTOFIX_SWEEP_SKIP pr=#<N> reason=merge_train_queued`: a review dispatched for a queued PR only re-runs the `Merge-train gate` and soft-exits, and the train's `release` tick in `orchestrate_poll.yml` is the path that re-dispatches the review once the blockers are gone. On 2026-10-09 06:54 UTC run 37895976433 dispatched 31 reviews, every queued PR in a 25-deep train among them. A removed label (the one-shot bypass) and a disabled train both make the sweep dispatch again; `tests/test_review_autofix_sweep_merge_queued_skip.py` runs the shipped step against a fake `gh` for all three cases. Every review dispatch runs from the default branch and passes only a validated PR number: the sweep since issue #4618, and the poller's `_dispatch_review_for_conflicts`, the merge train's `_mt_dispatch_review` (`scripts/review_merge_train.sh`), and `forward-merge-stable-to-main.yml`'s fallback-PR dispatch since issue #4701. A `--ref <PR head branch>` dispatch ran the unmerged branch's copy of the review workflow with `secrets: inherit` and write permissions. The forward-merge branch is cut from `stable` by the workflow itself, but it moved too, because any writer can push to it before the dispatch. A default-branch run's head branch is the default branch, so the guards find it by name instead: `internal-review.yml` names every `workflow_dispatch` run `Internal: AI Review & Autofix [pr:<N>]`, and the consumer template `workflow-templates/ai-review.yml` names it `AI Review [pr:<N>]` (`run-name`; every other event keeps GitHub's default name). The sweep's snapshot keys a `workflow_dispatch` run with the internal name by `pr:<N>`, and keeps such a run even when GitHub reports its `head_branch` as null (issue #4928); a run with neither a head branch nor that name is dropped. In the poller, `_pr_named_review_dispatch_runs <pr>` lists only the two wrappers' own `workflow_dispatch` runs (`GET actions/workflows/<internal-review.yml | ai-review.yml>/runs?event=workflow_dispatch&created=>=<now − REVIEW_RUN_MAX_RUNTIME_MINUTES>&per_page=100`), page by page until it has read every run the listing's `total_count` reports, at most 10 pages per wrapper (GitHub serves at most 1,000 results for a filtered listing). A wrapper the repo does not have answers 404 and counts as complete and empty; in coding-workflows that is 3 calls (two `internal-review.yml` pages and one `ai-review.yml` 404). It returns 1 when the listing is incomplete: a page failed, a page was malformed, the listing shifted under it, or more runs exist than 10 pages hold. That case is logged as `PR_NAMED_REVIEW_RUNS pr=<N> outcome=incomplete reason=<…>`. An incomplete listing never authorises a dispatch or an empty-commit push, and the next poll cycle retries. Issue #4927 replaced the earlier single `gh run list --event workflow_dispatch --limit 100` page of every workflow's dispatches, which failed open to `[]`. That page covered under 3.5 hours here (153 `internal-review.yml` dispatches in 5 hours, 2026-09-29), so a burst of unrelated dispatches could push a live review run off it and let the stall recovery push an empty commit under it. `_has_active_autofix_run` uses it only when its head-branch lookups found nothing, and counts an incomplete listing as an active run (the dispatch is skipped). The stall-recovery failed-autofix redispatch uses it only when the head-branch lookups found no failed run, and acts on a PR-named failure only when it is newer than every completed head-branch run; on an incomplete listing it neither redispatches nor pushes that cycle. It passes the helper's optional second argument, a lookback of `REVIEW_RUN_MAX_RUNTIME_MINUTES + STALL_THRESHOLD_MINUTES` (370 minutes by default, 4 calls here), because a run that failed at the codex-agent job's 240-minute timeout would leave the 250-minute window minutes after it ended; the in-flight guards keep the default. `_direct_inflight_review_run_on_branch <branch> [pr]` uses it only when its branch listing matched no fresh run, and prints the sentinel `listing-incomplete` (`STALL_INFLIGHT_DIRECT_CHECK … outcome=pr_named_listing_incomplete`) on an incomplete listing, which both empty-commit push sites treat as a skip. The stall judge's `workflow_outcomes` and both empty-commit push guards' cached scans match the names in the cached `actions/runs` blob, and the merge-train release keys active runs as `pr:<N>` from the listing it already makes, so those add no API call. Since issue #6629 (re-issue of #5152) a run name alone proves nothing, because anyone who can push a branch copy of a wrapper can dispatch a same-named run from that branch: every one of these matchers (`_pr_named_review_dispatch_runs`, both cached in-flight scans, the stall judge's `workflow_outcomes`, `_mt_inflight_review_branches`, the sweep's `dispatch_pr`, and `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`) counts a run only when its `event` is `workflow_dispatch`, its `head_branch` is the default branch or null (#4928), and its title is paired with its own wrapper path (`.github/workflows/internal-review.yml` with the internal name, `.github/workflows/ai-review.yml` with `AI Review [pr:<N>]`, after stripping `@<ref>` and, in the poller and merge train, a leading `<this repo>/`). The four poller sites share one jq rule (`_pr_named_review_trust_jq_def`). The default branch is read with no `main` fallback: once per poller process (`_review_run_default_branch`, memoized in `RUNTIME_DIR`), once per merge-train release (`_mt_resolve_default_branch`) and once per sweep tick with candidates. When it cannot be read, every matcher fails closed: `PR_NAMED_REVIEW_RUNS … reason=default_branch_unavailable` (no dispatch or empty-commit push), `MERGE_TRAIN_RUNS_LISTING … reason=default_branch_unavailable` (release held), and `AUTOFIX_SWEEP_SKIPPED reason=default_branch_unavailable` (no sweep dispatch that tick). The merge train drops an untrusted review-wrapper dispatch run instead of counting it as `unattributed_run`; an untitled dispatch on the default branch is still unattributed. A manual `gh workflow run internal-review.yml --ref <feature>` therefore no longer suppresses or holds anything. `build_active_issue_set` still maps runs to issues by head-branch pattern. `review_autofix.yml`, the last dispatch candidate after both wrappers, has no PR run name. A consumer repo sees its dispatched runs by name only after its `ai-review.yml` sync. Before run names, the PR #3895 incident (2026-08-29) accumulated 10 duplicate dispatches and 6+ duplicate Telegram conflict warnings in ~95 minutes while the one real resolver — dispatched on `main`'s ref, invisible to both guards — ran to success.
  For the preceding failed-autofix redispatch rule, a PR-named failure tied
  to the latest completed head-branch run at GitHub's second-precision
  `createdAt` is treated as not superseded and retried directly. A strictly
  newer head-branch completion still suppresses the retry.
- `AUTOFIX_SKIP_TERMINAL_SAME_HEAD` defaults to `true` and lets the reusable review gate stop `workflow_dispatch` reruns after the newest trusted `REVIEW_AUTOFIX_PARTIAL_V1` marker for the current head sets `resume_should_continue=false`. Reusable workflows retain the caller's `github` context, so sweep dispatches through `internal-review.yml` still expose `github.event_name=workflow_dispatch`; pull-request events remain ineligible. The gate resolves the account authenticated through `GH_PAT` with one `/user` read, accepts marker comments only from that account, and then reads the paginated PR comments. Identity, comment, or parser failures emit `AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED` and fail open; rejected current-head markers are counted in `AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD`. A conflicted PR (`mergeable` not `true`) is never skipped, and inside `codex-agent` a terminal same-head resume (`AUTOFIX_RESUME_TERMINAL=true`) still runs `Detect merge conflicts`, the resolver steps, and `Push all pending commits` for a resolved merge; the editor and `Commit changes` keep skipping that path.
- `scripts/pr_checks_lib.sh` is the single source of truth for the PR check-runs merge gate (`_pr_checks_completed` + `_pr_required_check_names_for_base`). Both `scripts/orchestrate_poll_process.sh` (the final integration-merge gate **and** all four review-blocked merge gates) and `scripts/review_rb_judge.sh` (the standalone judge's `merge_with_followup` gate) source it, so the required-checks filter (branch protection ∪ `ORCH_FINAL_MERGE_REQUIRED_CHECKS`, with `*`=block-on-any and `""`=allow-all sentinels) can never drift between paths. Pending check-runs always block; a FAILED check-run blocks only when its name is in the required set, so a non-required/environmental red (e.g. CodeQL when code scanning is disabled) no longer deadlocks a judge-approved review-blocked merge. The library carries the `_is_self_check_run` exclusion (gated by `PR_CHECKS_SELF_RUN_ID`) the rb_judge needs to skip its own in-progress host job; the orchestrator leaves it unset so the exclusion is a no-op there. `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` is declared in both the library (set-if-unset) and `orchestrate_poll_process.sh` (a fail-safe so a sourcing failure can never reach the empty-string allow-all sentinel); `tests/test_pr_checks_lib_required_filter.py` pins the two literals equal. The library is staged into `SUPPORT_SCRIPTS_DIR`/`scripts/` everywhere the two callers run (`REQUIRED_BOOTSTRAP_SCRIPTS`, `orchestrate_poll.yml`, `review_autofix.yml`); a missing library leaves `_pr_checks_completed` undefined and every gate fails closed (no merge) — the safe direction.

### AGENTS.md materiality classifier (deterministic v1)

| Materiality | Deterministic rule set in `scripts/review_agents_md_materiality.sh` | Advisory when root `agents.md` is unchanged? |
|---|---|---|
| `high` | Root manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`); `.github/workflows/**`; root build/test config files (`pytest.ini`, `tox.ini`, `jest` / `vitest` / `playwright` / `cypress` / `webpack` / `vite` configs, `turbo.json`, `go.work`); newly added top-level directories detected against `origin/$BASE_BRANCH` | Yes |
| `medium` | Dependency / lock manifests (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `go.sum`, `Pipfile*`, `requirements*.txt`, `constraints*.txt`); lint / format configs (`.eslintrc*`, `eslint.config.*`, `.prettierrc*`, `stylelint*`, `ruff`, `flake8`, `pylintrc`, `biome`); API-client wrapper paths such as `sdk/client.go`, `apis/client.ts`, or `*_client.*` | Yes |
| `low` | Paths that match none of the deterministic `high` / `medium` rules | No |

| Variable | Default | Contract |
|---|---|---|
| `REVIEW_FLOOR_RULES_ENABLED` | `1` | Enable floor-rule tagging before the editor runs. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | `(empty)` | Optional keyword catalog override; empty / missing / unreadable falls back to the built-in catalog. |
| `REVIEW_CONSOLIDATOR_ENABLED` | `1` | Enable the advisory consolidator stage. |
| `REVIEW_CONSOLIDATOR_MODEL` | `openai/gpt-6-sol` | Default consolidator model in `review_autofix.yml`. |
| `REVIEW_CONSOLIDATOR_REASONING` | `high` | Default consolidator reasoning effort. |
| `REVIEW_CONSOLIDATOR_TIMEOUT_SECS` | `300` | Default consolidator timeout in seconds. |
| `REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT` | `16000` | Default consolidator output-token budget. |
| `REVIEW_PARSER_FAILOPEN` | `1` | Keep parser failures advisory instead of fatal. |
| `REVIEW_LEDGER_ENABLED` | `1` | Enable per-PR ledger persistence and `ledger_status.txt` emission. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | `2` | Threshold for the `accepted-residual` transition. |
| `REVIEW_LEDGER_PATH` | `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` | Default per-PR ledger path, persisted through the repository/PR-scoped run-independent cache staging root. |
| `REVIEW_REVIEWER_CHECKLIST_ENABLED` | `1` | Append the reviewer checklist block when the prompt template is available. |
| `REVIEW_REVIEWER_ITERATION_SCOPING` | `1` | Scope later reviewer passes from last-run changed files plus actionable ledger rows; first pass stays full-diff. |
| `REVIEW_LEDGER_REREVIEW_ENABLED` | `false` | Enable ledger-aware re-review suppression in the consolidator and the review-blocked judge's prior-round-decision input. |
| `REVIEW_APPROVAL_RUBRIC_ENABLED` | `false` | Enable logical review-state output from the review-blocked judge and outbound PR-review mapping through `post_review_comment.sh --review-state`. |
| `REVIEW_BREAK_GLASS_ENABLED` | `false` | Enable the anchored `@codex break-glass` override scan; when active it downgrades only the outbound `REQUEST_CHANGES` event to comment-only. |
| `CI_POLL_TEST_SHARDS` | `4` | Parallel local shards for the orchestrate-poll module in each group of CI's `orchestrate-poll` matrix and in the release gates' `validate-scripts` job. `1` is sequential; invalid values warn and fall back to `1`. |
| `CONFLICT_MANIFEST_UNION_ENABLED` | `true` | Resolve two-sided `.ai/.workspace_source_manifest.txt` content conflicts (index stages `1 2 3` or add/add `2 3`) and gitignored one-sided delete/modify conflicts before the model resolver; manifest-only conflicts are committed as `[ai-merge-resolve]`. Other manifest conflicts, including disabled cases and integration-sync branches with further unmerged paths (a manifest-only conflict on `orchestrator/project-*` is resolved like any other branch, but committed only after the integration fingerprint check passes on the merged tree; an unavailable check or a violation fails with `reason=integration_sync detail=fingerprint_unverified|fingerprint_violations`), fail preparation with `Manifest union-merge: unhandled reason=...` instead of dispatching a resolver whose sandbox excludes `.ai/`. Before PR #6438 the stage check never matched, so the manifest always reached the resolver, whose sandbox cannot carry `.ai/`. |
| `MERGE_TRAIN_IGNORE_PATHS` | `.ai/.workspace_source_manifest.txt` | Exact repo-relative paths excluded from merge-train overlap checks in gate and release. Comma/newline-separated; `none` (or an empty helper env value) restores legacy behavior. Glob entries are rejected. |
| `REVIEW_RESOLVE_THREADS_ENABLED` | `true` | Resolve PR review threads the editor audited in its `PR comment audit:` section. Keyed on comment id, so two comments at one path cannot resolve each other; `ignored` entries get the editor's reason as a reply before resolving. |
| `REVIEW_RESOLVE_THREADS_MAX` | `50` | Per-run cap on resolved review threads; anything above it is warned about and left open. |
| `SWEEP_STALE_QUEUED_MINUTES` | `120` | Age past which a still-`queued` review run stops suppressing a sweep dispatch (wedged-run recovery). `in_progress` runs are never discounted; `0` disables the cutoff. |
| `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS` | `300` | Retired, no longer read (the no-PR `claude/**` push review it served was removed in PR #6438; `internal-review.yml` no longer has a push trigger, so `claude/**` branches are reviewed once they have a PR, like every other branch). Historical behaviour of the removed push route: when a `claude/**` push finds no open PR, re-check every 60s for up to this many seconds and skip the no-PR reviewer run once a PR appears (its `pull_request` run reviews the same commit). `0` restores the single lookup; values outside 0-3600 fall back to `300` with a warning; a failed lookup counts as no PR and the review runs when the window ends. Logs `RESOLVE_CLAUDE_BRANCH_PR_WAIT` / `RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED` (the latter with gh's error text as `error="..."`: one line, at most 200 characters). |
| `REVIEW_TIER_RESOLVER_ENABLED` | `true` | Size-based `lite \| standard \| full` review tiers (1, 4, or all reviewers). Set to `false` to turn them off: the full panel then runs unless `REVIEWER_RISK_TIER_ENABLED` is also on, whose selection (which can be smaller) then stands. |
| `REVIEW_TIER_LITE_MAX_LOC` | `50` | Maximum total diff LOC for the one-reviewer `lite` tier. Any file type qualifies unless the diff touches a protected path, which goes to `standard`. |
| `REVIEW_TIER_LITE_REVIEWER_SLUG` | empty | Empty draws one reviewer from the whole `REVIEWER_MODELS` panel (from the `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` list instead when a repo sets it and every slug in it is on the panel), seeded by the PR number; a set slug pins it. Unknown or unavailable slugs fail open to `full`. |
| `REVIEW_TIER_STANDARD_MAX_LOC` | `200` | Maximum total diff LOC for the four-reviewer `standard` tier, in any folder. |
| `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` | empty | Empty (the default) draws the `standard` tier's four reviewers at random from the whole `REVIEWER_MODELS` panel, seeded by the PR number. A set value pins the `standard` reviewers and becomes the unpinned `lite` pool. Unknown or unavailable slugs fail open to `full`. |
| `REVIEWER_MAX_STEPS` | `120` | Hard turn cap per review-panel reviewer attempt, enforced by the `scripts/review_run_reviewers.sh` watchdog from OpenCode `step_start` events. An attempt that starts more turns is killed and the slot fails without a retry or failback. Invalid values fall back to `120` with a warning. |
| `REVIEWER_TOOL_REPEAT_LIMIT` | `10` | Consecutive identical tool calls (same tool and same input) that end a review-panel reviewer attempt as a retryable `tool_repeat` failure (cheaper reasoning, then failback). Minimum `2`; invalid values fall back to `10` with a warning. |
| `REVIEWER_RISK_TIER_ENABLED` | `0` | Enable deterministic `trivial | lite | full` reviewer fan-out by reviewer-visible diff LOC/file count. |
| `REVIEWER_RISK_TIER_TRIVIAL_LOC` | `10` | Trivial-tier LOC threshold. |
| `REVIEWER_RISK_TIER_TRIVIAL_FILES` | `20` | Trivial-tier changed-file threshold. |
| `REVIEWER_RISK_TIER_LITE_LOC` | `100` | Lite-tier LOC threshold. |
| `REVIEWER_RISK_TIER_LITE_FILES` | `20` | Lite-tier changed-file threshold. |
| `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` | sensitive-path regex | Force full reviewer fan-out on matching paths (default matches `scripts/`, `.github/workflows/`, `.github/ai/`, `prompts/`, `workflow-templates/`, `db/contracts/`, and `ai-memory/`). |
| `REVIEWER_TIER_TRIVIAL_MODELS` | `(empty)` | Optional comma-separated trivial-tier subset; empty falls back to the first live reviewer model from `REVIEWER_MODELS`. |
| `REVIEWER_TIER_LITE_MODELS` | `(empty)` | Optional comma-separated lite-tier subset; empty falls back to the first two live reviewer models from `REVIEWER_MODELS`. |
| `REVIEWER_FILTER_UNINTERESTING_ENABLED` | `false` | Enable pre-review stripping of low-signal lock/generated/minified files before reviewer fan-out. |
| `REVIEWER_FILTER_EXTRA_GLOBS` | `(empty)` | Optional comma-separated extra skip globs for `review_filter_uninteresting_files.sh`. |
| `REVIEWER_FILTER_EXEMPT_GLOBS` | `db/contracts/**,**/migrations/**,**/migrate/**` | Comma-separated exemption globs that stay reviewer-visible even when they match a skip rule. |
| `REVIEWER_CIRCUIT_BREAKER_ENABLED` | `0` | Enable per-reviewer health-state caching and same-family failback attempts. |
| `REVIEWER_FAILBACK_MAX_RETRIES` | `1` | Retryable-failure budget before a reviewer slot consults the failback chain. |
| `REVIEWER_HEALTH_OPEN_THRESHOLD` | `3` | Consecutive retryable failures required to mark a reviewer slot `open` in the health cache. |
| `REVIEWER_HEALTH_OPEN_TTL_SECS` | `1800` | Seconds an `open` reviewer-health entry suppresses dispatch before automatic expiry. |
| `AGENTS_MD_MATERIALITY_ENABLED` | `1` | Post the deterministic, non-blocking `AGENTS.md` materiality advisory comment when a material change omits an `agents.md` update (on by default; set `0` to disable). |
| `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` | `0` | Reserved only; deterministic v1 still makes no materiality model call when this flag is on. |
| `AGENTS_MD_MATERIALITY_MODEL` | `openai/gpt-6-luna` | Reserved future materiality fallback model slug. |
| `AGENTS_MD_MATERIALITY_REASONING` | `medium` | Reserved future materiality fallback reasoning effort. |
| `CONTEXT_BUDGET_WARN_RATIO` | `0.7` | Per-model context-window ratio above which review-surface prompt builders emit `CONTEXT_BUDGET_WARN`. |
| `MAX_PROMPT_TOKENS_FOR_PHASE` | `(empty)` | Absolute prompt-token override that takes precedence over `CONTEXT_BUDGET_WARN_RATIO`; phase-specific `MAX_PROMPT_TOKENS_FOR_<PHASE>` overrides remain supported. |
| `CODEX_HEARTBEAT_ENABLED` | `1` | Enable the `codex_heartbeat.sh` wrapper on long-running review / validate Codex calls. |
| `CODEX_HEARTBEAT_INTERVAL_SECS` | `30` | Silence interval (seconds) between emitted `CODEX_HEARTBEAT` lines. |
| `REVIEW_DIATAXIS_LENS_ENABLED` | `true` | Documentation-only contract row for the advisory `DOCS COVERAGE (DIATAXIS)` consolidator lens. Current branch behavior is prompt-defined only (no separate workflow toggle yet): keep it `low` severity and name only still-missing `Reference` / `How-to` / `Tutorial` / `Explanation` updates. |
| `REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED` | `true` | Enable the consolidator-side companion `AGENTS.md` materiality finding. Unlike `AGENTS_MD_MATERIALITY_ENABLED`, which controls the separate advisory comment helper, this flag only controls whether `review_consolidate.sh` passes the helper JSON into Lens 7 (`NAMING / BACKWARD COMPATIBILITY`). |
| `ENABLE_SECURITY_PASS` | `true` | Enable the scheduled poller's mandatory current-integration-head security gate before validation or finalization. Set to `false` for the immediate operator kill switch and legacy completion behavior. |
| `SECURITY_AUDIT_LINE_OWNERSHIP` | `project` | Line ownership for the project security pass (plan item 4b, D4). `project` blocks only on findings whose cited line was written in the project's `merge-base..head` range; older lines become non-blocking advisories filed as `ai:security` follow-ups, unless the project deleted or added lines anywhere in the cited file, deleted lines in another file the finding names or that is linked to the cited file by module name (at base or head), directly, through a file naming both, through a bounded chain of referencing files (a search that cannot finish keeps it blocking) or through a third module both name, or changed a non-documentation, non-test file that names the cited module at head (those stay blocking). `off` restores the previous gate and payload exactly. |
| `SECURITY_AUDIT_LINE_OWNERSHIP_HUNK_WINDOW` | `40` | Lines either side of a pre-project cited line within which a project-added hunk logs `reason=changed_hunk_within_window`; an addition further away in the same file still blocks, as `reason=added_lines_in_file`. Invalid values warn and fall back to `40`. |
| `VALIDATION_SYNTH_SANDBOX_TIMEOUT_SECS` | `300` | Per-test bound on a synthesised behavioural smoke test (`validation/tests/synth_round_*.sh`) in `scripts/validate_driver.sh`'s credential-free, network-less container; a timeout reports TAP `ok ... # SKIP`. Invalid values warn and fall back to `300`. |
| `SUMMARISER_ISOLATION_MAX_ATTEMPTS` | `3` | Consecutive consensus-summariser attempts refused because the review sandbox was unavailable before the summariser hard-fails (`opencode_agent_failure ... failure_class=isolation_unavailable`). The summariser and behavioural-smoke synthesiser never run OpenCode on the host; a refused attempt logs `::error::REVIEW_UTILITY_ISOLATION role=<SUMMARISER\|BEHAVIOURAL_SMOKE> engine=<claude\|codex> outcome=refused reason=sandbox_unavailable\|sandbox_helper_outdated`, and smoke synthesis fails open with `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=isolation_unavailable`. Invalid values warn and fall back to `3`. |
| `MAX_SECURITY_PASS_CYCLES` | `5` | Maximum completed consolidated security-fix cycles before persistent findings terminalize as `ai:security-pass-failed`. Resets to `0` when an advancing integration head invalidates a recorded clean pass. Re-audits after a merged fix are delta audits, so the budget bounds persisting findings rather than fresh samples of unchanged code. For standalone PRs, a completed current-head audit is required to enter judge exhaustion mode. |
| `SINGLE_ISSUE_SECURITY_PASS_ENABLED` | `false` | Off by default (opt in with `true`); orchestrator projects keep their own pass under `ENABLE_SECURITY_PASS`. When enabled, hold eligible standalone PRs into the default branch until a security audit of the current head is clean. At the cycle cap an unaudited head gets bounded retries, then remains held without judge exhaustion mode; a completed findings audit for the same head still qualifies if a later attempt fails. A missing or unwritable `GITHUB_OUTPUT` fails the gate step closed; dispatch failure at any cycle holds the merge for a later review retry, and records a failed cycle (past the cap, a failed head attempt). With the flag off (the default) the pre-pass review-gate and deterministic-skip merge behavior applies. Only a sole verified `ai:security` follow-up is exempt; see `README.md` for dispatch and failure modes. |
| `SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS` | `2` | Maximum trusted pending audit attempts per head with a cycle number above the standalone pass's cycle cap. Pre-cap pending markers do not consume these extra attempts. Invalid or non-positive values fall back to `2`; a failed exhausted retry dispatch holds the merge. |
| `SECURITY_PASS_PENDING_STALE_HOURS` | `6` | A pending single-issue audit holds auto-merge until its marker is this many hours old; the next review run re-dispatches. Invalid or non-positive values fall back to `6`. Only a sole verified `ai:security` issue skips the pass; multiple linked issues are audited. When `GH_PAT` is absent, both gate and reporter trust only `github-actions[bot]` markers. |
| `SECURITY_PASS_FOLLOWUP_STALE_HOURS` | `24` | A findings hold pages CRITICAL after this many hours without a new head, even if matching pipeline-created follow-ups are still open. The audit log carries base64 JSON finding IDs into the pipeline-authored findings comment (before the unchanged final marker); the gate reads that comment from its cached PR history and requires open pipeline-authored `ai:security` issues for each ID on this branch. Older results without IDs or unparseable IDs page as `followups_unverifiable`, and uncovered IDs page as `followups_missing`. Missing, invalid or unverifiable timestamps also page. The source 30-minute review sweep and consumer `ai-review.yml` hourly schedule dispatch one judge-only recheck per stale head/cycle. Invalid or non-positive settings fall back to `24`. |
| `MAX_SECURITY_PASS_FIX_REISSUES` | `2` | Maximum re-issues of one `ai:implementation-failed` consolidated security-fix issue per fix cycle before the pass terminalizes as `ai:security-pass-failed`. |
| `SECURITY_PASS_CONFIDENCE_GATE` | `8` | Minimum 1-10 confidence score for findings that block the project security pass. |
| `CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED` | `true` | Defaulted inside `scripts/review_conflict_prepare.sh`; `review_autofix.yml` does not forward it from a repository variable. Before resolver dispatch, a conflict on `.claude/hooks/pr_merge_status_guard.py` is resolved to the clean merge of `workflow-templates/.claude/hooks/pr_merge_status_guard.py`, but only for content conflicts (stages `1 2 3` or `2 3`) where each side's live copy equals that side's template in blob and mode and the template has a single regular stage-0 entry. A hook-only conflict is committed as `[ai-merge-resolve]` (on `orchestrator/project-*`, only after the integration fingerprint check passes). Any other shape logs `Guard-hook template merge: skipped reason=<disabled\|no_merge_head\|stage_shape\|template_unmerged\|template_missing\|template_not_regular\|ours_mismatch\|theirs_mismatch\|unsafe_path>` and keeps the `sandbox_path_host_only` refusal. |

- A Claude pool reviewer attempt that does not succeed (`run_pool_reviewer` in `scripts/review_run_reviewers.sh`) keeps up to 64 KiB of its output as `PREVIOUS_REVIEWS_DIR/rejected_<prefix>_<slot>_attempt<N>.txt` (an attempt with no output removes any stale file of that name), outside the `<prefix>_*.txt` globs the consensus summariser reads; both reviewer-log artifacts upload `rejected_*.txt`. A crashed Claude run in `scripts/review_untrusted_sandbox.sh` also logs `CLAUDE_POOL crash_detail role= account= exit_code= result=<first 400 printable characters of the CLI result text|empty|no_result_event>`.
- Both README trimmers (`review_untrusted_workspace.py readme-trimmed` and `build_static_context.sh readme`) leave out the body of an exact `#### Variables` section, up to the next line that starts with `#`. They keep the heading and add one pointer line telling the model to read that section of `README.md` in the repository. Everything else before `### 2. Create wrapper workflows` (overview, quickstart, secrets, the `.github/ai` operator surfaces) is unchanged. The 200,000-byte prompt cap still applies to what is left. The two trimmers must stay byte-identical; `tests/test_review_static_context_readme.py` checks both against one awk reference.
- **Noop-suspicious force-merge trusts only pipeline evidence (#7126).** The poller's noop-suspicious sweep (`scripts/orchestrate_poll_process.sh`) skips fork PRs before any per-PR read. It uses `isCrossRepository` and `headRepositoryOwner`, which ride the existing `gh pr list` call, and re-checks `.head.repo.full_name` on the Gate A read; a missing field skips the PR. Noop warnings and the Gate B editor summary count only when `user.login` equals `unblock_trusted_login`, and an unresolved login skips the PR (no re-dispatch, no merge). A productive `[ai-autofix]` / `[judge-fix]` commit must have `NOOP_PRODUCTIVE_COMMIT_NAME` / `_EMAIL` (`codex-bot`, `codex@users.noreply.github.com`, the identity of every pipeline commit writer) as author or committer. Before the merge, one `repos/<repo>/activity?ref=refs/heads/<head>` read must show a push by that login covering such a commit: after the push's `before` in the PR commit list (or from the start when `before` is not in the list), up to and including its `after`. An unreadable listing re-checks next sweep without paging (`reason=activity_unreadable`); no covering push pages once (gate `C_pusher`; only one 100-record page is read, and a full page without a covering push names `reason=activity_page_full` in the alert). The snapshot `headRefOid` must equal the Gate A head, and the newest trusted noop warning's `review-autofix-failure:v1` marker must name that head (another head waits without paging; no marker pages once, gate `B_head`). Gate D now calls `_pr_wait_for_required_checks <pr> <head> <base> 0`, which evaluates once without sleeping and requires the named CI check (#6963); `orchestrate_poll.yml` does not forward `AUTO_MERGE_REQUIRED_CI_CHECK`, so in consumers no named check is required there. `timeout` (pending run, queued CI run, missing or pending named check) re-checks next sweep without paging. `query_failed` / `unresolved_head_sha`, and `ok` with an unreadable `actions/runs` listing (`PR_CHECKS_RUNS_READ=false`, `reason=ci_runs_unreadable`, checked before the empty-listing page), warn without paging. `ok` with no check-runs and any other outcome page once (`D_check`). The merge is `gh pr merge --squash --auto --match-head-commit <head>`. Every other poller merge site (`attempt_merge`, backward scan, ready-to-merge, the review-blocked `merge` / final `fix` / no-fix paths and the final integration merge) also passes `--match-head-commit` with the head its checks used, and skips the tick when that SHA is missing.
- **Cancelled checks block; newest run per name (#7127).** `_pr_checks_completed` (`scripts/pr_checks_lib.sh`) accepts only `success`, `neutral` and `skipped`. A required check-run that ended `cancelled`, `timed_out`, `action_required`, `failure` or `startup_failure` blocks, and in `*` mode any such check-run blocks. A cancelled check-run blocks only while it is the newest check-run of its name (highest `id`, then latest `started_at`, decided before the unchanged `PR_CHECKS_SELF_RUN_ID` exclusion), so a re-run supersedes a cancelled attempt. For the configured CI check name (`AUTO_MERGE_REQUIRED_CI_CHECK`, `lint` here, also a job of `lint-pr-body-auto-close.yml` and `lint-plan-archival.yml`, which re-run on every PR body edit) only a newer run from the same Actions run (a re-run attempt) or, with `PR_CHECKS_CI_RUN_IDS` set, a newer bound CI run supersedes a cancelled one; the shared jq rules are `PR_CHECKS_JQ_SUPERSEDE_DEFS`. Every other check-run is still evaluated on its own: a failed or running run blocks even when a newer same-name run exists, because a check-run carries no workflow identity and this repo has three jobs named `lint`. `PR_CHECKS_LAST_PENDING` counts every running check-run, so a cancelled required check is a settled failure (`blocking`, pending 0) that `_pr_wait_for_required_checks` refuses at once when no other check-run is still running (pending runs, advisory ones included, keep the wait polling). The CI side channel (`PR_CHECKS_LAST_CI_STATE`) drops a cancelled run superseded by a newer run only when the runs are bound to CI run ids (`PR_CHECKS_CI_RUN_IDS`); name-only matching keeps every run. `PR_CHECKS_LAST_TOTAL` keeps the raw non-self count. `_summarize_final_merge_blockers` in `scripts/orchestrate_poll_process.sh` applies the same rules. The noop-suspicious force-merge Gate D there calls `_pr_checks_completed "${N_PR}" "${N_HEAD_SHA}" "${N_BASE_REF}"`, taking the base ref from the Gate A PR read (fallback: the `STANDALONE_PRS` cache), and blocks with `reason=helper_unavailable` when the library is not loaded. Gate D pages through `_noop_gate_failure_alert` (gates `D_helper` and `D_check`, once per PR head); while a check-run is still running, or when the check-runs read failed (`query_failed`, `unresolved_head_sha`), it skips the PR without paging and re-checks on the next sweep. This keeps the library the single source of truth for the gate.
- `scripts/review_untrusted_workspace.py` reads each file up to 8 MiB (`MAX_FILE`; it was 2 MiB). `snapshot` fails closed on a larger tracked file, except when `REVIEW_SNAPSHOT_SKIP_OVERSIZED=1` is set (default `0`). Only `scripts/review_untrusted_sandbox.sh` sets it, for the review editor snapshot. There the file is left out of the snapshot and the baseline, and the step logs `REVIEW_ISOLATION_SNAPSHOT oversized_skipped=<n> max_file_bytes=<bytes>` with a count and no paths. The editor never sees the file, and `transfer` refuses a result that recreates it (`reason=result_conflicts_host`, or `reason=unsafe_file` when the recreated file is itself over 8 MiB, because the result read rejects it first). The heal editor (`heal_isolated_implement.sh`) and the pre-push test gate (`prepush_test_gate_run.sh`) never set the flag, so a missing file cannot turn into a false test failure there. A `snapshot` rejection now names `reason=unsafe_file` or `reason=file_changed` instead of `reason=unknown`.
- **Continuation dispatch peer check.** `scripts/review_autofix_step_post_commit_retrigger.sh` bypasses `autofix_retrigger_has_inflight_peer` for a continuation dispatch (productive `[ai-autofix]` push, `AUTOFIX_CONTINUATION_ENABLED` not `false`) only when `AUTOFIX_SKIP_SELF_TRIGGERED` is exactly `true`, the same comparison the gate uses to skip the push's own synchronize run (`AUTOFIX_PEER_CHECK_BYPASSED reason=continuation_dispatch`). Otherwise that synchronize run is a real review run in the same `pr-autofix-<PR>` group, so the continuation runs the peer check and, when it finds a peer, logs `AUTOFIX_DISPATCH_SKIPPED reason=sync_event_inflight ... source=post_commit_retrigger continuation=true` and dispatches nothing. No peer, a failed probe or a missing helper still dispatches (fail open). The non-continuation path and the default-branch dispatch chain (#4898) are unchanged.
- **Editor stall fallback.** In the editor retry loop of `scripts/review_apply_fixes.sh`, an attempt the stall guard killed (`stall_state=killed`) sets `editor_prev_attempt_stall_killed`, and the next attempt runs on `MODEL_EDITOR_FALLBACK` instead of `MODEL_EDITOR` (`editor_stall_fallback_applies`). It switches only when `MODEL_EDITOR_FALLBACK` is set and differs from `MODEL_EDITOR`, `${AI_ENGINE_RESOLVED_REVIEW_EDITOR:-codex}` is not `claude` (the Claude sandbox ignores the OpenRouter slug), and `EDITOR_STALL_FALLBACK_ENABLED` is on: unset, empty or `true` (any letter case) is on, `false` is off, and any other value is off with one `::warning::`, read once inside the script from the step environment with no `:?` guard. `review_autofix.yml` does not map a `vars.EDITOR_STALL_FALLBACK_ENABLED` repository variable into the step, so a repository variable alone does not reach the script. Once switched, the remaining attempts stay on the fallback. It logs the plain line `Editor attempt N: previous attempt was stall-killed; switching model to fallback <model>.` with no new prefix. The final-attempt capacity fallback, retries after failures that are not stall kills, and the exit 78, 79 and 80 branches are unchanged. `tests/test_editor_capacity_fallback_contract.py` pins the condition and checks the selection behaviour.

## Integration-sync verifier + bootstrap contract

- Source-repo resolver attempts in `scripts/review_conflict_resolve.sh` snapshot the
  worktree and merge index before invoking the model. An out-of-scope edit gets
  a bounded scope-feedback retry only after the pre-attempt state is restored
  and verified; an unsafe restore fails closed. Consumer repos retain the
  previous path, and the final `check_resolver_diff.sh` commit gate is unchanged.
  Both engines now run inside fresh review sandboxes; the model's Git snapshot
  is separate from the host merge index, and only admitted edits pass validated
  transfer. The script still stages accepted resolutions itself, and a change
  to the host index fails the source-repo scope check. The private-index helper
  (`_resolver_model_index_prepare`, #5627) remains for compatibility but is no
  longer called by the resolver loop. The source-repo OpenCode config still
  disables snapshots.

- `scripts/verify_integration_fingerprints.py` supports `--baseline-fingerprints-state <out>` / `--compare-against-baseline <in>` alongside `--ref`; capture mode records ref-accurate `head_sha` metadata, compare mode emits `PRE_EXISTING_FINGERPRINT_DRIFT_V1` markers for pre-existing drift that should not block the resolver commit, and the verifier-side false-positive defenses emit `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1` (capture-side multi-occurrence partial removal), `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1` (a `must_contain` line modified after capture by a non-`[ai-merge-resolve]` commit), and `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1` (a `must_not_contain` line re-added after capture by a non-`[ai-merge-resolve]` commit — e.g. a back-merge of the default branch keeping its still-present copy) when the ref-mode wave-dispatch gate suppresses a non-resolver false positive. The two post-capture defenses share one direction-agnostic pickaxe primitive and both fail closed in working-tree mode, so the resolver's own pre-commit self-check stays strict and still cannot silently revert merged intent.
- `.github/workflows/review_autofix.yml` stages required and main-primary helpers from the verified reusable-workflow SHA; PR-head copies are review data, not runtime code. `render_prompt.py`, `review_conflict_resolve.sh` and their dependencies ship with that same workflow commit. Embedded PR-diff template syntax is still handled by `render_prompt.sh` with `RENDER_PROMPT_SKIP_SYNTAX_VALIDATION=1` after assembly, while static templates retain strict validation. Optional support missing from that commit skips the feature; required support fails closed. The model catalog comes from the same commit as the reviewer roster, never from a PR branch or a separately resolved main snapshot.
- `scripts/review_merge_train.sh` is staged through `REQUIRED_BOOTSTRAP_SCRIPTS` for `review_autofix.yml` and copied best-effort (with `label_helpers.sh`) next to `gh_helpers.sh` by `orchestrate_poll.yml` and `cancel_on_pr_close.yml`, which do not run the full support staging. Both callers treat a missing copy as "skip this tick" so an older `SCRIPT_REF` keeps polling; its own API budget is documented in the script header (CLAUDE.md §15).
- `scripts/review_conflict_resolve.sh` persists one `AUTOFIX_RESOLVER_RETRY_STATE_V1` PR-body block per final PR/head SHA, keyed by normalized fingerprint failure signature or a synthetic resolver-isolation failure signature. `RESOLVER_ESCAPE_THRESHOLD_N` is the per-tier same-head, same-signature step size: fingerprint failures at multiples advance `strict` → `ratio` → `count_only` → `warn_only` and emit `FINGERPRINT_TIER_DOWNGRADED_V1`; isolation failures remain `strict`. After the next multiple, either failure class labels the **final PR issue** `ai:resolver-escalated` and records `escalated_at` for poller-side suppression / branch-rebuild gating.
- Conflict completion is a trusted-runner Git-index invariant. `scripts/review_conflict_prepare.sh` records initially unmerged paths separately from fingerprint-only resolver expansions; after the isolated model returns, `scripts/review_conflict_resolve.sh` fails on any path-specific staging error and refuses both no-change success and `[ai-merge-resolve]` commit creation while `git diff --name-only --diff-filter=U --` reports entries. `.github/workflows/review_autofix.yml` marks resolver actuation before invocation and summarizes an attempted run without `CONFLICT_RESOLVED=true` as `conflict_resolver_failed`.
- `scripts/verify_integration_fingerprints.py` uses `FINGERPRINT_QUARANTINE_RUNS_M` to move stable unchanged drift into ai-memory quarantine and emits `FINGERPRINT_QUARANTINED_V1` markers when the skip path activates. `.github/workflows/drift-audit.yml` (cron `0 3 * * *`, gated by `DRIFT_AUDIT_ENABLED`) scans `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` markers and maintains tracker issues for persistent clusters. The audit skips any cluster whose fingerprint path is absent from the repository checkout, so markers echoed from test fixtures or PR diffs (synthetic paths such as `scripts/example.py`) do not open tracker issues. Completed runs concluded `cancelled` / `skipped` routinely upload no logs (concurrency-superseded review runs); a failed log fetch for them is classified `unscannable` rather than missing, keeping coverage `full` so the per-run Telegram summary stays at DEBUG instead of firing a daily partial-coverage WARNING; their logs are still scanned when present. Every enabled run posts a Telegram run summary (`tg_send_msg`, gated by `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID`) linking to the run and writes a GitHub Actions job summary.
- `.github/workflows/security-audit.yml` (weekly `0 8 * * 0` plus `workflow_dispatch` plus `workflow_call`, gated by `SECURITY_AUDIT_ENABLED`, default `true`) is a default-branch maintenance audit that runs on the source repo and, via the synced `workflow-templates/ai-security-audit.yml` wrapper, on every consumer repo against its own default branch (consumer runs stage this repo's `scripts/` + `prompts/` from a `@stable` support checkout into `SECURITY_AUDIT_SUPPORT_DIR` and need `OPENROUTER_API_KEY`, optionally `GH_PAT`). It runs `scripts/security_audit.sh` with `prompts/mode-security-audit.txt`, appends dated findings sections to the stable `AI Security Audit Tracker` issue (`ai:security-audit`, marker `<!-- ai:security-audit-tracker:v1 -->`), and opens one `ai:security` follow-up issue for every finding that survives confidence-gate + false-positive-exclusion filtering, with no per-run or weekly cap (the former 3-per-week cap deferred findings silently: tracker #3576, run 35996690244, surfaced 5 and filed 3). Findings whose `<!-- ai:security-finding:<id> -->` marker is already on any `ai:security` issue, open or closed, are skipped; that dedupe reads every such issue with one paginated REST listing (`gh api --paginate --slurp repos/<repo>/issues?labels=ai:security&state=all`), so it no longer stops at 200 issues, and it ignores pull requests. The orchestrator's `[security-pass] Advisory: …` issues carry the same marker and label, so the audit never re-files a finding the security pass already filed. Each completed default-branch run records the audited HEAD on the tracker body (marker `<!-- ai:security-audit-last-sha:… -->`) unless a full scan skipped over-cap text: findings and the partial-coverage tracker comment still post, a WARNING is sent, the SHA remains unchanged, and `<!-- ai:security-audit-partial-coverage:v1 -->` is stored on the tracker body. While this marker remains, subsequent default-branch runs repeat a full scan even when HEAD is unchanged or incremental diff scope would normally apply; a complete scan clears it when advancing the SHA. Binary skips do not hold the marker. Otherwise unchanged HEAD skips (`SECURITY_AUDIT_SKIP_IF_UNCHANGED=true`, log-only skip) and changed HEAD diff-scopes the audit (`SECURITY_AUDIT_INCREMENTAL=true`; the post-filter drops findings citing unchanged files as `suppressed_out_of_scope`; first runs, history rewrites, and >200-file diffs fall back to the full scope). `.github/workflows/internal-clarify.yml` skips `ai:security-audit` issues so tracker bookkeeping never recurses into the normal clarify/plan pipeline.
- **Security dependency hold (issue #4934).** When the audit files a second finding for a file it already filed one for, the new `ai:security` issue carries one `- Depends on: #<n>` line. `scripts/security_dependency.py security-dependency --issue-json <file> --repo <owner/repo> --issue-number <n> [--number-only]` prints `{"status": "none" | "ready" | "held", "reason", "depends_on"}`; a malformed, repeated or unverifiable declaration is `held` (fail closed). Clarify (`Decide clarify route`), both implement gates and the standalone stall poller hold such an issue until #<n> is closed with `ai:merged`; the poller then posts one `/reclarify` with the `<!-- ai:security-dependency-released:<n> -->` marker. The check costs one issue read, only for an issue that declares a dependency. It moved unchanged from the retired Claude issue router.
- **Standalone clarify auto-decide (port P3).** On an issue that is not `ai:orchestrator-managed`, `clarify.yml`'s "Standalone auto-decide" step answers freshly posted questions with each question's RECOMMENDED option: `scripts/auto_decisions.py parse` builds the `Q1: A` lines and `scripts/orchestrate_parse_and_post_answer.sh` posts them with its loop guard (an exhausted guard still escalates to `ai:blocked`). Every pick becomes an `AD-<n>` entry (question, pick, why, alternatives) in the single trusted `<!-- ai:auto-decisions:v1 -->` comment, edited in place; `implement.yml` copies the entries into the PR body with `#<digits>` broken up. The existing clarify comment read is now paginated once (one API call per page), shared with semantic-cache history and auto-decide; the prompt still gets only the oldest 50, while the auto-decide fallback guard counts prior auto-answers from the full snapshot. If pagination fails, clarification stops before the answer; if the full history cannot be read at auto-decide time, it skips the answer. Skipped after a human `/reclarify`, when a question has no RECOMMENDED option, or with `STANDALONE_AUTO_DECIDE_ENABLED=false`. Costs one comment write for the answer and one for the AD comment; no additional comment read at the auto-decide step.
- **Unblock judge (Phase 7).** Markers, all trusted only when posted by the pipeline's own login (the GH_PAT user): `<!-- ai:unblock:v1 item=<n> stop=<id> fingerprint=<12 hex> verdict=<v> round=<k>[ override=bulk_delete] -->` as the last line of each verdict comment, on the item and, for a project's item, on the tracking issue (the ledger `scripts/unblock_ledger.py` counts rounds and never repeats a verdict per fingerprint from these); `<!-- ai:unblock-wait:v1 item=<n> fixup=<m>[ done] -->` (or `reason=invalid_verdict`) while a fix-up is pending, refreshed in place; `<!-- ai:unblock-fixup:v1 item=<n> round=<k> -->` opening a standalone fix-up issue; `<!-- ai:unblock-fixup-request:v1 item=<n> id=unblock-<n>-r<k> -->` on a tracking issue, which `handle_unblock_judge_project_hooks` files into the current wave (local id in `issue_number_map`, so once only); `<!-- ai:unblock-override-used:v1 comment=<id> -->`, which `implement.yml` posts when it spends a bulk-delete override. PR-derived issues carry the audit-only `<!-- ai:unblock-provenance:v1 source_pr=<n> author=<login> head_repo=<owner/repo> head_sha=<sha> -->`; issue-creating verdicts for PRs without a verified same-repository head and trusted author close the PR without creating an issue. Label `ai:unblock-closed` marks an item the judge closed; on a tracking issue the poller sets state `abandoned` and closes the issue only when the newest pipeline-authored verdict marker for that tracking issue is `close`, no later pipeline-authored V1 or V2 state comment supersedes it, and the project is still `failed` with the verdict's stop label (unless it is already `abandoned` for a close retry). State field `judge_output_failures` counts project-judge runs with no usable output (`JUDGE_OUTPUT_FAILURE_MAX`). The `close` verdict is offered and accepted only after a fixed terminal condition (item or project round cap, 24 hours still blocked after the last round on the same stop and fingerprint, or an exhausted menu; `TERMINAL_REASONS` in `scripts/unblock_ledger.py`), never on the model's choice, so comment text cannot make the judge abandon an item (#6557). Before recording a close, the judge re-reads the item and requires it to be open with the same block stop (`reason=block_state_changed` / `block_state_recheck_unavailable` otherwise, no marker posted). Kill switch `UNBLOCK_JUDGE_ENABLED` (default `true`). Needs-human digest (plan item 4a, D6): a ledger-forced terminal (every round spent: `item_cap`, `project_cap`, `still_blocked_24h`, `menu_exhausted`) no longer closes the item or adds `ai:unblock-closed`; `scripts/operator_step_issue.py needs-human park` labels it `ai:needs-human` and adds one line to the single pipeline-authored digest issue (`<!-- ai:needs-human:v1 -->`, label `ai:operator-step`, 200 entries plus an overflow marker), and the caller sends one CRITICAL per newly parked item. The unblock scan skips a parked item (`ai:needs-human` plus a trusted `close` marker, and listed in the digest per the tick's prune `items`) as `parked`; a failed digest write never removes the label, so an item the digest does not list is judged and parked again; the poller prunes the digest once per tick and, only with `UNBLOCK_JUDGE_ENABLED=false`, parks a project at its exhausted-budget terminal paths. Parking never writes the staged-support latch marker. With `NEEDS_HUMAN_DIGEST_ENABLED=false` a terminal `close` closes the item as before. Kill switch `NEEDS_HUMAN_DIGEST_ENABLED` (default `true`).
  The poller reads V1/V2 state only from the authenticated GH_PAT login's comments, skipping the tracking issue if identity is unavailable. Project reset commands require that login or a human OWNER/MEMBER/COLLABORATOR; outsider comments cannot resume failed work.
  An unavailable identity sends one fail-open CRITICAL Telegram alert per tick when Telegram is configured; the login probe is cached across projects.
  For `ai:security` issues, a terminal verdict leaves the issue open with `ai:unblock-closed` and its block label; reissue copies a validated finding marker, `ai:security` label, dependency and integration-branch metadata onto a standalone replacement before closing the original. A malformed finding marker or other metadata prevents replacement creation, and a create response missing the requested label leaves the original open. `accept_with_followup` is never offered for an `ai:security` issue (`unblock_ledger.py decide --security-issue`), and `unblock_actions.py` refuses it again with a keep-open comment and a WARNING, so a finding is never waived with an unbound follow-up (#6541). A tracked (project) security reissue puts the finding marker in its fix-up request, and the poller labels that successor `ai:security`; a tracked security issue without a valid marker is not split.
  An unlabeled tracking issue uses the `project-failed` fallback only when its latest V2 write from the pipeline login is complete and still `failed`; the judge's `--require-latest` read rejects an incomplete or malformed newer write instead of falling back to an older failed state. The judge re-reads that state before recording a verdict and again after label-catalog preparation, directly before adding `ai:unblock-closed`. A late change after the verdict comment may leave a recorded but unacted-on verdict. A failed project stays eligible for the unblock scan even if it carries an unverified `ai:unblock-closed` label.
  Guard rejection comments also end with `<!-- ai:guard-rejection:v1 item=<n> guard=<scope|scope-lock|destructive> reason=<reason> run=<id> count=<n> truncated=<true|false> paths=<base64-json-array> -->`. Only the newest trusted, complete, unused `scope` or `bulk-delete` rejection can authorize an `override_guard`; scope paths must match the verdict exactly, while bulk-delete paths may be a verified subset. `scope-lock` cannot be overridden through `files_touched`.
  The failing-run log in the judge prompt is taken only from a pipeline-login comment (not a verdict or state comment), after a metadata read binds the run to the item: same repository, not a fork, and a PR head SHA/branch, `[pr:<n>]` or `[tracking:<n>]` run name, or an `ai/issue-<n>` or `orchestrator/project-<n>` branch. A matching issue title alone cannot bind a run; at most three candidates are tried, otherwise the log is omitted (`op=run_log outcome=omitted`).
- For the unblock judge, both Claude and Codex run via `scripts/clarify_isolated_run.sh`; the Claude credential stays in the host relay, not the model container. Claude unavailability (exit 75) falls back to isolated Codex; other isolation failures produce no verdict. Invalid `UNBLOCK_JUDGE_TIMEOUT_SECS` falls back to the 1500-second in-container limit before either engine starts.
- Issue and project hand-offs post the needed phase label and resume comment before removing the scan-visible block label, except `/approved`: the block label must be removed before the command triggers `implement.yml`, which refuses guard labels. If its POST fails after removal (including a concurrent removal reported as HTTP 404), `unblock_run_ops` restores the block label (and alerts CRITICAL if restoration fails). Failed prerequisite writes stop dependent operations; PR hand-offs dispatch review before removing the label.
- Bulk-delete override approvals are a non-empty subset of both the trusted guard-rejection paths and the failed implement run's `destructive-rejection-issue-<n>` Actions artifact paths. Artifact listing is scoped to the rejection run. The judge's `Approved deletions: [...]` and `Rejected run: <id>` comment is re-verified at spend time; missing, expired or mismatched artifacts fail closed. The scope override path still requires exact equality.
- The unblock model's Claude profile is read-only and runs without GitHub or Telegram credentials; Codex runs through `scripts/clarify_isolated_run.sh` in a network-isolated container with a host-side OpenRouter broker. Literal or encoded provider credentials in parsed verdict fields are rejected before GitHub writes; isolation failure never falls back to host Codex. Failed item/project comment reads stop actuation; a failed project marker can be reconciled on a later run if the item remains blocked after the item verdict has been acted on. A failed review dispatch retains the PR's block label, and failed prerequisite writes stop later resume actions and notifications. An issue body can bind to a tracking project only when the issue carries `ai:orchestrator-managed` and the project's V2 state lists the issue (otherwise actuation skips); PRs targeting a project bind only when GitHub reports a same-repository `ai/issue-<n>` head and <n> is listed in project V2 state authored by the pipeline login; other project comments cannot establish membership or supply the model prompt state. The poller and judge must use the same GH_PAT identity; a mismatch fails closed. Bulk-delete verdict comments carry an `Approved deletions: [...]` line; both implementation guards require every staged deletion in that list and refuse the override when any deletion is under `.github/`, `.claude/` or `workflow-templates/` (case-insensitive), otherwise the normal threshold applies. The `ai:operator-step` tracker is accepted only when its creator is trusted; it is excluded from issue-opened clarification. New operator-step entries are immutable keyed comments; the highest-id trusted comment for a key supersedes earlier comments and legacy body sections, so simultaneous writers cannot overwrite each other's entries. A stale tracker listing fails closed after bounded retries; duplicate trackers with unmigrated entries are not closed. The scan dispatches at most one judge when no judge is active; `UNBLOCK_JUDGE_MAX_DISPATCH_PER_TICK=0` disables dispatch. An incomplete creation-ordered 30-comment window defers dispatch until a paginated REST history read verifies one rotating candidate; an older wait marker can have been refreshed in place outside the window. A failed/partial read retries on another tick.
  On ticks with no open tracking issue, `Run unblock scan without active projects` runs the scan alone with `UNBLOCK_SCAN_SWEEP_ONLY=true`.
- The poller rechecks the project binding before filing a trusted fix-up request and remembers definitive rejections in `unblock_fixup_rejected_ids` (last 50); unverifiable PR reads retry next tick. An untrusted head or author targeting a project branch is never bound to project state: issue-creating verdicts close the PR with a fixed explanation and WARNING, while other non-close verdicts are skipped. A trusted same-repository head without a valid `ai/issue-<n>` ref remains blocked for human handling.
  A malformed trusted fix-up request is skipped with `UNBLOCK_PROJECT action=fixup comment=<id> outcome=invalid_request`; parsing failures log `outcome=request_parse_failed`, without printing comment text.
- The judge extracts membership state only from tracking comments posted by its authenticated GH_PAT login. Before adopting a fix-up request for any non-tracking item, the poller uses the same already-fetched authenticated V2 state to decide whether the item is a project issue or requires a PR read. An unavailable trusted snapshot defers the request; neither a forged membership in the working state nor a self-consistent V2 snapshot from another author can bypass the PR check. A failed PR read or a mismatch between the working integration branch and the verified PR/trusted-state branch defers without permanently rejecting the request; definitive non-membership is recorded in `unblock_fixup_rejected_ids`.
- **Standalone issues answered by the clarify-respond worker (issue #6262 follow-up).** With `STANDALONE_CLARIFY_RESPOND_ENABLED` (default `true`), `clarify.yml`'s auto-decide step only delegates (`delegated=true`) and its "Clarification required" Telegram alert is skipped (`AI_PHASE_GATE_V1 phase=clarify gate=tg_alert reason=delegated_to_clarify_respond`; `reason=auto_answered` after a RECOMMENDED-only answer). `orchestrate_clarify_respond.yml`, which the questions comment already triggers, decides its mode in "Check orchestrator metadata" (outputs `mode=orchestrator|standalone|skip`, `respond`, and the unchanged `is_orchestrator`): standalone needs clarify's questions comment (first line `<!-- ai:clarification-questions -->`, so plan-stage questions are not answered here), an open issue without `ai:orchestrator-managed`, no `<!-- ai:clarification-human-answer -->` (clarify appends it after a human `/reclarify`) and neither `STANDALONE_AUTO_DECIDE_ENABLED` nor `STANDALONE_CLARIFY_RESPOND_ENABLED` set to `false`. Every later step runs on `respond == 'true'`. Standalone mode reads the full comment thread once (needed by the poster's backup loop guard and the AD comment; a failed read fails the run before the model), and both modes get a host-side GITHUB FACTS block from `scripts/clarify_github_facts.py` (one aliased GraphQL call for referenced issues, PRs and branches, plus at most 5 run reads; fail-open), because the model runs network-isolated without a GitHub credential. The model step is `continue-on-error` in standalone mode only: when it fails, "Standalone RECOMMENDED fallback" posts the RECOMMENDED options, or pages `CRITICAL` when a question has none. After the poster answers, "Record standalone auto-decisions" runs `scripts/auto_decisions.py from-answers` and `render`, so each decision becomes an `AD-<n>` entry naming its decider (`clarify-respond on claude|codex`, `clarify-respond, semantic cache`, `RECOMMENDED fallback`), and each `SETUP REQUIRED:` bullet from the worker becomes a deduplicated `SETUP-<n>` item under "Setup required", which `pr-section` repeats in the PR body. Prompt contract (`prompts/mode-clarify-respond.txt`): credentials and setup never ESCALATE; the worker decides with an UPPER_SNAKE_CASE placeholder secret or variable read with no default (skip, or fail closed when skipping weakens a security control) and lists it under `SETUP REQUIRED:`; an issue with no stated intent (the release gate's `[E2E Clarify Negative Test]` body "Make it better.") has every what-to-do question escalated. `prompts/mode-clarify.txt` no longer emits `BLOCKED:` for credentials, undecided branch names or future commits; `BLOCKED:` remains for auth-walled content the task depends on. `orchestrate_clarify_respond.yml` now also stages `clarify_data_provision_guard.py`, which consumer repositories never had, so the data-provision guard was silently skipped there. Tests: `tests/test_clarify_respond_standalone.py`, `tests/test_clarify_github_facts.py`, `tests/test_auto_decisions.py`.
- **Issue status sync on PR close (#5776 / #6632).** `issue_pr_status.yml` applies `ai:merged` / `ai:closed`, closes the issue and finalizes its AI-memory lineage only for a real completion: a PR closed without merging, a PR merged into the default branch (`github.event.repository.default_branch`, passed through `env:` as `PR_DEFAULT_BRANCH`; `main` when the payload lacks it), or an orchestrator-managed child merged into its integration branch (the `Integration branch:` its body declares, or any `orchestrator/project-*` branch when the body declares none). A merge into any other branch leaves the linked issue's labels, state and lineage unchanged and logs that it did so; the lineage step reads the new `LINEAGE_FINALIZE_ISSUE_NUMBERS` list (telemetry `reason=non_completion_merge` when it is empty), while `LINKED_ISSUE_NUMBERS` keeps its meaning. `extract_repo_scoped_issue_refs_from_text` (`scripts/gh_helpers.sh`) no longer counts an issue URL followed by a `#fragment` (for example `#issuecomment-…`) or a `?query` as a linked issue; closing keywords and bare `/issues/N` URLs still count.
- **Activation verification (port P4) and operator steps (Q33).** After a merge into the default branch (`issue_pr_status.yml` job `activation-verify`) and at project completion (poller `run_project_activation_verify`, after every `emit_orchestrator_completion_lessons`), `scripts/activation_verify.sh` grades the work LIVE or DORMANT (the `ACTIVATION_VERIFY` role: Claude by default through `claude_run_selected`, isolated codex when Claude is unavailable; consumer PR-mode runs get no pool credential, and so stay on codex, until their `ai-issue-pr-status.yml` grants `id-token: write`). The poller also retries completed projects with trusted partial verdict comments while fewer than three exist and until 30 minutes after the first partial comment; a failed follow-up write may be retried within that window, but failures without an initial partial comment are not retried after completion. PR mode gets the merged file list from the paginated PR-files API, falling back to the merge diff only for a merge commit or a known single-commit PR; project mode reads the final PR's file list or the state's planned file hints when no final PR exists. Missing/incomplete scope skips verification rather than grading only part of a rebase. Project verification waits for a complete tracking-comment fetch before deduplicating, trims trailing whitespace from verdict comments, and uses a unique worktree and runtime directory that are cleaned up after the run. The model's OpenRouter key is redacted from normalized text before posting it to GitHub. Because the read-only model sandbox omits symlinks from its snapshot and synthetic git tree, the script lists the target's tracked symlinks on the host from git objects (at most 200, no GitHub API call, fail-open to an empty list) and passes them in the context as `tracked_symlinks` / `tracked_symlinks_truncated`, so a tracked link is never reported missing. Markers: `<!-- ai:activation:v1 verdict=<V> source=<pr-N|project-N> -->` on a complete verdict comment (the poller trusts only a terminal marker by a repository-associated author), `<!-- ai:activation:v1 partial=true source=<pr-N|project-N> -->` on an incomplete verdict, `<!-- ai:activation-fix:v1 source=... -->` as the first line of the code-gap issue (a merge that closes such an issue is not verified again), and the `ai:operator-step` issue (`<!-- ai:operator-step:v1 -->`; entries are immutable keyed comments starting `<!-- ai:operator-step:entry key=<key> -->`, and the highest-id comment by the pipeline login for a key wins) written only by `scripts/operator_step_issue.py`. Operator-step drain (plan item 4d): the verifier prompt never lists promoting `main`, tagging `stable` or the consumer sync (`AI Update Workflows` / the daily wrapper sync) as a gap, because the pipeline does them. `upsert --source-sha` records the merge commit as the entry's second line, `<!-- ai:operator-step:source-sha=<sha> -->` (PR mode: `MERGE_SHA`; project mode: the checked-out default-branch `HEAD`). Each nightly `scripts/promote_main_cycle.sh` run that resolves the `stable` tag runs `operator_step_issue.py tick` with the tag commit before its code-change guard. The cycle first fetches commit history only (`--filter=tree:0`, `--unshallow` on a shallow checkout). For each key whose newest pipeline-authored comment has a source commit that `git merge-base --is-ancestor` finds in the tag commit, `tick` appends a superseding comment with every step ticked and `<!-- ai:operator-step:done stable=<sha> -->`; older comments are never edited. Entries without a source commit (written before this change, or by the unblock judge's `operator_step` verdict) are never ticked automatically, and the needs-human digest is untouched. `tick` costs at most one identity read, one tracker listing, one paginated comment read and one comment write per ticked key, never creates a tracker, and logs `OPERATOR_STEP_TICK key= outcome=ticked|already_done|no_source_sha|not_ancestor|unknown_commit stable=`. It is fail-open: a fetch or tick failure is a `::warning::` and the cycle continues; it rides `PROMOTE_CYCLE_ENABLED` (helper path `PROMOTE_CYCLE_OPERATOR_STEP_HELPER`, default `scripts/operator_step_issue.py`). A failed fix-issue lookup does not create another issue or finalize the verdict; operator steps and a non-terminal comment still surface the failure (PR mode needs a rerun after recovery). The operator-step writer reconciles observed duplicates and reads its just-created issue directly if the label list lags, with bounded retry backoff. Whole-body GitHub PATCHes are not atomic across independent writers: concurrent upserts can still lose an entry. `ai:operator-step` is excluded from issue-opened clarification. Kill switch `ACTIVATION_VERIFY_ENABLED` (default `true`).
- `scripts/security_audit.sh` exposes `SECURITY_AUDIT_OUTPUT_MODE=findings-json` for the default-on orchestrator project security pass. It accepts an optional project-spec file, supports a fail-closed explicit `SECURITY_AUDIT_DIFF_BASE`/`SECURITY_AUDIT_DIFF_HEAD` range, optionally narrows that range with `SECURITY_AUDIT_DIFF_SINCE` (only range files changed since that commit stay in scope; fails closed on an unresolvable or non-ancestor commit) and re-verifies `SECURITY_AUDIT_PRIOR_FINDINGS` (a JSON array of earlier findings whose files stay in scope and which the prompt asks the model to re-emit under the same ID if they persist, alongside every remaining instance of the same class; fails closed on malformed input), applies the existing validation/exclusion/confidence/scope filters, and atomically publishes `security_audit_findings.v1` to `SECURITY_AUDIT_FINDINGS_OUT` (with `SECURITY_AUDIT_LINE_OWNERSHIP=project`, the default, each finding carries an additive boolean `advisory` and the payload a top-level `line_ownership` summary; older consumers ignore both). This mode performs no GitHub tracker, label, follow-up, last-SHA, or notification side effects; the default `issues` path remains the production weekly mode.
- The security audit chunks explicitly scoped changed/prior-finding/fix-cycle files over 2 MiB, and in full scans every other filter-eligible oversized text file. Explicitly scoped files take the cap budget first and fail closed above the per-file or total cap. A scoped file of any size that the credential/hidden-path filter withholds (for example a committed `.env.example`) is never exported and never fails the audit: it is listed as not inspected (`filtered_scoped`, reason `credential_filter` or `hidden`; findings-json `coverage.scoped_filtered_skipped`) and does not mark coverage partial. Other oversized files that are binary (a NUL byte in the first 8 KiB) or would pass a cap, and in incremental scans every unscoped oversized file, are listed as a coverage note (`unscoped_oversized`, each with a `reason`).
- The security audit runs on the Claude engine by default (`SECURITY_AUDIT` role in `.github/ai/claude_engine.json`, Opus 5.5 at `high`, read-only tool profile, same credential-free isolated container as codex via `claude_run`). `scripts/security_audit.sh` resolves the engine with `ai_engine_for_role SECURITY_AUDIT` (project labels via `AI_ENGINE_LABELS`, so `ai:codex` keeps a project's audit on codex; then `AI_ENGINE_SECURITY_AUDIT`, `AI_ENGINE`). Any Claude failure reruns the same prompt on codex (`openai/gpt-6-sol`) and logs `AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=`: `all_gated` (every pool account at or above `gate_utilization` 0.95, passed in as `CLAUDE_POOL_REASON`), `no_credential`, `isolation_unavailable`, `all_accounts_failed`, `timeout`, `crashed_rc_<n>`, `missing_output`, `malformed_output`, or `schema_mismatch` (not a JSON array of objects; one outer ```` ```json ```` fence is stripped first; a reply that is not JSON as a whole falls back only when it holds no top-level array of objects with a `finding_id`, see below). A valid empty array from Claude counts as clean, with no codex cross-check. The run log names the engine that produced the findings (`security-audit: engine=claude|codex`). `security-audit.yml` installs the CLI and fetches the pool only when the role resolves to Claude; `orchestrate_poll.yml` already fetches them for its judges and now also when `SECURITY_AUDIT` resolves to Claude.
- On a Codex execution failure, `scripts/security_audit.sh` emits only a sanitized stderr tail (at most 40 lines and 4,096 rendered bytes) between `security-audit: codex-stderr-tail begin/end` markers and adds `provider=402|401|429|5xx|unknown` to the existing failure line; successful runs emit no tail.
- `.github/workflows/workflow-log-analysis.yml` now also has a source-repo-only weekly retro path (cron `0 9 * * 1`, gated by `WORKFLOW_RETRO_ENABLED`, default `true`). `WORKFLOW_RETRO_CRON` defaults to the same cron string and must stay in sync with the trigger because GitHub does not interpolate vars into `on.schedule`. The workflow builds retro context with `scripts/workflow_retro.py`, renders the narrative through `prompts/mode-workflow-analysis.txt` in retro mode using `WORKFLOW_RETRO_MODEL` / `WORKFLOW_RETRO_REASONING` (defaults `openai/gpt-6-luna` / `medium`), and posts into the stable `AI Workflow Weekly Retro` tracker issue (`ai:retro`, marker `<!-- ai:retro-tracker:v1 -->`). Zero-activity windows (no workflow runs and no merged PRs; `has_activity: false` in the `workflow_retro.v1` JSON) skip the LLM pass and the tracker comment when `WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY=true` (default), leaving only a `WORKFLOW_RETRO_SKIP_V1:` line in the run log and no Telegram alert. After the source-repo retro, the `Consumer retro fan-out` step (gated by `WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED`, default `true`) runs `scripts/workflow_retro_fanout.sh`: for each repo in `.github/ai/consumer_repos.json` (source repo excluded) it builds a per-repo retro from the same collect-logs artifact, honors the consumer's own `WORKFLOW_RETRO_ENABLED` repo var (one fail-open `gh api` GET per consumer per week), applies the same no-activity skip, and upserts the week-marked comment on that consumer's `AI Workflow Weekly Retro` tracker via `GH_PAT` (§14 repo scope). Per-repo outcomes are logged as `WORKFLOW_RETRO_FANOUT_V1: repo=… status=posted|refreshed|up_to_date|skipped_no_activity|skipped_disabled|failed`; individual failures fail open and the step errors only when every attempted consumer fails. Both `.github/workflows/internal-clarify.yml` (source repo) and the consumer-facing gate in `.github/workflows/clarify.yml` skip `ai:retro` / `ai:security-audit` issues so tracker upkeep never recurses into the clarify/plan pipeline.
- `scripts/orchestrate_poll_process.sh` gates last-resort `orchestrator/project-*` branch rebuilds behind `BRANCH_REBUILD_ENABLED`, `BRANCH_REBUILD_THRESHOLD_HOURS`, and `BRANCH_REBUILD_COOLDOWN_HOURS`. Audit snapshots are persisted as `BranchRebuildAuditV1` in `ai-memory/schemas/branch_rebuild_audit.v1.json` (this shipped artifact supersedes the old plan placeholder name `BRANCH_REBUILD_AUDIT_V1`; there is no literal runtime marker with that string).

**Unblock judge: CI-failure reissue and its owning PR (#7032).** Before a
standalone, non-security `reissue`, `scripts/unblock_judge.sh`
(`unblock_resolve_ci_owner`) looks for the PR that owns a CI-failure issue:
label `ai:check-triage` / `ai:check-triage-escalated`, or the line-anchored
`<!-- check-failure-triage:pr=<n> -->` marker, plus the body's
`**Pull request:**` line into this repository. It trusts that body text only
when the issue author is the pipeline login, `github-actions[bot]` or an
OWNER/MEMBER/COLLABORATOR (`op=ci_owner outcome=none reason=untrusted_author`
otherwise), then reads the same-repository PR once. A PR merged into the
default branch or the issue's declared integration branch counts as superseding
the issue only when `_pr_checks_completed` returns `ok` (not `allow_all`) with
at least one check-run on its head and the named CI check
(`AUTO_MERGE_REQUIRED_CI_CHECK`, state `PR_CHECKS_LAST_CI_STATE`) ran and
succeeded on it in an `AUTO_MERGE_WAIT_WORKFLOWS` run on that head
(`PR_CHECKS_CI_RUN_IDS`, as the auto-merge wait binds it; an unreadable runs
listing never supersedes); with no named check configured (`disabled`, the
consumer default) the issue is never superseded. `unblock_judge.yml` forwards
both variables. The issue is then closed
`not_planned` without a replacement
(`UNBLOCK_JUDGE item=<n> op=reissue outcome=superseded pr=<n> head=<sha>`).
Otherwise the replacement carries the marker, and, when the PR is open and its
head ref exists, a standalone `- Integration branch:` line with the PR head
ref (never the base ref or model text). Unverifiable PR facts keep the
unchanged reissue.

- The security audit's Claude reply check (`security_audit_try_claude` in `scripts/security_audit.sh`) first takes the reply as one JSON array, or one outer ```` ```json ```` fence around it. Otherwise it uses the last top-level JSON array whose items are all objects with a `finding_id`, scanning with `json.JSONDecoder.raw_decode` from each `[` and `{` and skipping arrays nested in an array or object that parsed; an empty array qualifies only when it starts a line, nothing but a closing fence follows it, and no non-empty candidate came before it. A reply with no such array falls back as `malformed_output`, so a `[]` in prose or a cut-off reply never reads as a clean audit. Orchestrator run 38113094454 and others fell back to codex with `malformed_output`. Each fallback prints `SECURITY_AUDIT_CLAUDE_FALLBACK reason=` on stdout, because the poller sends the audit's stderr to `security_pass_audit_<N>.err`; a reason from inside `claude_run` comes from `AI_ENGINE_LAST_FALLBACK_REASON`, which `ai_engine_fallback` now sets. A rejected reply also prints `SECURITY_AUDIT_CLAUDE_REJECTED` and its first and last 300 characters (printable ASCII, whitespace collapsed, credentials redacted before the excerpt is cut, with the `security_audit_sanitize_log_value` rules plus `sk-`, AWS, Google, Slack and private-key patterns), and keeps its first 64 KiB at `SECURITY_AUDIT_CLAUDE_REJECTED_OUT` when set; the security pass sets `${RUNTIME_DIR}/security_pass_claude_rejected_<tracking>.txt`. Tests: `tests/test_security_audit_workflow_contract.py`.

An extracted candidate, empty or non-empty, also requires every earlier opener that failed JSON decoding to close with matching brackets before the candidate, treating quoted text both as JSON strings and as literal prose. A findings array nested inside a truncated wrapper falls back as `malformed_output`.

- **Unblock judge protected-path handoff (#7060).** With `UNBLOCK_PROTECTED_HANDOFF_ENABLED=true` (default `false`), a `scope-blocked` issue whose newest pipeline-authored guard comment is an `ai:guard-rejection:v1 ... guard=automation-path` marker (`unblock_ledger.py protected-rejection`, parsed separately from `REJECTION_RE`, so `override_guard` stays unavailable) gets an operator handoff for `descope` / `operator_step` instead of a fix-up. The ops run in this order: the `ai:operator-step` entry, then a comment ending in `<!-- ai:unblock-wait:v1 item=<n> reason=operator_handoff run=<r> -->`, then a WARNING; a failed operator-step write skips the hold comment. The verdict comment is recorded before the ops run, so a failed handoff is not retried with the same verdict (the ledger drops it from the next menu); the WARNING is still sent, saying the handoff is incomplete. The issue stays blocked. A later run with that trusted marker newer than the rejection skips before `decide` (`reason=operator_handoff_pending`) and refreshes the hold comment in place, so the poller's scan (`UNBLOCK_JUDGE_RETRY_HOURS` on the marker's `updatedAt`) does not re-dispatch it. Other stops ignore the rejection, so an old handoff cannot hold a later, unrelated block. `dormant_until` is set only for an `_UNSET_OPERATOR_STEP` placeholder.
- **Pull-request dependency hold (issue #7228).** A trusted standalone issue (author OWNER, MEMBER, COLLABORATOR or the GH_PAT login) may carry exactly one `- Depends on PR: #N` line and exactly one ``- Requires on main: <path> contains <fixed string>`` line. `scripts/pr_dependency.py pr-dependency --issue-json <file> --repo <owner/repo> --issue-number <n> [--trusted-login <login>] [--number-only]` prints `{"status": "none" | "ready" | "held", "reason", "depends_on_pr"}` and imports only the standard library. It is `ready` only when `pulls/N` reports the PR merged with `base.ref` equal to `base.repo.default_branch` in this repository and the raw `contents/<path>?ref=<default branch>` read (made only after those checks) contains the string; malformed or repeated lines, a self-reference, a path with `..`, `//`, a leading `/` or a `.git` segment, a non-ASCII or backticked string, an untrusted author, an unknown login and any failed read are `held`. `ai:orchestrator-managed` issues return `none`. Clarify ("Decide clarify route"), plan ("Validate planning phase label", before any label change) and both implement gates run it with `python3 -I -B` from trusted copies; clarify's hold keeps `ai:clarification` and removes `ai:blocked` so the unblock scan never sees the issue. The standalone stall poller skips a held issue (`STALL_SKIP ... reason=pr_dependency_held detail=<reason>`) and, once ready, posts one `/reclarify` with `<!-- ai:pr-dependency-released:<issue> -->`. Only a marker whose `user.login` is the GH_PAT login counts, and nothing is posted when that login or the complete comment history cannot be read. If the string later disappears from the default branch, the re-clarify holds again and the marker blocks a second release; edit out the declaration or post `/reclarify` once the condition is restored. The poller passes the candidate GraphQL batch's existing `author_login` / `author_association` to the helper. The unblock scan (`run_unblock_scan`) adds the issue body and author to its existing GraphQL batch and drops a held issue from its search results before `unblock_scan.py select` (`UNBLOCK_SCAN item=<n> kind=issue outcome=skip reason=pr_dependency_held`), so the hold does not depend on clarify removing `ai:blocked`, and a held issue never takes the selector's single dispatch slot from other blocked items (a failed filter write skips the tick with `reason=pr_dependency_filter_failed`). Declaration lines are read after CRLF is normalized to LF. `scripts/security_dependency.py` (`- Depends on: #N`) is unchanged; its matcher needs a colon right after "on", so the two declarations never collide.
- **Protected-path handoff after a merged fix-up (#7287).** With `UNBLOCK_PROTECTED_HANDOFF_ENABLED=true`, the merged-fix-up arm of `scripts/unblock_judge.sh` checks the `protected-rejection` result before running `unblock_actions.py followup`. When that result is `ok`, it first runs `unblock_try_regrant` (which reads a re-issue's source issues, at most 8 reads); a grant the current guard accepts resumes the item and marks the wait done (`outcome=followup_regrant`); a failed regrant actuation (`UNBLOCK_REGRANT_RESULT=failed`) leaves the wait open (`outcome=skip reason=followup_regrant_failed`). Otherwise, on a `scope-blocked` issue whose newest trusted guard comment is an automation-path rejection: a pending hold marker marks the fix-up wait done and skips (`outcome=skip reason=operator_handoff_pending fixup=<N>`); otherwise, when the output's `prior_handoff_verdict` (the newest pipeline-authored `scope-blocked` verdict marker after the rejection, only `descope` / `operator_step`, else null) is set, `unblock_actions.py followup-handoff --fixup <N> --verdict <v>` produces the #7060 handoff ops (operator step under its own key `unblock-handoff-<item>`, so the original `unblock-<item>` entry and its placeholder are kept, hold comment, WARNING with `handoff_retry: true`) instead of a resume, and the wait is marked done (`outcome=followup_handoff`). A failed write leaves the wait open for the next run (`reason=followup_handoff_failed`, WARNING "the next judge run retries it"). Flag off, or any other case, keeps the existing follow-up resume.
- **Re-grant resume retry in the merged-fix-up arm (#7287).** When `unblock_try_regrant` returns 1 because the rejection run is `already_regranted` (an earlier re-grant posted its marker but its resume failed), and no hold marker is pending, the arm logs `op=regrant run=<r> outcome=resume_retry reason=already_regranted` and falls through to the normal `unblock_actions.py followup` resume instead of the operator handoff.
- **PR-dependency release route (#7328).** The standalone poller releases a held `- Depends on PR: #N` issue with a comment that is exactly `/reclarify` + newline + `<!-- ai:pr-dependency-released:<n> -->`, posted with `GH_PAT`. The clarify job predicate (`clarify.yml`, `internal-clarify.yml`, `workflow-templates/ai-clarify.yml`, kept identical by `tests/test_phase_wrapper_predicate_contract.py`) adds one clause that admits a `User` comment starting with that marker from an author without an OWNER/MEMBER/COLLABORATOR association; such a comment runs in the concurrency group `ai-clarify-<n>-release-<comment id>`, so it cannot cancel a running clarify. In "Decide clarify route", the block between `# BEGIN/END pr-dependency release classification (#7328)` strips `\r` and trailing whitespace and, only for an exact case-sensitive match naming this issue, reads the token login with one `GET /user`. A matching author sets `is_automated_release=true`, `is_forced_reclarify=false` and route trust (log `AI_PHASE_GATE_V1 phase=clarify gate=pr_dependency_release reason=verified outcome=restart`), so the questions carry no human-answer marker and auto-decide plus clarify-respond answer them. A mismatch or failed read (`reason=author_mismatch|identity_unavailable`) leaves a trusted author's comment an ordinary human `/reclarify` (`outcome=human_reclarify`) and skips an untrusted one (`outcome=skip`, then `gate=route reason=untrusted_comment_author`); an untrusted comment writes no labels and cannot take the orchestrator fast path. A failed release run that the poller replays (`/reclarify [auto-requeued-by-poller]` + `<!-- ai:reclarify-replay:v1 source=<id> -->`, posted only for a trusted association) is a release again when the replay comes from the `GH_PAT` login and one `GET issues/comments/<id>` shows that comment is this issue's exact release marker by the same login (`reason=verified_replay`; otherwise `reason=replay_source_unverified outcome=human_reclarify`). The step also writes `comment_route_trusted`, and the three AI-memory run-event steps (`Record clarification run start|completed|failed`) skip when it is `false`; when it is empty (the job failed before the route step finished), `Record clarification run failed` also skips for a comment whose author has no OWNER/MEMBER/COLLABORATOR association. A failed release from such an author is not requeued: "Queue failed reclarify for poller" logs a `::notice::` and stops. The README wrapper examples carry the release clause too, and `test_readme_core_wrapper_examples_carry_reusable_predicates` requires the exact reusable predicate. The `security-dependency-released` marker is unchanged and still routes as a human `/reclarify`. The other #7249 activation gap, `_AI_ENGINE_EXIT_REFUSED` missing from `scripts/ai_engine.sh` on `main`, waits on PR #6667 (project #6664); issues that require it stay held until then.
- **Unblock judge `wait` verdict and operator-step holds (Q116, Q126).** With `UNBLOCK_JUDGE_WAIT_ENABLED` on (the default; `false` turns it off), the judge can answer `wait` with a `wait_for` condition it checks itself: `pr_on_branch` (pull request merged and its merge commit on the branch: `pulls/<n>` plus `compare/<branch>...<sha>`, status `behind` or `identical`), `item_closed` (issue or pull request closed) or `placeholder_retired` (`git grep` finds the `_UNSET_OPERATOR_STEP` name nowhere in the judge's default-branch checkout `TARGET_DIR` outside `CHANGELOG.md` and `changelog.d/`, or, while code still names it, `actions/variables/<NAME>` or `actions/secrets/<NAME>` exists; a read error keeps waiting, Q130). The verdict comment ends with the hold marker `<!-- ai:unblock-wait:v1 item=<n> hold=<kind> ref=<ref>[ done| expired] -->` instead of an `ai:unblock:v1` marker, so a hold is not a round, never counts toward the item or project caps, and gets no tracking-issue copy. The item keeps its block label (stall recovery already skips `ai:blocked`). Each judge run on a held item checks the condition first: met posts the stop's resume command (`unblock_actions.py hold-followup`) and marks the hold `done`; unmet refreshes the hold in place (the scan counts the marker from its last edit, so it is re-checked every `UNBLOCK_JUDGE_RETRY_HOURS`); after `UNBLOCK_JUDGE_WAIT_MAX_DAYS` (default 14), when a pull request closed unmerged, or with waits turned off, the hold is marked `expired` and the item is judged again. A failed read never resumes or ends the hold before `UNBLOCK_JUDGE_WAIT_MAX_DAYS`; it refreshes the open hold in place, so the scan waits `UNBLOCK_JUDGE_RETRY_HOURS` before re-checking (a failed refresh logs `reason=hold_refresh_failed`). The ledger offers `wait` at most `MAX_HOLDS_PER_ITEM` (3) times per item and never for a condition the item already saw met; `marker` refuses `wait`, and a forged `verdict=wait` round marker is ignored. When the fix-up of an `operator_step` verdict whose placeholder ends in `_UNSET_OPERATOR_STEP` merges, the judge posts a `placeholder_retired` hold instead of the resume command (the placeholder is read from the trusted verdict comment's "Stays off behind" line); an `_ENABLED` placeholder still resumes at once.
- **Unblock judge closes resolved check-triage issues (Q129).** Before checking a hold or asking the model, the judge runs `scripts/check_triage_resolution.py gate` (the #7095 gate clarify runs, `CHECK_TRIAGE_RESOLVED_GATE_ENABLED`) on an open `ai:check-triage` issue, with the default branch from `TARGET_DIR` (`git symbolic-ref`, else one repository read). `resolved` / `already_resolved` ends the run with the issue closed with evidence and no round used; exit 3 (verified, write failed) stops the run so the next scan retries; anything else judges the item as before. Log: `UNBLOCK_JUDGE item= op=triage_resolution outcome=closed|skip|judge status= [rc=] [reason=write_failed|default_branch_unknown]`.
- **Unblock judge closes heal issues a sibling already fixed (Q148).** After the check-triage gate and before checking a hold or asking the model, the judge checks an open `ai:workflow-heal` issue with no tracking issue (`UNBLOCK_HEAL_ALREADY_FIXED_ENABLED`, default on): it lists the repository's heal issues (paginated), runs one `search/issues` read for unblock-judge re-issues, and `workflow_failure_heal.py heal-already-fixed` returns up to three siblings, newest first. A sibling is written by the pipeline login, has the item's `source=` marker and `Failure reason` line (or its fingerprint for kinds without one), directly or through its re-issue chain, and is closed as completed with `ai:merged`; the item itself must be written by the pipeline login. For each candidate one closed-pull-request read looks for an `ai/issue-<N>` pull request merged after the item's `created_at` (a fix that merged before the item was filed did not hold, so the item is judged; no `created_at` logs `status=item_created_unknown`); the first found gets a comment ending in `<!-- ai:unblock-heal-already-fixed:v1 item=<n> sibling=<N> pr=<P> -->` (not posted again when a trusted copy already exists) and the item is closed as completed, with no round used. A failed comment or close stops the run so the next scan retries; every read failure judges the item as before. Log: `UNBLOCK_JUDGE item= op=heal_already_fixed outcome=closed|skip|judge sibling= pr= status=|reason=comment_failed|close_failed`.
- **Resolver union merges of one line are not regressions (project #7355 wave 3).** `scripts/verify_integration_fingerprints.py` `_is_must_contain_resolver_union_merge_false_positive` runs after the post-capture-evolution defense on a `must_contain` miss, in ref mode only. It accepts the miss only when the newest first-parent pickaxe hit for the captured line is a two-parent `[ai-merge-resolve]` merge dated after `captured_at`, the file at the verified ref has a line that contains the whole captured line with text only inserted (`difflib` opcodes `equal` / `insert` only), and one line of the file at the merge's second parent, different from that line and absent from the file at the merge base, keeps the same merge-base line whole as the captured line (so it is the other side's edit of that line), is itself kept whole in it and contains every inserted run, with every character of the merged line matched to the captured line or to that other line (so a duplicated or moved copy of other-side text fails). Anything else (working-tree mode, no `captured_at`, a non-`re.escape` regex, a resolver line identical to the other side's line, an other-side line unchanged since the merge base or derived from a different base line, an unreadable merge base, a single-parent resolver commit, a resolver commit before capture, dropped or changed text, or text the other side does not have) fails closed. Zero API calls; cached in `_RESOLVER_UNION_MERGE_CACHE`. Log: `::warning::FINGERPRINT_RESOLVER_UNION_MERGE_V1 issue=#<n> pr=#<p> file=<path> resolver_commit=<sha> reason=must_contain_line_kept_whole_in_union_with_other_merge_side`.

## Operational lessons learned (categorised)

**General / Tooling**
- Treat the `openai/codex#11151` no-edit regression as closed only with function-style patch tooling; keep `apply_patch_tool_type = "function"` as the settled baseline. Pointers: `scripts/codex_model_catalog.json`, `scripts/write_codex_config.sh`.
- Keep `low` as the default gpt-6-sol verbosity across workflow entrypoints unless a specific phase re-proves the old announce-without-emit failure. Pointers: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate.yml`.

**codex-cli quirks**
- Announce-without-emit is a known codex-cli failure mode on patch-heavy turns; the mitigation is to keep patch tooling explicitly enabled rather than raising verbosity by default. Pointers: `scripts/codex_model_catalog.json`, `.github/workflows/implement.yml`.
- The OpenRouter Responses-path regression was tied to `apply_patch_tool_type: "freeform"`; keep `include_apply_patch_tool = true` and function-style patch wiring in editor phases. Pointers: `scripts/codex_model_catalog.json`, `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`.

**OpenRouter / prompt-cache**
- Prompt-cache hit rate depends on stable prompt ordering and unchanged prefix blocks; preserve cache-friendly layout before adding new dynamic material. Pointers: `probably_unnecessary_but_read_if_stuck.md` (OpenRouter Prompt Cache Instrumentation / Semantic Cache Scope), `scripts/openrouter_prompt_cache.py`.
- `OPENROUTER_PROMPT_CACHE_DISABLED` is the explicit kill switch, and Gemini-family models may skip cache breakpoints when the reviewer path marks them incompatible. Pointers: `probably_unnecessary_but_read_if_stuck.md`, `scripts/review_run_reviewers.sh`.

**GitHub API rate-limits**
- Shared GitHub quota handling is reset-aware: use the repo helpers' `gh_retry` backoff behavior instead of ad-hoc retry loops. Pointer: `scripts/gh_helpers.sh`.
- A retry wrapper must buffer each attempt's stdout and emit only the successful attempt's: `gh api` prints failed responses to stdout, so an unbuffered retry prepends error bodies to the real output (#5495). `gh_retry` does this; an inline `"$@"` retry loop does not. Pointers: `scripts/gh_helpers.sh`, `tests/test_gh_retry_stdout_isolation.py`.
- `gh_api_retry [--idempotent] [--optional] <gh api args>` (`scripts/gh_helpers.sh`; Python twin `scripts/gh_api_retry.py`, shared fixture `tests/fixtures/gh_api_retry/classify_cases.json`) runs `gh api -i` per attempt (no `-i` with `--paginate`), prints only a successful body, and classifies failures: a primary limit waits for the `x-ratelimit-reset` of the bucket in `x-ratelimit-resource` (core / graphql / search), a secondary limit waits `retry-after` (60 s when absent), 5xx/408/network errors back off with jitter (`GH_RETRY_BACKOFF_CAP_SECS`, 120), other 4xx are permanent. It never sleeps after the last attempt and gives up at once when a reset is beyond `GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS` (600). Exit codes: 0 ok, 1 transient exhausted, 2 permanent, 75 rate-limited or optional skip; callers fail loudly on 1/75, and on 2 unless the status was 404 (the review gate reads it from the `GH_API_RETRY` stderr line, the sweep from `GH_API_RETRY_LAST_STATUS`), instead of defaulting. `--optional` skips while the breaker file has a future `<bucket> <reset>` line (written on every limit, including those `gh_retry` / `gh_retry_to_file` / `gh_api_json_to_file` / `curl_gh_api` see, and when `x-ratelimit-remaining` drops below `GH_API_RETRY_LOW_BUDGET_REMAINING`, 100). A POST create (explicit POST, implicit `-f`/`-F`/`--input` POST, `graphql` documents with a mutation operation anywhere (not only first), inline, in `query=@file` or in `--input` JSON, unreadable documents counting as mutations, `gh issue|pr create|comment`, `gh workflow run`, `gh release create`) makes one attempt in `gh_api_retry`, `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` unless marked `--idempotent` or `GH_RETRY_IDEMPOTENT=true` (label/assignee adds carry the prefix). `gh_retry_to_file` now writes only a successful attempt and truncates the file after a final failure. Steps that run before support staging (review gate, validate authorize, implement precheck/safety check, clarify-respond metadata, security-audit resolve) get the helper from the `Bootstrap GitHub API retry helper` step (git clone of `main` here; in consumers the commit a 40-hex job `workflow_ref` pins, else `stable`; exports `GH_API_RETRY_HELPERS`; on failure a shim calls plain `gh api`). `scripts/resolve_integration_ref.sh` exits 75 when rate-limited and 2 on any other API failure; both resolve steps fail with `INTEGRATION_REF_RESOLVE outcome=rate_limited|api_unavailable`, and only exit 1 (declared branch missing) keeps the default-branch fallback. The sweep skips every dispatch on an incomplete active-run snapshot (`AUTOFIX_SWEEP_SNAPSHOT_INCOMPLETE`). Pointers: `tests/test_gh_api_retry.py`, `tests/test_gh_api_retry_py.py`, `tests/test_workflow_gh_retry_fallback_contract.py`.
- Rate-limit alerting is deduplicated by pin/cooldown state, and repeated issue/PR lookups should flow through the poller's batched GraphQL helpers. Pointers: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`.

**Memory subsystem**
- The `ai-memory` branch is the canonical backing store; consumers must fail open when memory reads or writes are unavailable. Pointers: `scripts/memory_helpers.sh`, `scripts/ai_memory.py`.
- `AI_MEMORY_TELEMETRY` and the per-PR review ledger are continuity surfaces, not hard gates; preserve ledger identity across reruns. Pointers: `scripts/ai_memory.py`, `scripts/review_issue_ledger.sh`.
- Lessons-learned memory uses the standalone schema `ai-memory/schemas/lessons_learned_record.v1.json`; review-autofix writes issue-scoped records under `ai-memory/tasks/issue-*/lessons_learned/` via `scripts/ai_memory_lib.py::record_lessons_learned`, and plan-mode prompts treat surfaced same-file lessons as soft priors rather than hard requirements.
- Operator-facing memory hygiene lives in `scripts/ai_memory.py`: `review --since <duration>` lists stale task candidates, `prune --record-id <id>` marks task candidates for the existing monthly `compact --prune true` archival path, `search --query <text>` prefers OpenRouter embeddings when `OPENROUTER_API_KEY` is set and otherwise falls back to keyword ranking, and `export --issue <n>` / `--pr <n>` dumps matching memory records as JSON. `prune` is intentionally additive: it writes a `timestamps.prune_marked_at` marker on candidate records instead of introducing a second maintenance channel.
- Prompt retrieval reads lessons-learned records too: `retrieve_memory_context` (`scripts/ai_memory_lib.py`) appends a `LESSONS LEARNED (soft priors from earlier runs; not requirements)` block for the `planning`, `implementation`, and `reviewer` roles with up to 5 of the newest `tasks/*/lessons_learned/*.json` records whose text or tags match the issue keywords. They use at most a quarter of the role's token budget (`LESSONS_RETRIEVAL_BUDGET_FRACTION`); records keep the rest, and with no matching lesson the context is unchanged. Invalid lesson files are skipped; `LESSONS_LEARNED_ENABLED=false` turns the block off. The retrieve JSON and telemetry gain `lessons_selected` (and `lesson_ids` in the JSON).
- The orchestrator writes a retrospective when a project completes, including clean-skip completions where the judge never runs. `scripts/orchestrate_poll_process.sh` records causes as they happen in state `lesson_events` (newest 20, via `orchestrate_lib.py append-lesson-event`): judge fix-up issue created (`create_judge_fixup_issues_from_verdict`), validation `needs_fixes` diagnosis (`sync_validation_fix_issues_from_comments`), security findings reported (`run_security_pass_inline`, after `SECURITY_PASS_BLOCKED`), and orchestrator stall recovery. On each of the four `status = "complete"` paths, `emit_orchestrator_completion_lessons` turns the events plus the recovery / judge-stall / review-blocked / validation / security counters into `lessons_learned_record.v1` records (`orchestrate_lib.build_completion_lessons`; phase `orchestrator_completion`, kind `project_retrospective`, tags `source:*`, `project:<tracking>`, `file:*`) under `ai-memory/tasks/issue-<tracking>/lessons_learned/`. Record ids are deterministic and existing ids are skipped, so a repeated completion tick writes nothing; a clean project with no events or counters writes nothing. Fail-open, no GitHub API calls; gated by `AI_MEMORY_ENABLED` / `LESSONS_LEARNED_ENABLED`.

**Validation harness Docker lifecycle**
- Validation containers distinguish `/bin/sh -c` from `/bin/sh -lc`; shell choice is part of harness correctness, not a cosmetic variation. Pointers: `scripts/validation_lint.py`, `prompts/mode-validate-generate.txt`.
- npm/yarn/pnpm wrapper shutdown handling and `mongosh` apt-repo constraints are harness invariants; keep the existing SIGTERM/exit-code and package-source rules intact. Pointers: `scripts/validate_driver.sh`, `prompts/mode-validate-fix-harness.txt`.

## Pre-push affected-test gate

Issue #7137, plan phase P1 of `docs/plans/prepush-affected-test-gate-plan.md`.
P2 (orchestrator `files_touched` augmentation), P3 (implement gate), P4
(review autofix, conflict resolver and heal gates) and P5 (integration-branch
post-sync repair) are follow-ups; until they land no pipeline runs the gate.

- **Module.** `workflow-templates/.claude/hooks/prepush_affected_tests.py`
  (live copy `.claude/hooks/prepush_affected_tests.py` via
  `sync-claude-live-copies.yml`), stdlib only. Subcommands `select`, `run`,
  `assertion-diff`, `stamp` and `hook`. Selection, in order: changed test files
  (`changed_test`); the reverse of the heal naming convention (`naming`); test
  files whose text contains a changed path, basename, or basename stem of 4+
  characters outside a stop list as a whole word (`path_text`,
  `basename_text`, `stem_text`), plus importers of a non-`test_` helper module
  in the same test directory that matches (`helper:<module>`); registry guards
  that exist in the repo (`registry:ci_test_wiring`, `registry:inventory_parity`,
  `registry:claude_live_parity`, `registry:workflow_file_size`, and the
  `make generate-check` / `scripts/assemble_agents.py check` commands); the
  inverse of `HEAL_SCOPE_GUARD_TEST_SUBJECTS` for `ci.yml` (`guard_inverse`,
  copied in `CI_GUARD_TESTS` and pinned by a test); `ruff` / ShellCheck on
  changed files (missing tool → `unverified tool_unavailable`). A Python test
  file run under pytest (configured, or `test_` functions with no
  `__main__` runner) is `unverified pytest_unavailable` when pytest is not
  installed. A baseline re-run gets at
  least 90 seconds even after the main budget is spent. A Python test
  file with more than `PREPUSH_TEST_GATE_LARGE_FILE_TESTS` (50) tests keeps only
  the test functions that mention a changed function or file name. Commands:
  `.ai/validate.yml` `prepush_test_command` (`{files}` = quoted selected
  files), then a workflow's `python3 tests/<f>` / `pytest` usage, pytest
  detection, `bash`, the `package.json` jest/vitest/mocha runner or
  `npm test`, `make test`, else `unverified no_test_command`. Units run in
  `PREPUSH_TEST_GATE_SHARDS` (4) threads within
  `PREPUSH_TEST_GATE_TIMEOUT_MINUTES` (15); unfinished units are
  `unverified timeout`. Failures re-run on `--baseline-tree`; those that also
  fail there are `inherited`. Result schema `prepush_test_gate.v1`
  (`status` pass | fail | unverified | skipped). The Markdown summary carries
  test names and paths only, never output tails, and neutralises `::` lines.
  The module makes no GitHub API call.
- **Interactive flow.** After committing, run
  `python3 .claude/hooks/prepush_affected_tests.py stamp`: it refuses
  uncommitted tracked changes, diffs `HEAD` against its merge-base with
  `origin/HEAD` (else `origin/main`, `origin/master`; none →
  `unverified no_merge_base`), runs the gate on the host (the operator's own
  session, like running pytest by hand) with the merge-base exported by
  `git archive` as the baseline, and writes
  `${TMPDIR:-/tmp}/claude-prepush-gate/<sha256(toplevel)[:16]>/<HEAD>.json`
  (0600, directory 0700, never under the repository or its git dir). The
  `PreToolUse` `Bash` hook (its own settings entry, timeout 30) parses pushes
  with `pr_merge_status_guard._guarded_git_invocations` (conservative regex
  plus a warning if that module cannot load; a `git push` that module does
  not parse is checked as a push of `HEAD` (since #6038 the guard parses
  `command git push` and the other transparent prefixes itself);
  quoted words are ignored for that check). It
  resolves each pushed source ref (the guard's `_push_targets` tip, `HEAD`
  for a bare push) to a commit and asks when that commit's stamp is missing,
  the ref does not resolve, the guard cannot resolve the push options or
  destination (an abbreviated or unknown option such as `--follow-tag`), or
  the stamp records failures this change caused. A bulk push (`--all`, `--mirror`, `--branches`, a pattern) and a
  push that includes tags or other non-branch refs (`--tags`, a
  `refs/tags/` refspec or `--follow-tags`, also next to a branch) always ask. A `pass`,
  `unverified` or `skipped` stamp, a non-push command and
  `CLAUDE_PREPUSH_TEST_GATE=off` give no decision. Internal errors and
  malformed payloads fail open with a `systemMessage`. The hook never runs
  tests or repository code. CLAUDE.md §31 (the instruction to stamp before
  pushing) is an operator step for an interactive session, which must add it
  to both CLAUDE.md copies together.
- **Container wrapper.** `scripts/prepush_test_gate_run.sh --worktree --module
  --image --base --out-dir [--timeout-minutes] [--deps-root]...` refuses a
  module inside the worktree (`reason=module_untrusted`), snapshots the
  worktree through `review_untrusted_workspace.py`, exports the merge-base,
  runs `docker run --network none --read-only --cap-drop ALL --security-opt
  no-new-privileges` with no runner environment (module, baseline and
  dependency roots read-only), and copies back only `result.json` and
  `summary.md` (regular files, 1 MiB). The changed-file list is the worktree
  against the merge-base (uncommitted tracked edits included); `Makefile` and
  `.ai/validate.yml`, which the snapshot filter leaves out, are copied into the
  snapshot; a non-zero container exit discards the result
  (`container_rc_<n>`), since the gate itself exits 0 for every outcome. It always exits 0; wrapper failures
  write a synthetic `unverified` result (`docker_unavailable`,
  `snapshot_failed`, `container_survived`, ...). Unused until P3.
- **PR-only parity tolerance.** `tests/claude_template_drift.py`
  `pending_template_paths()` returns the drifted template paths the PR changed
  or that already drift at `CLAUDE_PARITY_PR_BASE_SHA`; it is empty unless that
  variable is a 40-hex SHA, and on any git error. `ci.yml` sets it from
  `github.event.pull_request.base.sha` on the steps that run the live-copy
  parity, merged-PR guard, PR-watch guard, `gh api` guard and SessionStart
  tests, so push CI is strict; the release gates never set it. The
  `CLAUDE.md` byte-equality check stays strict.

## Repo-tree (auto-generated)

Active workflow files (regenerate with `make generate`):

<!-- TREE:START id=workflows -->
```
.github/workflows/audit_consumer_drift.yml
.github/workflows/auto-release-stable.yml
.github/workflows/cancel_on_pr_close.yml
.github/workflows/check-failure-triage-historical.yml
.github/workflows/check_failure_triage.yml
.github/workflows/ci.yml
.github/workflows/clarify.yml
.github/workflows/claude-engine-smoke.yml
.github/workflows/comprehensive-test-and-release.yml
.github/workflows/drift-audit.yml
.github/workflows/forward-merge-stable-to-main.yml
.github/workflows/implement.yml
.github/workflows/integration-pr-readiness.yml
.github/workflows/internal-cancel-on-pr-close.yml
.github/workflows/internal-check-failure-triage.yml
.github/workflows/internal-clarify.yml
.github/workflows/internal-implement.yml
.github/workflows/internal-issue-pr-status.yml
.github/workflows/internal-memory-maintenance.yml
.github/workflows/internal-orchestrate-clarify-respond.yml
.github/workflows/internal-orchestrate-poll.yml
.github/workflows/internal-orchestrate.yml
.github/workflows/internal-plan.yml
.github/workflows/internal-review.yml
.github/workflows/internal-validate.yml
.github/workflows/internal-workflow-failure-heal.yml
.github/workflows/issue_pr_status.yml
.github/workflows/lint-plan-archival.yml
.github/workflows/lint-pr-body-auto-close.yml
.github/workflows/mark-stable.yml
.github/workflows/memory_maintenance.yml
.github/workflows/nightly-validation-selftest.yml
.github/workflows/opencode-live-smoke.yml
.github/workflows/orchestrate.yml
.github/workflows/orchestrate_clarify_respond.yml
.github/workflows/orchestrate_poll.yml
.github/workflows/plan.yml
.github/workflows/pr6576-shard-followup.yml
.github/workflows/promote-main-to-stable.yml
.github/workflows/propagate-consumer-secrets.yml
.github/workflows/publish-sandbox-images.yml
.github/workflows/review_autofix.yml
.github/workflows/review_autofix_sweep.yml
.github/workflows/review_rb_judge_dispatch.yml
.github/workflows/security-audit.yml
.github/workflows/sync-claude-live-copies.yml
.github/workflows/sync_ai_labels.yml
.github/workflows/test-and-mark-stable.yml
.github/workflows/unblock_judge.yml
.github/workflows/unblock_judge_dispatch.yml
.github/workflows/update_workflows.yml
.github/workflows/validate.yml
.github/workflows/validation-improvements-intake.yml
.github/workflows/validation-refresh.yml
.github/workflows/workflow-failure-heal-intake.yml
.github/workflows/workflow-log-analysis.yml
.github/workflows/workflow_failure_heal.yml
.github/workflows/workspace-cache-maintenance.yml
```
<!-- TREE:END id=workflows -->

Consumer-facing workflow templates (regenerate with `make generate`):

<!-- TREE:START id=workflow_templates -->
```
workflow-templates/ai-cancel-on-pr-close.yml
workflow-templates/ai-check-failure-triage.yml
workflow-templates/ai-clarify.yml
workflow-templates/ai-implement.yml
workflow-templates/ai-issue-pr-status.yml
workflow-templates/ai-memory-maintenance.yml
workflow-templates/ai-orchestrate-clarify-respond.yml
workflow-templates/ai-orchestrate-poll.yml
workflow-templates/ai-orchestrate.yml
workflow-templates/ai-plan.yml
workflow-templates/ai-review.yml
workflow-templates/ai-security-audit.yml
workflow-templates/ai-sync-labels.yml
workflow-templates/ai-update-workflows.yml
workflow-templates/ai-validate.yml
workflow-templates/ai-workflow-failure-heal.yml
workflow-templates/review_rb_judge_dispatch.yml
workflow-templates/unblock_judge_dispatch.yml
```
<!-- TREE:END id=workflow_templates -->

## Task-state files (`.tasks/<wave>/<issue>.json`)

- Feature flag: `ORCH_TASK_FILES_ENABLED` (default `false`). When disabled, `scripts/task_state.py` is a no-op and the poller writes only the authoritative chunked GitHub-comment state.
- Schema: each mirrored file is a single issue payload plus `schema_version: "task_state.v1.json"`.
- Layout: wave directory from `waves[].wave`, filename from the stable local wave issue `id` (for example `.tasks/1/issue-1.json`). Existing fields such as `github_issue`, `depends_on`, and `reissue_depends_on` are preserved verbatim inside the mirrored JSON.
- Write path: `scripts/orchestrate_poll_process.sh::post_state_comment()` mirrors every successful authoritative checkpoint into `.tasks/` via atomic tmp-write + rename.
- Unblock path: `scripts/task_state.py::unblock_dependents()` rewrites only the mirrored files, removing a completed issue from `depends_on[]` / `reissue_depends_on[]` and logging `TASK_STATE_UNBLOCK <wave> <completed> <count_unblocked>`.
- Authority: `.tasks/` is mirror-only in Phase C. The chunked `ORCHESTRATOR_STATE_V2` / `STATE_FILE` path remains the sole read source until a future cut-over plan lands.
- Fail-open logging: mirror write and unblock write failures log `TASK_STATE_WRITE_FAIL <issue> <reason>` and do not stop the poll loop.

## Worktree registry (`.worktrees/index.json`)

- Feature flag: `ORCH_WORKTREE_REGISTRY_ENABLED` (default `false`). When disabled, `scripts/worktree_registry.sh` is bypassed and the existing bare `git worktree` lifecycle remains authoritative.
- Schema: the registry root is `{ "schema_version": "worktree_registry.v1.json", "entries": [...] }`.
- Entry shape: each entry records `name`, `path`, `branch`, `task_id`, `created_at`, `owner_phase`, and `owner_run_id`.
- Write path: `scripts/orchestrate_poll_process.sh` registers worktrees only after `git worktree add` succeeds and deregisters them before the matching remove/fallback cleanup path runs.
- GC path: the existing `internal-orchestrate-poll.yml` `*/5` cadence reaches `scripts/worktree_gc.sh` through the reusable `.github/workflows/orchestrate_poll.yml` job, so stale registry/worktree cleanup rides the poller's existing schedule instead of adding a new cron surface.
- Active-run safety: GC first reuses `${RUNTIME_DIR}/state_snapshot_actions_runs.json`, then the cached `scripts/ai_memory.py actions-runs-cache get --repo <owner/repo>` payload, and treats `queued` plus `in_progress` owner runs as live.
- Fail-open logging: invalid names emit `WORKTREE_REGISTER_INVALID_NAME`; registry rebuilds emit `WORKTREE_REGISTRY_REBUILD`; register/deregister I/O failures emit `WORKTREE_REGISTER_FAIL` / `WORKTREE_DEREGISTER_FAIL`; successful lifecycle events emit `WORKTREE_REGISTER`, `WORKTREE_DEREGISTER`, and `WORKTREE_GC`.

## Phase wrapper predicate parity

- Internal `internal-{clarify,plan,implement,orchestrate-clarify-respond}.yml` callers and their `workflow-templates/ai-*.yml` consumer counterparts mirror the complete job-level `if` predicate from the reusable workflow they invoke. Reusable predicates are canonical; `tests/test_phase_wrapper_predicate_contract.py` prevents actor, association, command-marker, issue-kind, and tracker-exclusion drift.

## Conflict-safe PR-close cleanup

- `.github/workflows/internal-cancel-on-pr-close.yml` and `workflow-templates/ai-cancel-on-pr-close.yml` retain the immediate `pull_request.closed` path and also run every five minutes. The schedule covers conflicted PRs for which GitHub suppresses the close event without introducing `pull_request_target` trust.
- Scheduled mode in `.github/workflows/cancel_on_pr_close.yml` snapshots queued and in-progress `pull_request` runs, resolves every linked PR through aliased GraphQL batches of at most 50, and cancels a run only when every association is known and terminal (`CLOSED` or `MERGED`). Missing, malformed, open, or partial state preserves the run. Event mode remains branch- and PR-scoped.
- Cleanup jobs share repository-scoped concurrency. Scheduled ticks run the merge train's existing global release scan; event runs retain the closed PR's base filter.
- The release gate's Phase 7 (`test-and-mark-stable.yml`, step `Phase 7: Close PR and verify cancel_on_pr_close fires`) makes the smoke PR mergeable before closing it. GitHub does not fire `pull_request.closed` for a conflicted PR, and the scheduled sweep's observed cadence (median about 12 minutes) is longer than `PHASE7_WAIT_BUDGET_MINUTES`, so run 35672590166 failed `no_run` after the forward-merge of `stable` rewrote the same `.ai/.workspace_source_manifest.txt` hunk as the smoke PR 75 seconds before the close. The step merges the base into `ai/issue-N` through the merges API; on a 409 it overwrites each PR file the base also changed since the merge base with the base's version (contents API, `[E2E Smoke Test]` commit prefix), retries once, and waits for mergeability to be recomputed. It never fails the gate on its own: the outcome is logged as `PHASE7_UNCONFLICT_CHECK` / `PHASE7_UNCONFLICT_FILE` / `PHASE7_UNCONFLICT_RESULT`, exported as the step output `unconflict`, and shown in the results table. `tests/test_test_and_mark_stable_phase7_unconflict.py` pins the contract.
