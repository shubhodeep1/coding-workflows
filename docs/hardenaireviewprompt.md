# Harden the AI review/autofix pipeline: finalize on timeout instead of dying

## What happened (real incident, use as the reproduction target)

Consumer repo `shubhodeep1/binance-blessings`, PR #239 ("FUN withdrawals: source
priority, BitMart wind-down…"). The `AI Review` wrapper
(`.github/workflows/ai-review.yml`) calls
`shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable`.

- Workflow run `30610297369`, job `91091384818` (`review / codex-agent`).
- Started `2026-07-31T06:39:30Z`, terminated `2026-07-31T10:39:59Z` — exactly
  4h00m, i.e. the job's own timeout, reported to GitHub as `cancelled`.
- Every other check on the PR passed: `review / gate` ✅,
  `copilot-pull-request-reviewer` ✅, the two other `review /` jobs `skipped`.
- Net result of those 4 hours: **no review comment, no findings, no autofix
  commit, and a check that reads as failed on the PR page.** All work performed
  during the run was discarded.

Relevant log lines:

```
10:17:15  Reviewer slot moonshotai/kimi-k2.5 (moonshotai/kimi-k2.5)
          failure classified as retryable (rate_limit) on attempt 2.
10:17:47  CODEX_HEARTBEAT: phase=review_run_reviewers elapsed_secs=30
   …      (heartbeats only — no model events for 20 minutes)
10:27:17  codex_stall_observed pid=20195 mode=review_run_reviewers
          idle_secs=600 last_event_kind=stderr enabled=false
   …
10:37:33  CODEX_HEARTBEAT: phase=review_run_reviewers elapsed_secs=30   ← counter
                                                       resets: new attempt began
10:39:59  job cancelled (4h wall); orphan `codex` process terminated
```

Observed configuration in that run: `REVIEWER_MODELS: minimax/minimax-m2.5`
(with a `moonshotai/kimi-k2.5` reviewer slot in play), `REVIEW_TIER: disabled`,
`XPOLL_SUMMARISER_CALL_TIMEOUT_SECS: 2400`, `CHECK_RUNS_WAIT_TIMEOUT_SECS: 300`,
`REVIEW_CONSOLIDATOR_TIMEOUT_SECS: 300`, `JUDGE_INTERIM_TIMEOUT_S: 120`,
`BEHAVIOURAL_SMOKE_TIMEOUT_S: 120`. Note there are per-call timeouts but no
**overall** budget that forces the run to land its work before the job cap.

Contributing factors, in order of importance:

1. The reviewer slot hit a provider **rate limit**, was classified `retryable`,
   and was retried — each attempt re-sending the full prompt (the PR diff was
   ~1,115 insertions across 6 files, so each attempt is expensive).
2. After the retry the agent produced **no events for 20+ minutes**. The stall
   watchdog detected it (`codex_stall_observed … idle_secs=600`) but its
   recovery action is **disabled** (`enabled=false`), so nothing killed or
   advanced the hung attempt.
3. There is **no soft deadline**: work continues until the hard job timeout,
   at which point everything in flight is lost.

## What to change

Make an AI review/autofix run **incapable of ending as a hard failure due to
time**. When the budget runs out, land whatever was produced and finish green,
then let the normal `synchronize` re-trigger continue the work on the next
round.

### Requirements

1. **Soft deadline below the job cap.** Add an overall run budget (e.g.
   `REVIEW_SOFT_DEADLINE_MINUTES`, defaulted comfortably under the job
   `timeout-minutes`, which stays as the backstop). Once it elapses, the run
   starts **no new** reviewer/editor work and transitions straight to finalize.
   Every phase must check the remaining budget before starting, and the budget
   must be visible in the heartbeat line.

2. **Finalize instead of dying.** On soft-deadline (and on a caught fatal in a
   single phase), the run must:
   - commit and push any autofix edits already made to the PR branch;
   - post the findings gathered so far as a PR review/comment, explicitly
     labelled partial, stating which phases/files/reviewer slots completed and
     which did not;
   - persist whatever state a later run needs to resume.

3. **Green on partial completion.** A run that landed a partial result must end
   `success` (or `neutral`), never `failure`/`cancelled`. Reserve non-success
   for genuine faults: bad config, auth failure, push rejected, unparsable
   inputs. The PR page must not show a red/failed check just because the budget
   was exhausted.

4. **Resume on the next round.** Persist partial state keyed by PR number +
   head SHA (artifact, PR-attached state comment, or the existing workspace
   cache — whichever fits the current design) so the next run continues from
   where the last one stopped instead of restarting from scratch. The push in
   requirement 2 fires `pull_request: synchronize`, which already re-triggers
   the workflow — that is the intended continuation path; do not add a
   self-dispatch.

5. **Loop safety (mandatory).** The partial-apply → re-trigger cycle must be
   provably bounded:
   - bound the number of resume rounds per PR (e.g.
     `REVIEW_MAX_RESUME_ROUNDS`, default 3), tracked in a sticky comment,
     label, or commit trailer;
   - stop when a round makes **no progress** (no new findings, no new edits);
   - never re-trigger when the head SHA is unchanged;
   - when the round budget is exhausted, post a final summary explaining what
     remains unreviewed and stop — still green/neutral, never a failing check.

6. **Enable stall recovery.** The watchdog already detects idleness but is
   switched off (`enabled=false`). Turn it on by default with a per-attempt
   idle cap (well under the soft deadline) and have it **kill the hung attempt
   and advance** — next retry, next reviewer slot, or finalize — rather than
   leaving it to the job cap. Log the kill decision.

7. **Bound rate-limit retries and make them budget-aware.** Cap attempts per
   reviewer slot, use exponential backoff with jitter, fall through to the next
   model in `REVIEWER_MODELS` after N consecutive failures for a slot, and
   never let retries consume more than a configured share of the soft deadline.
   Reuse the prompt cache (`scripts/openrouter_prompt_cache.py`) rather than
   re-sending an identical large prompt where the provider supports it. A
   `rate_limit` classification should count against a slot-level budget, not
   retry indefinitely.

8. **Partial-work safety.** Only push edits that satisfy whatever validation
   the pipeline already runs (lint/tests/behavioural smoke). Never push a
   half-applied or unvalidated edit: if edits cannot be validated within the
   remaining budget, push nothing and post findings only, and say so in the
   partial comment. A partial round must never leave the branch in a worse
   state than it found it.

9. **Observability.** Emit one structured summary line/section at the end of
   every run: phases completed vs skipped, per-slot attempt counts and failure
   classifications, elapsed vs budget, why the run finalized (completed /
   soft-deadline / fatal), whether edits were pushed, and the resume-round
   counter. This is what makes the next incident diagnosable without reading
   4 hours of heartbeats.

10. **Backward compatibility.** New behaviour must be default-on but every new
    knob needs a default; do not rename or repurpose existing inputs, env vars,
    job names, or check-run names — consumer repos pin
    `review_autofix.yml@stable` and their wrappers must keep working untouched.
    If the wrapper interface does change, update the consumer templates in the
    same change.

### Acceptance criteria

Reproduce the incident conditions — force a reviewer slot to return
`rate_limit` and force an attempt to hang past the idle cap — and demonstrate:

- the run ends **not** `cancelled`/`failure`, well before the job `timeout-minutes`;
- partial findings are posted to the PR, clearly marked partial, listing
  covered vs uncovered scope;
- any completed autofix edits are pushed (or explicitly skipped with a stated
  reason), and the push re-triggers the workflow;
- the hung attempt is killed by the watchdog rather than consuming the budget;
- resume rounds are bounded and a no-progress round terminates the cycle;
- the end-of-run structured summary contains everything in requirement 9.

Add automated tests/CI coverage for the deadline, finalize, resume-bound, and
no-progress paths — not just a manual demo.

### Deliverables

- Implementation in `review_autofix.yml` and the supporting scripts
  (`scripts/codex_heartbeat.sh` and the reviewer-slot/retry logic).
- Docs: README/workflow docs covering the new env vars, defaults, the
  partial-completion contract, and the resume/round-bounding semantics.
- Tests as above.
- Follow that repo's own `CLAUDE.md` / `unattended_system_instructions.md`
  conventions, and its ask-first rules where a design decision is genuinely
  ambiguous.
