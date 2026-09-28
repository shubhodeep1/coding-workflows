# Claude-fixer PRs converge unattended

## Summary

Make every `claude/*` pull request reach merge without a human unless Claude is
genuinely stuck: stop handing clean reviews to Claude while checks are still
running, replace the unusable verdict bot with an independent GPT judge that
runs inside the review workflow (and fixes what it upholds), and remove the
remaining session-side reasons for human involvement (checkers that ask,
fixers that hold without reasoning, hourly polling of held PRs, and sessions
that are never archived).

## Automation & Wiring (§18.E)

- **New script vs extension:** extends existing scripts and workflows. New
  scripts are step bodies and helpers invoked by existing automation only:
  `scripts/review_claude_fixer_evidence.py` [new, helper called by workflow
  steps], `scripts/review_autofix_step_claude_fixer_judge.sh` [new, externalised
  step body], `.claude/scripts/stale_sessions.py` [new, run by the hourly Claude
  issue pickup session]. Nothing requires manual invocation.
- **Scheduler entry points:** the review workflow
  (`.github/workflows/review_autofix.yml`, called by `internal-review.yml` on
  `pull_request` here and by the synced `workflow-templates/ai-review.yml`
  wrapper in consumers, and by `workflow_dispatch`); the review sweep
  (`.github/workflows/review_autofix_sweep.yml`, job `sweep`, cron
  `*/30 * * * *`) which already re-dispatches every open PR; the §26 status
  checkers (hourly `send_later`); the hourly Claude issue pickup session
  (`.claude/commands/claude-issue-pickup.md`, Routine `Claude issue pickup:
  hourly`).
- **Long-running supervisor:** none new. The review sweep and the Claude issue
  pickup are the existing supervisors.
- **DB operations:** none. No MongoDB collections, indexes, or contracts (§10
  not applicable).
- **Future-removal registry (§18.F):** no single-use or long-running scripts;
  no `docs/scripts-pending-removal.md` entry.

## Decisions

Answered with the operator in the planning session (2026-09-27). The
implementing chain must not re-open them.

### D1 — Checks pending is not a Claude hand-off

- **Chosen:** a clean ledger whose fresh same-head check snapshot is not ready
  never posts `kind=findings`. After the existing refresh wait, the workflow
  posts a non-hand-off "checks pending" note; the 30-minute review sweep's
  dispatch takes a merge-check-only path (no reviewers) that merges on green,
  hands off `ci` on failure, and exits quietly while checks run.
- **Alternatives considered:** keep handing off and have Claude re-dispatch;
  add a check-completion trigger; re-run the full reviewer panel.
- **Why:** no session wake, no reviewer cost, ≤ 30 min lag, no new trigger.

### D2 — Independent GPT judge replaces the verdict bot

- **Chosen:** when Claude judges every finding invalid it posts
  finding-by-finding reasons and dispatches the review workflow, which runs
  the review-blocked judge in a Claude mode on `WORKFLOW_EDITOR_MODEL`
  (follows the variable; default `openai/gpt-6-sol`). Agree → auto-merge
  bound to the unchanged head once checks are fresh and green, no reviewer
  re-run. Uphold some → the judge fixes them itself (`[judge-fix]` commit),
  the push starts a normal review round, findings go to Claude, a Claude
  rejection returns to the judge. Guards: judge-fix cap 2 per PR, never reset;
  sticky rulings (D4); forced final decision at the cap: merge with a
  follow-up issue for open upheld findings, unless one is security or
  data-loss → hold for a human. `close_and_reissue` is disabled for `claude/*`
  PRs and becomes a hold. The verdict-bot path stays unchanged (§6) but is no
  longer required.
- **Alternatives considered:** a GitHub App verdict bot (a cloud session
  cannot post as it: the agent proxy replaces the `Authorization` header,
  CLAUDE.md §23.A); upheld findings handed back to Claude instead of the judge
  fixing them; a human at the cap every time; allowing `close_and_reissue`.
- **Why:** an independent model in Actions stops Claude self-attesting,
  works in cloud sessions, and the judge fixing its own upheld findings avoids
  a Claude↔judge ping-pong; the cap and sticky rulings make it terminate.

### D3 — Trust only what a workflow run wrote

- **Chosen:** the judge, the checks-pending merge check and sticky rulings
  read the ledger and prior rulings from workflow-run artifacts, after
  verifying the run through the API; comment markers only point at run ids.
  Claude's rejection text is untrusted argument.
- **Alternatives considered:** trusting comment markers by the workflow
  account.
- **Why:** the workflow, the Claude sessions and the second operator account
  all comment as the same GitHub login, so any marker is forgeable.

### D4 — Sticky-ruling match rule

- **Chosen:** same file and within ±3 lines of a finding the judge ruled
  invalid on this PR (the rule #4596 uses).
- **Alternatives considered:** also require the reviewer category to match.
- **Why:** consistent with #4596; stops re-raised noise from looping.

### D5 — Kill switches default on

- **Chosen:** `CLAUDE_FIXER_JUDGE_ENABLED=true`,
  `CLAUDE_FIXER_CHECKS_PENDING_ENABLED=true`; `false` restores today's
  behaviour.
- **Alternatives considered:** default off.
- **Why:** the goal is unattended operation from the moment each phase merges.

### D6 — Held PRs polled every 3 hours

- **Chosen:** `check_in_status.py` returns `retry_after_minutes` (180 for
  `held` via `CLAUDE_CHECK_IN_HELD_RETRY_MINUTES`, 60 otherwise) and both
  checker kinds use it.
- **Alternatives considered:** 6 or 12 hours; unchanged hourly.
- **Why:** cuts idle Sonnet wakes while a human-bound hold is still noticed
  the same day.

### D7 — Session janitor retention

- **Chosen:** archive an automation session 24 hours after its PR/issue is
  merged or closed when no enabled Routine is bound to it, including a session
  waiting on a question; archive a fixer superseded by a newer fixer for the
  same open PR at once. Run by the hourly Claude issue pickup.
- **Alternatives considered:** 72 hours; 7 days; never archive sessions
  waiting on a question.
- **Why:** a question about a terminal PR is moot; 24 hours leaves time to
  read the report.

### D8 — Bootstrap of this project's own PRs

- **Chosen:** if one of this project's PRs ends on today's no-verdict-bot
  hold, the supervising session reviews the rejections and asks the operator
  for a one-line merge approval.
- **Alternatives considered:** leave it held until the operator looks.
- **Why:** the judge cannot review the PR that introduces it.

### D9 — Four phases

- **Chosen:** (1) checks pending, (2) GPT judge, (3) checker/fixer rules and
  held backoff, (4) session janitor.
- **Alternatives considered:** merging phases 3 and 4.
- **Why:** each is small, independently reversible, and safe alone.

### D10 — The catch-all keeps covering every `claude/*` PR

- **Chosen:** the §26.H sweep keeps acting on all `claude/*` PRs, including
  those of the second operator account that acts as the same GitHub login.
- **Alternatives considered:** exclude them by a label or branch rule.
- **Why:** it is a safety net that never acts while a live claim exists.

### D11 — A BLOCKED stop on a held PR still arms the checker

- **Chosen:** a `/implement-plan-claude` stage that stops at
  `Status: BLOCKED` because one of its PRs is held still hands that PR's wait
  to the project checker (polled at the held backoff, D6), so the chain
  resumes on its own once the PR merges or the hold lifts. Added to Phase 3
  on 2026-09-27.
- **Alternatives considered:** a separate PR against
  `implement-plan-claude.md` (conflicts with Phase 3's edits to the same
  file); no change, relying on the judge and the supervising session.
- **Why:** observed on this project: after the operator merged the held
  phase 1 PR #4651, nothing started phase 2, because the checker hands every
  wait to the stage it starts and the BLOCKED stage armed none. In plan mode
  no `/reclarify` exists to resume it, so the chain waited until the
  supervising session started phase 2 by hand. Holds remain possible after the
  judge ships (security or data-loss at the cap, conflict decisions).

### D12 — Report sessions stuck on a permission prompt

- **Chosen:** the Phase 4 janitor also lists sessions blocked on a
  permission prompt for more than 20 minutes; the hourly pickup sends one
  push notification per stall and files it as an `ai:permission-prompt`
  issue, and `/fix-claude-pr` runs the prompt report before it ends. Added
  to Phase 4 on 2026-09-28.
- **Alternatives considered:** a separate PR now; relying on the
  supervising session's check-ins.
- **Why:** #4597's prompt report runs only at the end of an
  `/implement-plan-claude` stage, so a session stuck on a prompt never
  reports it, and fixer sessions never report at all. On 2026-09-27 three
  sessions stalled this way (the #4619 implement session for hours, PR
  #4601's fixer, and this project's phase 2 for about 50 minutes) until a
  human happened to look.

### D13 — Issue-mode projects start from the default branch

- **Chosen:** an issue-mode project uses the default branch as its base
  unless the code it fixes exists only on the branch the issue names.
  `ai:security` follow-ups keep the project branch they name, because the
  audited code lives there. Another named base (a heal issue's PR head,
  `stable`) is used only when a file the plan changes, as opposed to
  creates, is missing on the default branch. When a project still runs on a
  non-default base and that base's pull request closes without merging, the
  chain rebuilds the project on the default branch by itself when the
  project's diff applies cleanly, and records an auto-decision (§28.D). It
  stops at `Status: BLOCKED` only when the diff does not apply. Added to
  Phase 3 on 2026-09-28.
- **Alternatives considered:** always the default branch, with no
  exception; keep today's named-base rule and ask each time.
- **Why:** #4653's project was built on `ai/issue-4605` (AD-1), the head of
  PR #4607, because the heal issue named it. PR #4607 was then closed without
  merging, so validation could never be authorised and the recommended "wait"
  answer would have waited forever. Retargeting the final PR to `main` would
  have carried 203 unrelated commits. The project's 8-file diff applied
  cleanly to `main`, which the operator chose on 2026-09-28.

### D14 — The GPT resolver takes conflicts in protected `.claude/**` paths

- **Chosen:** on a `claude/*` head in Claude-fixer mode, a merge conflict
  whose unmerged paths include any `.claude/**` path goes to the existing
  GPT conflict resolver (`review_conflict_prepare.sh` and the codex-agent
  resolver steps) instead of a Claude hand-off. The resolver resolves every
  unmerged path of that conflict. Its push starts a normal Claude-fixer
  review round, so the reviewer panel and the Claude session still see the
  result. A conflict that touches no `.claude/**` path is handed to Claude
  as today. If the resolver fails or escalates (`RESOLVER_ESCALATED`), the
  workflow posts today's `kind=conflict` hand-off. A kill switch,
  `CLAUDE_FIXER_PROTECTED_CONFLICT_RESOLVER_ENABLED` (default `true`, D5),
  restores today's behaviour when set to `false`. Added to Phase 3 on
  2026-09-28.
- **Alternatives considered:** a deterministic keep-both-sides merge only
  for pure-addition hunks (JSON allow lists), with every other `.claude/**`
  conflict going to the operator; keep today's behaviour and send each one
  to the operator.
- **Why:** Claude Code never auto-approves an edit under `.claude/**`, so an
  unattended fixer cannot resolve such a conflict. PR #4610 stopped on
  2026-09-28 because auto mode blocked its merge of `main` into
  `.claude/settings.json` and `.claude/commands/claude-issue-pickup.md`, and
  the operator had to resolve it in a watched session. `settings.json` is a
  hotspot, since most PRs add allow entries. The resolver runs in Actions,
  where no auto-mode block applies, and it already resolves conflicts for
  every non-`claude/*` PR.

### D15 — A hold comment states its real reason

- **Chosen:** `claude_fix_claim.py post --kind hold` takes a `--reason`, and
  the hold comment states that reason. The "reached the cap of N Claude
  hand-backs" wording is used only when the caller passes `--reason cap`.
  Without `--reason`, the comment keeps today's text, so older callers and
  consumer copies behave as before. The claim marker line does not change
  (§6). Added to Phase 3 on 2026-09-28.
- **Alternatives considered:** leave it, relying on Phase 3's
  reasoning-before-hold comment.
- **Why:** every hold says "reached the cap of 3" whatever its cause
  (`claude_fix_claim.py:67`). On PR #4610 on 2026-09-28 that sent the
  supervising session after a conflict-cap diagnosis that the claim history
  did not support: the fixer had actually stopped on its own conflict
  questions.

### D16 — Plan-mode stops and fixer holds post their question on the PR

- **Chosen:** every place a plan-mode `/implement-plan-claude` stage stops
  at `Status: BLOCKED`, and every `/fix-claude-pr` hold, posts its §2
  question as one PR comment. For a stage the comment goes on the project's
  integration PR, or on the phase PR when the stop is about that PR. For a
  fixer it goes on the held PR. The comment:
  - starts with `<!-- ai:claude-blocked:v1 -->`, the marker issue mode
    already uses on the source issue;
  - holds the blocker, the evidence, every option and the recommended one;
  - is posted with `mcp__github__add_issue_comment`.

  The question is still asked in the session as today. The comment makes it
  visible without opening the session. Answering is unchanged: the
  operator replies in the session, or through the supervising session.
  Added to Phase 3 on 2026-09-28.
- **Alternatives considered:** keep the questions in the sessions and have
  the supervising session ask each one to post.
- **Why:** issue mode already posts its stops on the source issue, but plan
  mode and fixers ask only inside their own sessions. On 2026-09-28 neither
  the master poller nor the supervising session could see PR #4610's fixer
  questions or phase 2's `.claude` stop until each session was asked to post
  them.

## Context

Observed on 2026-09-27 in `shubhodeep1/coding-workflows`: 11 open `claude/*`
PRs were on hold, each with a Claude session blocked on a human answer and
most with an hourly Sonnet checker polling it.

- **Clean review handed to Claude (4 PRs: #4582, #4554, #4599, #4601).**
  `scripts/review_autofix_step_claude_fixer_handoff.sh` (lines ~112–133)
  refreshes checks after a clean ledger via
  `scripts/collect_pr_check_runs_context.py` (waits up to
  `CHECK_RUNS_WAIT_TIMEOUT_SECS`, default 300). When checks are still
  running it logs "Claude-fixer clean review has no fresh ready same-head
  check-run snapshot; auto-merge disabled." and falls through to a
  `kind=findings` hand-off with 0 entries (run 36295340728, PR #4554:
  `CLAUDE_FIXER_HANDOFF … kind=findings findings=0 ledger=ok
  failed_checks=none`). The fixer has nothing to fix, no verdict bot, and
  holds. The supervising session unblocked all four by re-dispatching
  review with the `force-review` label.
- **Every finding rejected, no verdict bot (6 PRs: #4602, #4594, #4609, #4611,
  #4638, #4596).** `.claude/commands/fix-claude-pr.md` step 5 and
  `.claude/commands/implement-plan-claude.md` step 7a require a dedicated
  GitHub App (`CLAUDE_FIXER_VERDICT_BOT_LOGIN`) to post the verdict. The
  variable is empty, and a Claude Code Web session cannot post as a bot at all:
  the agent proxy replaces the `Authorization` header on every `api.github.com`
  call (CLAUDE.md §23.A). Most rejected findings were raised by one reviewer
  model only; the rejections cited code and tests.
- **Hold without reasoning (#4610, #4611).** The fixer went straight to hold;
  on #4610 a 4-of-6 consensus finding (a misleading comment) was left
  unaddressed.
- **Mislabelled holds.** `.claude/scripts/claude_fix_claim.py` prints "reached
  the cap of 3" for every hold. Open PR #4596 (project #4586) fixes this with
  `--reason` and makes rejected single-reviewer findings non-blocking.
- **A checker asked the human** (PR #4609's `PR #4609 status check-in` session:
  "decide whether to proceed or defer"). §26.C says the checker acts on nothing.
- **Sessions pile up.** 40 non-archived sessions on 2026-09-27; completed and
  superseded fixer/report sessions never archive ("Never archive yourself: the
  report is what the user opens", `fix-claude-pr.md:60`) and nothing archives
  them later.
- **`gh workflow run` fails in Claude Code Web** (`unable to determine default
  branch … GraphQL is not available`), so the dispatch instructions in the
  command docs do not work in cloud sessions. The REST form works:
  `gh api -X POST repos/<o>/<r>/actions/workflows/review_autofix.yml/dispatches
  -f ref=<default> -f 'inputs[pr_number]=<N>'`, which `.claude/hooks/
  gh_api_write_guard.py` classifies as routine for an allow-listed workflow.

Existing machinery reused:

- The review-blocked judge (`scripts/review_rb_judge.sh`, prompt
  `prompts/mode-judge-review-blocked.txt`, step `Review-blocked judge
  decision`, `id: rb_judge`, `review_autofix.yml` ~5774). Actions `merge`,
  `fix`, `merge_with_followup`, `close_and_reissue`; `fix` runs an OpenCode
  writer on `MODEL_EDITOR` and the script commits `[judge-fix] …` and pushes.
  Today every Claude-fixer PR skips it (`env.CLAUDE_FIXER_MODE != 'true'`).
- The `force_rb_judge` dispatch shape (gate `FORCE_RB_JUDGE`, retrigger guard
  forcing `max_iterations_reached=true`), and the `claude_fixer_converged_head`
  gate checks (head binding, hand-off/ledger binding, comment ordering;
  `review_autofix.yml` ~753–810), which the new judge dispatch mirrors.
- Merge binding: `scripts/review_enable_auto_merge.sh` (`gh pr merge --squash
  --auto --match-head-commit`), unlocked for Claude-fixer PRs by
  `CLAUDE_FIXER_ZERO_FINDINGS=true`.
- `retrigger_guard` counts: `autofix_count` walks consecutive
  `^\[(ai|claude)-autofix\]` subjects (a `[judge-fix]` stops the walk, which is
  the "reset"); `judge_fix_count` counts `[judge-fix]` subjects in the whole
  `git log`.

## Goals

- A clean review whose checks are still running never produces a Claude
  hand-off; the PR merges within one sweep interval (≤ 30 min) of its checks
  going green, or gets a `ci` hand-off if they fail.
- A Claude rejection of every finding is decided by the GPT judge in the
  workflow, with no human, and the PR either merges, gets a `[judge-fix]`
  commit, or (security/data-loss at the cap) holds with `ai:needs-human`.
- No Claude-fixer PR can loop forever: at most 2 `[judge-fix]` commits per PR
  (counted on the PR's own commits, never reset), then a forced final
  decision.
- No merge or dismissal is ever based on a comment marker alone: ledger,
  clean-review and ruling evidence come from verified workflow-run artifacts.
- §26 checkers never ask the human; fixers always post finding-by-finding
  reasoning before any hold; held heads are polled every 180 minutes.
- Finished and superseded automation sessions are archived by the hourly
  pickup within about 25 hours, without ever archiving a running session, the
  pickup itself, a session with an enabled Routine, or a session waiting on a
  decision for an open PR/issue.
- Each behaviour has a kill switch that restores today's behaviour.

## Non-goals

- Removing or renaming the verdict-bot path, `CLAUDE_FIXER_VERDICT_BOT_LOGIN`,
  or the `claude_fixer_converged_head` input (§6: kept as-is).
- Changing the reviewer panel, reviewer models, `MAX_AUTOFIX_ITERATIONS`, or
  the review-blocked judge's behaviour for non-`claude/*` PRs.
- Re-implementing what open PR #4596 ships (single-reviewer non-blocking
  findings, `claude_fix_claim.py --reason`) or #4609 (deterministic skip for
  small/doc-only `claude/*` PRs). This plan builds on them when merged and
  coexists when not (see Constraints).
- Fixing the `agents.md` (~832–835) statement that `.claude/commands/` is not
  synced, which contradicts `update_workflows.yml`'s `claude_sync`; separate
  follow-up.
- The deprecated "Claude issue dispatcher" Routine (operator deletes it in the
  claude.ai UI; agents cannot delete an `http_api` Routine).

## Constraints

- **§6 naming immutability.** No identifier is renamed or removed. New
  identifiers (all checked unique on 2026-09-27): repo vars
  `CLAUDE_FIXER_JUDGE_ENABLED`, `CLAUDE_FIXER_CHECKS_PENDING_ENABLED`,
  `CLAUDE_FIXER_JUDGE_FIX_CAP`, `CLAUDE_CHECK_IN_HELD_RETRY_MINUTES`; dispatch
  input `claude_fixer_judge_head`; artifact name prefix
  `claude-fixer-evidence-`; comment markers `ai:claude-fixer-checks-pending:v1`,
  `ai:claude-fixer-rejection:v1`, `ai:claude-fixer-judge:v1`; JSON field
  `retry_after_minutes`; script `stale_sessions.py`. Log keys use the existing
  `CLAUDE_FIXER_HANDOFF` prefix plus new `CLAUDE_FIXER_JUDGE` and
  `CLAUDE_FIXER_CHECKS_PENDING` prefixes; add them to agents.md "Stable log
  prefixes (contractual)".
- **§4 env defaults.** Every new variable has a default (above) and is
  documented in the README variable table.
- **§27 workflow size.** `review_autofix.yml` is 451,394 bytes. New logic goes
  into `scripts/` using the step-script pattern (agents.md "Workflow file size
  limit": `review_autofix_step_<slug>.sh`, `REQUIRED_BOOTSTRAP_SCRIPTS` in
  `scripts/stage_workflow_support.sh`, `REVIEW_AUTOFIX_STEP_SCRIPTS` in
  `tests/review_autofix_step_scripts.py`, `docs/INVENTORY.md`). Each phase
  checks `wc -c` and must leave the file < 470,000 bytes; if a phase would
  cross that, it externalises the largest inline `run:` body it touches.
- **§15 API hygiene.** Budgets are stated per step below; every new read
  reuses `gate_fetch_marker_comments` / existing comment fetches where they
  exist and caches per run.
- **§14 / consumer propagation.** `review_autofix.yml` changes reach consumers
  on the next `@stable`; `.claude/` files reach them through `claude_sync`
  (`update_workflows.yml`), CLAUDE.md through `claude_md_sync`. Every edited
  `.claude/` file that has a `workflow-templates/.claude/` twin is edited in both
  (tests assert parity). `claude-issue-pickup.md` and `stale_sessions.py` are
  coding-workflows-only (no template copy).
- **§20 changelog.** One `changelog.d/` fragment per phase PR.
- **§19.** PR bodies use `Refs #N`, never closing keywords, for any
  `ai:orchestrator-tracking` issue.
- **Coexistence with #4596 / #4609.** #4596 restructures the clean-ledger
  block of `review_autofix_step_claude_fixer_handoff.sh` and adds a
  `NON-BLOCKING FINDINGS` ledger block plus `claude_fix_claim.py --reason`.
  Before editing those files each phase checks whether #4596's changes are on
  the base branch: if so, extend its block and use `--reason` (value
  `review-no-verdict-bot` stays valid but is no longer the normal path; add no
  new reason values); if not, add the minimal equivalent and leave a code
  comment naming #4596. Never duplicate its filter. #4609 only changes the
  deterministic-skip gate condition; this plan does not touch that condition.
- **Security (§1 first).** Claude must never self-attest a merge. All new
  merge paths go through `review_enable_auto_merge.sh` with
  `--match-head-commit`, only after the evidence checks in D3.

## Approach

### Evidence artifact (shared by Phases 1 and 2)

A new always-run step after `Hand review round to Claude session (Claude-fixer
mode)` uploads `claude-fixer-evidence-<run_id>-<run_attempt>` (retention 30
days) containing `reviewer_consensus.txt` (the ledger file the hand-off step
read) and `evidence.json` written by the hand-off script:
`{schema: 1, pr, head_sha, round, outcome: "clean"|"checks-pending"|
"findings"|"conflict", ledger_sha256, finding_count, failed_checks[],
judge: null | {decision, rulings: [{file, line, claim, ruling:
"invalid"|"upheld"}]}}`. The upload is a `uses: actions/upload-artifact@v6`
step (it must stay in YAML), guarded by `env.CLAUDE_FIXER_MODE == 'true'`.
Whichever of Phase 1 / Phase 2 lands first adds it; the other reuses it.

`scripts/review_claude_fixer_evidence.py` [new] loads evidence for a PR:
given run ids referenced by workflow marker comments, it calls
`GET /repos/{o}/{r}/actions/runs/{id}` and accepts the run only when all hold:
(a) `repository` is this repo; (b) the run's workflow is a trusted review
workflow — the run's `path` is `.github/workflows/review_autofix.yml`
dispatched from the default branch (`head_branch` = default), or its
`referenced_workflows` contains `review_autofix.yml` at the trusted ref this
repo already uses for review (here `refs/heads/<default>`; in consumers the
`@stable`/pinned ref the wrapper uses — reuse the existing trusted-ref helper
from the "Review self-repo support staging runs under main's workflow YAML" /
"Immutable consumer wrapper pins" machinery in agents.md; if no helper exists,
accept `refs/heads/<default>` and `refs/tags/stable` only); (c) for a
`pull_request` run, the PR's changed files (existing PR-files fetch, reused)
do not include the calling workflow file; (d) `status == completed`. It then
lists the run's artifacts (`GET …/runs/{id}/artifacts`), downloads the one
named `claude-fixer-evidence-<id>-*`, and checks `evidence.json.pr` and
`head_sha` against the PR. Anything else → "unverified" (callers fail closed to
today's behaviour). Budget: 3 REST calls per verified run, cached per run.
Related: #4618 (review dispatches unmerged workflow) hardens the same trust
boundary; if it has merged, use its helper.

### Phase 1: checks pending

In the hand-off script, when the ledger is clean, no check failed, but the
refreshed snapshot is not ready (the existing warning branch), and
`CLAUDE_FIXER_CHECKS_PENDING_ENABLED != false`: write `outcome:
"checks-pending"` to `evidence.json`, post (or update in place) one comment
`## Review round <r>: clean, waiting for checks` ending
`<!-- ai:claude-fixer-checks-pending:v1 head=<sha> round=<r> run=<run_id> -->`,
log `CLAUDE_FIXER_CHECKS_PENDING pr=… head=… action=wait`, and exit 0 without a
hand-off. `check_in_status.py` ignores this marker (it only reacts to the
hand-off family, claims and blocking labels), so checkers keep re-arming.

Gate (`review_autofix.yml`, externalised to a step script if > ~40 lines): on a
`workflow_dispatch` for a Claude-fixer PR whose latest workflow marker for the
current head is `checks-pending` (via `gate_fetch_marker_comments`), set
`CLAUDE_FIXER_MERGE_CHECK=true`, skip reviewers (force
`max_iterations_reached`-style short-circuit like `FORCE_RB_JUDGE`, but run no
judge), and run a new step "Claude-fixer merge check" that verifies the
evidence run (outcome `checks-pending`, same head) via
`review_claude_fixer_evidence.py`, then re-collects checks with
`collect_pr_check_runs_context.py` (`CHECK_RUNS_WAIT_TIMEOUT_SECS=0`, no wait):
ready and green → `CLAUDE_FIXER_ZERO_FINDINGS=true` (existing auto-merge steps
fire, bound to the head); any failed → post the existing `kind=findings`
hand-off listing the failing checks (Claude fixes as `ci`); still running →
log `CLAUDE_FIXER_CHECKS_PENDING … action=still_waiting` and exit. The sweep's
30-minute dispatch provides the retry; the terminal same-head skip and the
awaiting-session skip must not swallow it (checks-pending is not a hand-off, so
awaiting-session already does not apply; add the bypass to the terminal skip).

### Phase 2: GPT judge

Claude side (`fix-claude-pr.md` step 5 "None valid", `implement-plan-claude.md`
step 7a "no valid finding"): post one comment listing every finding with
`rejected: <reason citing file:line or test>`, ending
`<!-- ai:claude-fixer-rejection:v1 head=<sha> round=<r> -->`; then dispatch
over REST: `gh api -X POST repos/<o>/<r>/actions/workflows/<review
workflow>/dispatches -f ref=<default branch> -f 'inputs[pr_number]=<N>' -f
'inputs[claude_fixer_judge_head]=<sha>'` (`<review workflow>` is
`review_autofix.yml` here; consumers dispatch their `ai-review.yml` wrapper),
report, arm/keep the §26 check-in, and end the turn — no hold. If the dispatch
fails (HTTP 422 unknown input, i.e. an older wrapper, or the kill switch is
reported off in the run), fall back to today's hold with reason
`review-no-verdict-bot`. The verdict-bot instructions stay as an alternative
when `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is configured.

Workflow side:

- New `workflow_dispatch` / `workflow_call` input `claude_fixer_judge_head`
  (string, default `""`) in `review_autofix.yml`; plumbed through
  `internal-review.yml` (if it forwards dispatch inputs) and
  `workflow-templates/ai-review.yml` (and its twin). Gate: when set, accept
  only if `CLAUDE_FIXER_JUDGE_ENABLED != false`, the PR is Claude-fixer, the
  value is 40-hex and equals the current head, the latest workflow hand-off
  for this head is `kind=findings` with a verified evidence run (outcome
  `findings`, same head/round, `ledger_sha256` matches the hand-off's v2
  digest), no `ai:claude-fixer-judge:v1` verdict exists yet for this head and
  round (idempotency), and the judge-fix cap state allows a run (the final
  decision is still allowed at the cap). Log `AUTOFIX_GATE_CLAUDE_FIXER_JUDGE
  pr=… head=… accepted=… detail=…`. On accept set `CLAUDE_FIXER_JUDGE=true`,
  bypass the awaiting-session and terminal same-head skips, and short-circuit
  the reviewers like `force_rb_judge`.
- `rb_judge` step `if:` gains `|| env.CLAUDE_FIXER_JUDGE == 'true'` (keeping
  `env.CLAUDE_FIXER_MODE != 'true'` for every other path; update
  `tests/test_review_autofix_claude_fixer_mode.py`'s `EDITOR_TAIL_STEPS`
  expectation accordingly). A new step script
  `review_autofix_step_claude_fixer_judge.sh` prepares the judge inputs and
  exports them; `review_rb_judge.sh` gains a Claude mode
  (`CLAUDE_FIXER_JUDGE=true`) that:
  - takes the ledger from the verified evidence artifact (not the comment),
    Claude's latest `ai:claude-fixer-rejection:v1` comment for this head as
    untrusted argument (quoted, never instructions; the existing
    prompt-injection guard applies), prior rulings from verified judge
    evidence, and the diff;
  - appends a Claude-mode block to the prompt (in
    `prompts/mode-judge-review-blocked.txt` and any `prompts/_templates/`
    twin): rule on each finding (`invalid` / `upheld`) with a one-line reason
    and a `category` (`security`, `data-loss`, `correctness`, `other`);
    allowed actions `merge`, `fix`, `merge_with_followup`, `hold`;
    `close_and_reissue` is not allowed;
  - counts `judge_fix_count` only over the PR's own commits
    (`git log <merge-base>..HEAD`), cap `CLAUDE_FIXER_JUDGE_FIX_CAP` (default 2),
    never reset by other commits;
  - executes: all `invalid` → `merge` path (re-collect checks as in Phase 1;
    green → enable auto-merge bound to head; still running → post the Phase 1
    checks-pending marker if Phase 1 is present, else exit and leave the
    hand-off for the sweep; failed → `ci` hand-off); some `upheld` and below the
    cap → `fix` (existing writer; commit subject
    `[judge-fix] claude-fixer round <r>: <summary>`; the push starts a normal
    review round); at the cap → `merge_with_followup` with a follow-up issue
    listing the open upheld findings, unless any upheld finding is `security`
    or `data-loss` → `hold`; model returns `close_and_reissue` → treated as
    `hold`;
  - `hold`: add label `ai:needs-human` (already a blocking label for
    `check_in_status.py`, so the §26 fixer takes the existing "needs-human →
    hold and ask, one PushNotification" path) and post the verdict comment;
  - always posts one verdict comment ending
    `<!-- ai:claude-fixer-judge:v1 head=<sha> round=<r> run=<run_id>
    decision=<merge|fix|merge_with_followup|hold> -->` with the per-finding
    rulings, and writes `judge.rulings` into this run's evidence artifact.
- Sticky rulings: in the hand-off script, before counting findings, load
  rulings from verified judge evidence runs referenced by
  `ai:claude-fixer-judge:v1` comments on this PR (latest 3 runs at most) and
  move every finding that matches an `invalid` ruling (same file, line within
  ±3) into the non-blocking block (#4596's `NON-BLOCKING FINDINGS` if present,
  else a new `=== NON-BLOCKING FINDINGS ===` block not counted as a finding),
  tagged `sticky judge ruling run=<id>`. A round with only such findings is
  clean.

### Phase 3: session rules

- CLAUDE.md §26.C preamble and §26.B step 3's required prompt contents: "The
  checker never asks the human anything and never waits on an answer: on an
  unexpected state it re-arms per step 2 and records the state in its session
  title only." Same line in `implement-plan-claude.md`'s `### Checker prompt`
  block.
- `check_in_status.py` adds `retry_after_minutes` to its JSON (60; 180 for
  `held`, from `CLAUDE_CHECK_IN_HELD_RETRY_MINUTES`, clamp 60..1440). §26.C
  step 2 and the implement-plan checker prompt use `delay_minutes =
  retry_after_minutes` (default 60 when the field is absent, so an old script
  still works).
- `fix-claude-pr.md` and `implement-plan-claude.md`: before posting any hold
  claim, post the finding-by-finding reasoning comment (valid/fixed or
  rejected with reason), including for a hold that is not about findings (state
  the blocker and the evidence). A hold with no reasoning comment on the head
  is a rule violation.
- `implement-plan-claude.md` (D11): a stage that stops at `Status: BLOCKED`
  while one of the project's PRs is held (a hold of any reason: all findings
  rejected, a security or data-loss hold at the judge cap, a conflict
  decision, a cap-reached hold) still **arms the project checker's wait on
  that PR** before it ends, exactly as step 7 arms a normal wait: next stage on
  merge = the stage that follows that PR's merge, next stage on a review
  hand-off = the review round, next stage on a block = the blocked-PR stage.
  The BLOCKED ask stays (a human may still merge by hand); only the automatic
  resume is added. The checker treats `held` as not done and re-arms at
  `retry_after_minutes`. Once the PR merges (by a human or the judge), or the
  hold lifts and a hand-off appears, the checker starts that next stage.
  In issue mode a `/reclarify` can start the same stage: a stage session whose
  stage the progress log already records as started by another live session
  (`Stage:` plus the stage session id the checker records) reports that and
  ends without acting, so two resumes never both run.
- `implement-issue-claude.md` and `implement-plan-claude.md` issue mode
  (D13):
  - Step 3's base rule: resolve `<issue base>` as today, then, when it is not
    the default branch and the issue is not an `ai:security` follow-up, use
    the default branch unless a file the plan changes (not one it creates)
    is missing there (`git cat-file -e origin/<default>:<path>`). The check
    runs once the plan's file list exists and before the project branch is
    created. Record the choice as an `AD-<n>` (`Base: <default> instead of
    <named> (D13)`, or the reverse with the missing paths).
  - Every issue-mode stage start (step 0), and every project check-in while
    the log's base is not the default branch, spends one REST read
    (`GET pulls?head=<owner>:<base>&state=all&per_page=5`) and treats the
    base as dead when no listed PR is open and the newest is closed with
    `merged_at` null, or when the base branch is gone (`git ls-remote`
    empty).
  - On a dead base, the checker starts the next stage with
    `Waiting on was: base <base> closed unmerged`. That stage rebuilds the
    project:
    1. Create `<project branch>-main` from `origin/<default>` (append `-2`,
       `-3`, … when taken).
    2. Apply `git diff origin/<base>...origin/<project branch>` after a clean
       `git apply --3way --check`.
    3. Update the progress log's project-branch and base lines, commit the
       uncommitted log entries, and push.
    4. Close the old final PR without merging, as superseded. The chain
       opened it, so this is not a §23.C ask.
    5. Open the final PR from the new branch into the default branch.
    6. Re-run the last conformance audit's checks on the new base, then
       resume at the stage it was on.
  - The old project branch is never deleted, rewritten, or force-pushed.
    The rebuild is an `AD-<n>`. When `--check` fails, the stage stops at
    `Status: BLOCKED` with the conflicting paths, as a failure escalation
    (§28.C).

- `review_autofix.yml` and `scripts/review_autofix_step_claude_fixer_handoff.sh`
  (D14):
  - After "Detect merge conflicts" sets `MERGE_CONFLICT=true` on a Claude-fixer
    head, a small step lists the unmerged paths once (the same
    `git diff --name-only --diff-filter=U` the resolver and the hand-off
    already use). It exports `CLAUDE_FIXER_PROTECTED_CONFLICT=true` when any
    path starts with `.claude/` and the kill switch is not `false`.
  - The resolver steps' `env.CLAUDE_FIXER_MODE != 'true'` guard becomes
    `(env.CLAUDE_FIXER_MODE != 'true' || env.CLAUDE_FIXER_PROTECTED_CONFLICT == 'true')`.
    This covers the resolve step, its push and re-trigger tail, and the
    `CONFLICT_RESOLVED` follow-ups. The editor, the review-blocked judge and
    auto-merge keep their Claude-fixer guards.
  - The hand-off script skips its `kind=conflict` comment while
    `CLAUDE_FIXER_PROTECTED_CONFLICT=true`, unless the resolver failed or
    escalated. In that case it posts today's hand-off, adding one line that
    names the protected paths so the Claude session stops at once.
  - Log key `AUTOFIX_GATE_CLAUDE_FIXER_PROTECTED_CONFLICT pr=<n> head=<sha>
    paths=<k> resolver=<ran|skipped_switch_off>` (stable prefix, agents.md).
  - New variable `CLAUDE_FIXER_PROTECTED_CONFLICT_RESOLVER_ENABLED`, default
    `true` (README variables table).
  - The workflow stays under the plan's per-phase byte cap. The new step
    body is an externalised `review_autofix_step_<slug>.sh` (§27 pattern),
    and each touched `if:` gains one clause.
- `claude_fix_claim.py` (+ twin) and the command docs (D15, D16):
  - `post` gains `--reason <text>`, allowed only with `--kind hold`. It is
    one line, at most 300 characters, with backticks and `<!--` removed. The
    hold comment reads "**Claude fixes on hold:** <reason> at head `<sha>`.
    `<by>` has asked a human how to continue…", and `--reason cap` gives
    today's cap sentence. No `--reason` keeps today's text.
  - `fix-claude-pr.md` passes `--reason cap` at its cap stop (step 3). Every
    other hold passes a one-line reason ("all findings rejected; no verdict
    path", "conflict needs a side decision", "failure not caused by this
    PR", "a human was requested").
  - `/implement-plan-claude` does the same for the holds it posts.
  - Before any hold, `fix-claude-pr.md` posts the `ai:claude-blocked:v1` §2
    question comment on the PR. It can be the same comment as Phase 3's
    reasoning-before-hold comment, carrying both the reasoning and the
    question.
  - `implement-plan-claude.md` plan mode posts the same comment on the
    integration PR, or the phase PR, at every `Status: BLOCKED` stop,
    before it arms the checker's wait (D11). Issue mode is unchanged: it
    already posts on the issue.

### Phase 4: session janitor

`.claude/scripts/stale_sessions.py` (modelled on `stale_routines.py`):
`--sessions FILE` (one or more `list_sessions` dumps, `{ccr:{data:[…]}}` or a
bare array), `--triggers FILE` (a `list_triggers` dump), `--grace-hours 24`.
Output one JSON line `{"archive":[{"id","title","reason"}], "kept": n,
"not_ours": n, "errors": [...]}`. Only titles matching the automation
patterns are considered (`PR <repo>#<N> — …`, `PR #<N> status check-in…`,
`PR #<N> <merged|closed> — …`, `issue <repo>#<N> — implement`,
`implement-plan <slug> — …` except `— deploy-activate`); everything else is
`not_ours` (never archived: the pickup, operator sessions, deploy-activate).
Archive when **all** hold: status not running/working
(`session_status` ≠ `SESSION_STATUS_RUNNING`, `status_bucket` ≠ `…WORKING`),
no enabled Routine has `persistent_session_id` = the session, and either
(a) its PR/issue is merged/closed ≥ grace hours ago — the number comes from
the title; for `implement-plan <slug> — …` titles only when the slug has the
issue-mode form `issue-<N>-…` (the source issue), otherwise the session is kept
because no PR/issue is derivable — or (b) it is a `PR …#<N> —`
fixer session and a newer fixer session for the same repo/PR exists. A
`need_input` session is archived only under (a). One REST read per distinct
PR/issue (cached); a failed read keeps the session (`errors`). The pickup
(`claude-issue-pickup.md`) adds a step after trigger bookkeeping on `— wake.`:
`list_sessions` `mine: true` (pages until `created_at` older than 14 days),
`list_triggers` `include_completed: false`, run the script, `archive_session`
each id (ignore not-found), and append `archived <k>` to its one-line report.

Stuck permission prompts (D12): `stale_sessions.py` also returns a
`stalled_on_prompt` list: every session (any title, including operator
sessions) whose `status_bucket` is `…BLOCKED` and whose
`post_turn_summary.needs_action` starts with `Approve or deny`, with
`updated_at` more than 20 minutes ago (`--prompt-stall-minutes`, default 20).
For each new entry (the pickup keeps the reported `id` + `updated_at` pairs in
its session so a stall is reported once) the pickup sends one
`PushNotification` (`<session title>: waiting on a permission prompt since
<HH:MM> UTC`) and, in coding-workflows, opens or comments on an
`ai:permission-prompt` issue through `.claude/scripts/permission_prompts.py`
with the session title and its `task_summary` as the evidence (the command
itself is not visible from outside the session). `/fix-claude-pr` runs
`permission_prompts.py file` before its report, as `/implement-plan-claude`
stages already do (#4597), so prompts a fixer answered late are filed too.

## Phases & Merge Strategy

1. **Checks-pending, not a hand-off.** Files:
   `scripts/review_autofix_step_claude_fixer_handoff.sh`,
   `scripts/review_claude_fixer_evidence.py` [new],
   `scripts/review_autofix_step_claude_fixer_merge_check.sh` [new],
   `.github/workflows/review_autofix.yml` (evidence upload step, gate
   merge-check branch, merge-check step, terminal-skip bypass),
   `scripts/stage_workflow_support.sh`, `tests/review_autofix_step_scripts.py`,
   `docs/INVENTORY.md`, tests, `README.md` (variables, Claude-fixer section),
   `agents.md` (log prefixes, Claude-fixer notes), `changelog.d/`.
   Done when: a clean ledger with incomplete checks posts the checks-pending
   marker and no hand-off (unit test on the script with a fake context file);
   the gate routes a dispatch on such a head to the merge check and skips
   reviewers (YAML contract test); a merge check with green checks sets
   `CLAUDE_FIXER_ZERO_FINDINGS=true`, with failed checks posts a findings
   hand-off naming them, with running checks exits quietly; evidence from an
   untrusted run is rejected; `CLAUDE_FIXER_CHECKS_PENDING_ENABLED=false`
   restores today's behaviour; workflow < 470,000 bytes.
   Rollback: set `CLAUDE_FIXER_CHECKS_PENDING_ENABLED=false`, or revert the PR.
2. **GPT judge for Claude-fixer PRs.** Files: `.github/workflows/
   review_autofix.yml` (input, gate branch, `rb_judge` `if:`, evidence upload
   if Phase 1 is absent), `.github/workflows/internal-review.yml` (only if it
   forwards dispatch inputs), `workflow-templates/ai-review.yml`,
   `scripts/review_autofix_step_claude_fixer_judge.sh` [new],
   `scripts/review_rb_judge.sh`, `prompts/mode-judge-review-blocked.txt` (+
   template twin), `scripts/review_autofix_step_claude_fixer_handoff.sh`
   (sticky rulings), `scripts/review_claude_fixer_evidence.py` (create if Phase 1
   is absent), `.claude/commands/fix-claude-pr.md`,
   `.claude/commands/implement-plan-claude.md` (+ `workflow-templates/.claude/`
   twins), `CLAUDE.md` §26.H wording, `README.md`
   (`CLAUDE_FIXER_VERDICT_BOT_LOGIN` row: optional; new variables), `agents.md`,
   tests, `changelog.d/`. Every `gh workflow run review_autofix.yml` /
   `ai-review.yml` instruction in `.claude/commands/` is changed to the REST
   dispatch form.
   Done when: gate accepts a valid judge dispatch and rejects stale head,
   unverified evidence, digest mismatch, repeated head+round, disabled switch
   (contract tests); Claude-mode judge unit tests cover agree→merge, uphold→fix
   with correct commit subject, cap reached→merge_with_followup, cap reached
   with a security finding→hold (`ai:needs-human`), `close_and_reissue`→hold,
   `judge_fix_count` counts only `merge-base..HEAD`; sticky rulings demote a
   re-raised finding within ±3 lines and not one at +4 or in another file; the
   fixer docs no longer hold on "none valid" when the dispatch succeeds.
   Rollback: `CLAUDE_FIXER_JUDGE_ENABLED=false` (fixers fall back to the hold
   when the run reports the switch off), or revert the PR.
3. **Checkers never ask; reasoning before holds; held backoff.** Files:
   `CLAUDE.md` §26.B/§26.C/§26.H, `.claude/commands/implement-plan-claude.md`,
   `.claude/commands/fix-claude-pr.md`,
   `.claude/commands/implement-issue-claude.md`,
   `.claude/scripts/check_in_status.py`, `.claude/scripts/claude_fix_claim.py`
   (+ twins), `.github/workflows/review_autofix.yml`,
   `scripts/review_autofix_step_claude_fixer_handoff.sh`,
   `scripts/review_autofix_step_claude_fixer_protected_conflict.sh` [new],
   `scripts/stage_workflow_support.sh`, `tests/review_autofix_step_scripts.py`,
   `docs/INVENTORY.md`, `agents.md` (check-in section, log prefixes),
   `README.md` (new variables), tests (`tests/test_check_in_status_hand_back.py`,
   `tests/test_review_autofix_claude_fixer_mode.py`, template parity),
   `changelog.d/`.
   Done when: `check_in_status.py --hand-back` emits `retry_after_minutes` 180
   for `held` and 60 otherwise (env override and clamp tested); the docs carry
   the never-ask and reasoning-before-hold rules verbatim in each place;
   `implement-plan-claude.md` (+ twin) makes every BLOCKED stop on a held PR
   arm the project checker's wait on that PR, and says how a stage started by
   both `/reclarify` and the checker ends the duplicate (D11), with the
   command-doc tests asserting both. D13: the command docs carry the base
   rule and the rebuild procedure, and the command-doc tests assert the
   default-branch choice, the `ai:security` exception, the dead-base read,
   and the BLOCKED stop on a failed `--check`. D14: contract tests show a
   Claude-fixer conflict with a `.claude/` path runs the resolver and posts no
   hand-off, one without runs no resolver and posts the hand-off, a failed
   resolver posts the hand-off naming the protected paths, and the switch set
   to `false` restores today's path; the workflow stays under the byte cap.
   D15: `--reason` changes the hold text, `--reason cap` and no `--reason`
   give today's text, `--reason` with another kind is rejected, and the
   marker line is unchanged. D16: the command-doc tests assert the
   `ai:claude-blocked:v1` PR comment at every plan-mode BLOCKED stop and
   every fixer hold.
   Rollback: revert the PR (checkers default to 60 when the field is absent).
4. **Session janitor.** Files: `.claude/scripts/stale_sessions.py` [new],
   `tests/test_stale_sessions.py` [new], `.claude/commands/claude-issue-pickup.md`,
   `.claude/settings.json` (allow `python3 .claude/scripts/stale_sessions.py *`
   and the `PYTHONDONTWRITEBYTECODE=1` form, as for `stale_routines.py`),
   `.github/workflows/ci.yml` (own test step, like `test_stale_routines.py`),
   `agents.md`, `README.md` (pickup section), `changelog.d/`.
   Done when: tests cover every archive/keep rule (running, enabled Routine,
   not-ours title, need_input on open PR kept, need_input on PR merged 25 h ago
   archived, superseded fixer archived, failed read kept), input shapes, exit
   codes; the pickup doc runs it only on `— wake.`; `stalled_on_prompt`
   lists a BLOCKED `Approve or deny` session older than 20 minutes and not a
   younger or non-prompt one, and the pickup reports each stall once (D12);
   `fix-claude-pr.md` (+ twin) runs the prompt report before its report.
   Rollback: revert the PR; archived sessions can be unarchived.

Phases are delivered in this order but each is safe alone: Phase 2 creates the
evidence helper and upload step if Phase 1 has not; Phase 3's checkers
default to 60 minutes without the new field; Phase 4 touches only the pickup.

## Implementation Steps

### Phase 1

1. `scripts/review_claude_fixer_evidence.py` [new]: `write` subcommand (used by
   the hand-off script to write `evidence.json`) and `verify` subcommand
   (`--repo --pr --head --run-id --expect-outcome`, prints JSON
   `{verified, reason, evidence}`); REST only, budget as in Approach; exit 0
   with `verified:false` on any failure.
2. Hand-off script: write `evidence.json` for every outcome; in the
   no-fresh-snapshot branch add the checks-pending path behind the switch
   (post/update one marker comment per head; reuse
   `claude_fixer_post_marker_comment`).
3. `review_autofix.yml`: add the evidence upload step right after the hand-off
   step (`if: always() && env.CLAUDE_FIXER_MODE == 'true'`,
   `continue-on-error: true`, retention 30).
4. Gate: detect checks-pending head on Claude-fixer `workflow_dispatch`
   (reuse `gate_fetch_marker_comments`), export `claude_fixer_merge_check`
   output/env, short-circuit reviewers, bypass the terminal same-head skip for
   it. Put the new logic in a gate helper function in an existing sourced gate
   script if one exists; otherwise inline ≤ 40 lines.
5. `scripts/review_autofix_step_claude_fixer_merge_check.sh` [new] + step
   "Claude-fixer merge check" (`if: env.CLAUDE_FIXER_MERGE_CHECK == 'true'`),
   registered per §27 pattern; auto-merge step conditions accept the zero-
   findings flag it sets (they already do).
6. Tests: extend `tests/test_review_autofix_claude_fixer_mode.py`; new
   `tests/test_review_claude_fixer_evidence.py`.
7. Docs + changelog fragment `changelog.d/<pr>-claude-fixer-checks-pending.md`.

### Phase 2

1. Ensure Phase 1's evidence helper and upload step exist (create them if not).
2. Add `claude_fixer_judge_head` input (dispatch + call), plumb through
   wrappers, gate acceptance as specified, log key.
3. `review_autofix_step_claude_fixer_judge.sh` [new]: resolve latest hand-off,
   verify evidence, fetch the rejection comment and prior judge rulings, export
   `CLAUDE_FIXER_JUDGE_*` inputs for `review_rb_judge.sh`.
4. `review_rb_judge.sh` Claude mode (inputs, prompt block, cap counting over
   `merge-base..HEAD`, action mapping, `hold`, verdict comment, evidence
   rulings); `rb_judge` step `if:`.
5. Prompt block in `prompts/mode-judge-review-blocked.txt` (+ template twin).
6. Hand-off script sticky-ruling demotion.
7. Fixer docs (`fix-claude-pr.md` step 5, `implement-plan-claude.md` step 7a,
   twins), REST dispatch form everywhere, CLAUDE.md §26.H sentence on the
   judge, README/agents.md.
8. Tests (gate contract, judge Claude-mode unit tests with a stubbed model
   output, sticky rulings, docs parity), changelog fragment.

### Phase 3

1. `check_in_status.py` `retry_after_minutes` + env var + clamp (+ twin).
2. CLAUDE.md §26.B step 3 / §26.C preamble and step 2; implement-plan checker
   prompt block; fixer reasoning-before-hold rule in both command docs (+
   twins).
3. `implement-plan-claude.md` (+ twin): the D11 rule. Every place that stops at
   `Status: BLOCKED` with a held PR (step 7 Blocked rule, step 7a no-valid-
   finding branch, the cap stops) first arms the wait per the Check-in Loop's
   "Arming the wait", then stops; add the duplicate-resume guard to step 0.
4. D13: the base rule in `implement-issue-claude.md` step 3 and the dead-base
   check and rebuild in `implement-plan-claude.md` issue mode and its checker
   prompt (+ twins).
5. D14: the protected-conflict step (externalised), the resolver `if:`
   clauses, the hand-off skip and fallback line, the variable and log key.
6. D15: `claude_fix_claim.py --reason` (+ twin) and the callers in both
   command docs (+ twins). D16: the `ai:claude-blocked:v1` PR comment in
   `fix-claude-pr.md` and `implement-plan-claude.md` plan mode (+ twins).
7. Tests + agents.md + README + changelog fragment.

### Phase 4

1. `stale_sessions.py` + tests + CI step.
2. Pickup doc step, Tool Access (`list_sessions`, `list_triggers`,
   `archive_session`, `PushNotification`), settings allowlist, docs, changelog
   fragment.
3. D12: `stalled_on_prompt` in `stale_sessions.py` (+ tests), the pickup's
   once-per-stall notification and `ai:permission-prompt` filing, and the
   `permission_prompts.py file` step in `fix-claude-pr.md` (+ twin).

## Files & Modules

- `.github/workflows/review_autofix.yml`
- `.github/workflows/internal-review.yml` (only if it forwards dispatch inputs)
- `.github/workflows/ci.yml`
- `workflow-templates/ai-review.yml` (+ any twin the parity tests require)
- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `scripts/review_autofix_step_claude_fixer_merge_check.sh` [new]
- `scripts/review_autofix_step_claude_fixer_judge.sh` [new]
- `scripts/review_autofix_step_claude_fixer_protected_conflict.sh` [new]
- `scripts/review_claude_fixer_evidence.py` [new]
- `scripts/review_rb_judge.sh`
- `scripts/stage_workflow_support.sh`
- `prompts/mode-judge-review-blocked.txt` (+ `prompts/_templates/` twin if present)
- `.claude/scripts/check_in_status.py`, `.claude/scripts/claude_fix_claim.py`
  (+ `workflow-templates/.claude/scripts/` twins)
- `.claude/scripts/stale_sessions.py` [new]
- `.claude/commands/fix-claude-pr.md`, `.claude/commands/implement-plan-claude.md`,
  `.claude/commands/implement-issue-claude.md` (+ twins),
  `.claude/commands/claude-issue-pickup.md`
- `.claude/settings.json`
- `CLAUDE.md`, `README.md`, `agents.md`, `docs/INVENTORY.md`
- `tests/test_review_autofix_claude_fixer_mode.py`,
  `tests/review_autofix_step_scripts.py`,
  `tests/test_review_claude_fixer_evidence.py` [new],
  `tests/test_review_rb_judge_claude_mode.py` [new],
  `tests/test_check_in_status_hand_back.py`, `tests/test_stale_sessions.py` [new]
- `changelog.d/` (one fragment per phase)

## Tests

- Unit: evidence verify (trusted run, dispatch from non-default branch
  rejected, wrong PR/head rejected, missing artifact, API failure → unverified);
  hand-off outcomes (clean+ready, clean+pending, findings, sticky demotion
  boundaries); merge check (green/failed/running); judge Claude mode with
  stubbed model JSON for every action and cap boundary; `retry_after_minutes`;
  `stale_sessions.py` rules.
- Contract (YAML via `yaml.safe_load` and `expanded_review_autofix_text()`):
  new input, gate branches and log keys, step order (evidence upload after
  hand-off; merge check and judge short-circuit reviewers), `rb_judge` `if:`,
  size guard.
- Parity: template twins, settings allowlist, INVENTORY.
- End-to-end (operator-observable, after merge): the next Claude-fixer PR with a
  clean review and slow checks merges via the sweep; the next all-rejected
  round shows an `ai:claude-fixer-judge:v1` verdict and merges or gets a
  `[judge-fix]` commit, with no hold; the pickup report shows `archived <k>`.
- Run the full suite locally before each push (`make test` or the repo's
  pytest invocation per README).

## Risks & Mitigations

- **Judge merges something a reviewer was right about.** Mitigation: the judge
  sees the code and every finding; security/data-loss upheld at the cap holds;
  merge is bound to the head; kill switch. ACCEPTED as the price of unattended
  operation (planning decision).
- **Prompt injection through Claude's rejection text.** Mitigation: passed as
  quoted untrusted argument under the existing guard; the judge's rulings are
  about the code, and the ledger comes from verified evidence.
- **Sticky ruling hides a genuinely new bug within ±3 lines.** Mitigation:
  scoped to the same PR and file, max 3 judge runs; the final PR of a project
  still gets a full review. ACCEPTED (planning Q3 A).
- **Forged evidence via a modified caller workflow.** Mitigation: trusted-ref
  and changed-files checks in `review_claude_fixer_evidence.py`; unverified
  evidence fails closed to today's hold. Related #4618.
- **Consumer wrappers lack the new input until synced.** Mitigation: the
  dispatch returns 422 and the fixer falls back to today's hold; the next
  `@stable` sync adds the input.
- **Workflow size.** Mitigation: step scripts; < 470,000-byte check per phase.
- **Conflicts with #4596 / #4609.** Mitigation: per-phase check of the base
  branch, build on #4596 when present, never touch #4609's condition.
- **`list_sessions` output size in the pickup.** Mitigation: the harness saves
  large results to a file which is passed to the script as is; the pickup's
  report stays one line.
- **The GPT resolver edits protected `.claude/**` files unattended (D14).**
  Those files steer every Claude session: settings allow lists, hooks,
  commands. Mitigation: the resolver only merges the two sides of an
  existing conflict, within its unmerged-path allowlist. Its push is
  reviewed by the panel and the Claude session like any head. `.claude/**`
  changes reach consumers only through the `@stable` release. The switch
  turns it off. ACCEPTED (operator Q12 B, 2026-09-28).
- **A rebuild on the default branch validates a change against different
  code (D13).** Mitigation: it happens only after a clean `git apply
  --3way --check`, the conformance checks re-run on the new base, and the
  final PR gets a full review. The old branch is kept.
- **Bootstrap: this project's PRs may hit today's hold.** ACCEPTED (planning
  Q8 A): the supervising session reviews and asks the operator for a one-line
  merge approval.

## Rollout

Each phase is live on merge in this repo (default-on switches, D5) and reaches
consumers on the next `@stable` release plus the daily `update_workflows.yml`
sync. Rollback per phase: set the phase's switch to `false`
(`CLAUDE_FIXER_CHECKS_PENDING_ENABLED`, `CLAUDE_FIXER_JUDGE_ENABLED`) or revert
its PR; Phase 3 and 4 revert cleanly. After Phase 2 merges, the six PRs held on
"no verdict bot" (#4602, #4594, #4609, #4611, #4638, #4596) are re-driven by
their fixers through the judge (the supervising session re-dispatches them).

## References

- Held PRs: #4582, #4554, #4599, #4601, #4602, #4594, #4609, #4611, #4638,
  #4596, #4610 (protected-path conflict D14, misleading hold text D15,
  questions only in-session D16); projects #4586, #4618, #4653
  (dead base, D13).
- Run 36295340728 (PR #4554 zero-finding hand-off).
- Overlapping PRs: #4596 / #4593 (issue #4586), #4609; related #4618.
- CLAUDE.md §4, §6, §14, §15, §18, §19, §20, §23.A, §25, §26, §27, §28.
- agents.md "Workflow file size limit", "Interactive post-push PR status
  check-in", "Stable log prefixes (contractual)".
- README.md variables `CLAUDE_FIXER_VERDICT_BOT_LOGIN`, `CHECK_RUNS_*`,
  `MAX_REVIEW_BLOCKED_RETRIES`, `WORKFLOW_EDITOR_MODEL`.
