# Review autofix: terminalize deterministic editor failures and make partial-finalize resume advance

## Summary

Stop `review_autofix.yml` from re-running multi-hour reviewer cycles into an
editor that dies before its first model call, and make the "resume round N
of 3" counter actually advance across runs. Two independently mergeable
phases: P1 classifies a fast, pre-attempt editor exit as deterministic and
routes it to `ai:review-blocked`; P2 recovers the resume round from the
partial-finalize PR comment the workflow already posts and gives the
ledger/resume cache a run-independent path.

## §18 Automation surface (plan output requirements, CLAUDE.md §18.E)

- **Scripts:** P1 modifies existing code only (`scripts/review_apply_fixes.sh`
  and `.github/workflows/review_autofix.yml`). P2 adds one workflow-invoked
  helper, `scripts/review_resume_from_comments.py` [new], and modifies
  `.github/workflows/review_autofix.yml`, `.github/workflows/validate.yml`,
  and `scripts/stage_workflow_support.sh`. No standalone manual scripts
  (§18.A): the helper is executed only by the "Restore same-head partial
  resume state" step.
- **Scheduler / trigger entry point:** the existing review triggers —
  `.github/workflows/internal-review.yml` (this repo) and the consumer
  wrapper `workflow-templates/ai-review.yml`, both calling the reusable
  `review_autofix.yml` (`pull_request` and `workflow_dispatch`). The stall
  poller's failed-autofix re-dispatch
  (`scripts/orchestrate_poll_process.sh:11615-11659`) is the retry path this
  plan stops feeding. No wrapper edits.
- **Supervisor:** none. The review job stays a per-PR one-shot run.
- **DB operations:** none. No MongoDB collections, indexes, or contracts
  are touched (§10 not applicable).
- **§18.F registry:** one entry for `scripts/review_resume_from_comments.py`
  (type `permanent — review annually`) added in the P2 PR; P1 adds no
  entry.

## Context

PR #4057 (`ai/issue-4056`, the security-pass fix for project #3965) exposed
two pipeline weaknesses on the `main` tip (`0192df4`, 2026-09-09). All line
references below are to that tip.

**The editor died before its first model call, five times, and every time
the workflow called it transient.** Runs 34257678201, 34266425657,
34283641666, 34290972060 and 34297628750 each spent 1–2.5 h in "Run reviewer
models", then `scripts/review_apply_fixes.sh` exited 1 within three seconds
(`line 26: PR_CLOSED_SENTINEL_FILE: PR_CLOSED_SENTINEL_FILE must be set`).
The "Post editor summary comment" step (`review_autofix.yml:4078-4112`)
sees an empty `EDITOR_SUMMARY_FILE` and empty `COMMITTED_FILES_FILE`, posts
"**AI review/autofix produced no output — will retry**", exports
`AUTOFIX_EDITOR_EMPTY_NOOP=true` and exits 1. That flag is exactly what
suppresses the label at "Mark linked issues review-blocked (workflow
failure)" (`review_autofix.yml:6704-6705`, gated on
`env.AUTOFIX_EDITOR_EMPTY_NOOP != 'true'`), so the linked issue never turns
`ai:review-blocked`, the stall poller sees a failed last run and
re-dispatches (`orchestrate_poll_process.sh:11615-11659`), and the cycle
repeats every tick. The editor step itself runs
`bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh"` under `set -euo
pipefail` (`review_autofix.yml:3829,3870`), so neither the exit code nor
the elapsed time of the script is recorded anywhere a later step can read.

**After the first fix landed, the editor failed inside its attempts and the
"resumable" partial finalize never resumed.** Runs 34304993091 and
34320556598 (head `09edc3e`) reached the editor, lost all three attempts to
`opencode_helpers.sh: Permission denied`, and posted a
`<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->` comment with `resume_round=1`,
`resume_state=resumable`. Both runs then wrote the resume marker and ledger
into the cache ("Save review-issue ledger after partial finalize",
`review_autofix.yml:6324-6333`), but the next run restored nothing:

- `WORKSPACE_REUSE_ENABLED` defaults to `false`
  (`review_autofix.yml:1772`), so `scripts/workspace_init.sh:203-208` names
  the workspace `<workspace_root>/<pr>-<run_id>-<attempt>`. Every cache
  `path:` (`review_autofix.yml:2080-2083`, `4072-4075`, `6329-6332`) embeds
  that run-specific directory, so a saved archive can only ever extract
  into the run that saved it. Run 34320556598 logged
  `Cache not found for input keys: review-ledger-…-pr-4057-34320556598-1,
  review-ledger-…-pr-4057-` and, in "Restore same-head partial resume
  state", `No cached same-head partial resume state matched
  HEAD=09edc3e…`; its ledger stage reported `iteration=1
  ledger_prior_entries=0`.
- `pull_request` runs and `workflow_dispatch` runs live in different
  GitHub Actions cache scopes (`refs/pull/N/merge` vs the branch), so even a
  correctly-pathed cache is invisible to the alternate event type. The
  stall poller re-dispatches with `workflow_dispatch`; the pushes trigger
  `pull_request`.
- When the resume evaluation does reach a terminal state
  (`review_autofix.yml:6105-6125`: `no_progress` or
  `round_budget_exhausted`), it only sets `AUTOFIX_RESUME_TERMINAL=true`.
  No step is conditioned on that value being `'true'`; it only suppresses
  the post-editor steps. The PR is left with a success-coloured run and no
  label.

`validate.yml:625-633` restores the same `review-ledger-*` cache (for the
behavioural-smoke runtime) into *its* run-specific workspace path, so it
has the same extract-into-the-wrong-directory problem and must move with
P2.

Related: `docs/postmortems/2026-05-18-project-2734-stall.md` (a different
stall class; same lesson that "will retry" must be bounded).

## Decisions

### D1 — Deterministic signal: attempt marker plus an elapsed-time bound

- **Chosen:** `review_apply_fixes.sh` touches an attempt-started marker at
  the moment it launches its first model process; the summary step treats
  "no summary, no commits, no marker" as deterministic **only when** the
  editor script also exited non-zero in under
  `AUTOFIX_EDITOR_DETERMINISTIC_FAIL_SECS` (default `120`).
- **Alternatives considered:** elapsed time alone (owned entirely by the
  YAML; misclassifies a genuinely fast transient crash); reserved exit
  codes alone (misses `:?` guards and other plain `exit 1` paths).
- **Why:** user answer `Q4: A`. The marker is the precise signal; the time
  bound protects source-repo PRs whose staged script (scripts come from
  the PR SHA on this repo) predates the marker, and consumer repos on an
  older `@stable`.

### D2 — Deterministic outcome: distinct comment, no empty-noop flag, existing review-blocked path

- **Chosen:** post "editor precondition failure — not retrying" with the
  script's exit code, elapsed seconds and the tail of its stderr; do **not**
  export `AUTOFIX_EDITOR_EMPTY_NOOP`; exit 1 so the existing "Mark linked
  issues review-blocked (workflow failure)" and "Post review-blocked comment
  on PR (workflow failure)" steps fire unchanged.
- **Alternatives considered:** label only after two consecutive
  deterministic failures on the same head; comment without label.
- **Why:** user answer `Q5: A`. Reuses the label path that already exists
  for every other hard failure; the review-blocked judge is the designed
  owner of "a human or a re-plan is needed".

### D3 — Resume round source: the partial-finalize PR comment

- **Chosen:** when the cache-based marker scan finds nothing for the
  current head, parse the `<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->` comments
  already present in `PR_ISSUE_COMMENTS_FILE` (fetched once per run by
  `scripts/review_collect_pr_metadata.sh:213-214`), select the highest
  `resume_round` whose `head_sha` matches HEAD, and export the same
  `AUTOFIX_RESUME_*` values the marker path exports.
- **Alternatives considered:** cache only with a stable path (still resets
  whenever the event type alternates); both.
- **Why:** user answer `Q6: A`. Zero new API calls (§15), survives the
  cache-scope split, and the writer and reader are the same workflow file.

### D4 — Stable cache path via a per-PR staging directory under `RUNNER_TEMP`

- **Chosen:** copy the three cached paths into
  `${RUNNER_TEMP}/review-state/pr-<PR_NUMBER>/` before every save and back
  into the workspace after every restore; point every `path:` at that
  directory; leave keys and restore-keys unchanged.
- **Alternatives considered:** default `WORKSPACE_REUSE_ENABLED` to `true`
  (whole-workspace persistence, much larger blast radius); skip and rely on
  D3 alone (reviewer artifacts regenerated every round).
- **Why:** user answer `Q7: A`.

### D5 — Round-limit terminal state routes to review-blocked

- **Chosen:** when the partial-finalize evaluation ends terminal
  (`round_budget_exhausted` or `no_progress`) with `incomplete_scope`
  containing `editor`, a new step applies `ai:review-blocked` to the
  linked issues and posts the review-blocked comment, mirroring the
  workflow-failure step's body.
- **Alternatives considered:** leave terminal behaviour as is.
- **Why:** user answer `Q8: A`. Today terminal means "silently stop".

### D6 — Rollout: on by default with a repo-var off switch

- **Chosen:** `AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED` defaults to
  `true` (P1). By the same reasoning P2's comment-sourced resume is gated by
  `REVIEW_RESUME_FROM_COMMENTS_ENABLED`, default `true`; both are the
  documented rollback lever.
- **Alternatives considered:** default `false`, enable per repo after
  observation.
- **Why:** user answer `Q9: A` for P1; the P2 flag follows the same
  posture so each phase can be switched off without a revert.

### D7 — Two phases

- **Chosen:** P1 = D1 + D2 + D5 + P1 flag; P2 = D3 + D4 + P2 flag.
- **Alternatives considered:** single phase.
- **Why:** user answer `Q10: A`. Neither phase references identifiers the
  other introduces; each is shippable and revertible alone.

### D8 — Delivery: plan PR on the `/write-plan` branch, implementation by the orchestrator

- **Chosen:** this document ships on `claude/write-plan-…` as its own PR;
  implementation is handed to the unattended pipeline via
  `/implement-plan-ai`.
- **Alternatives considered:** plan and implementation on the session
  branch in one PR; plan here, implementation in a separate session PR.
- **Why:** user answer `Q11: B`.

## Goals

- A `review_apply_fixes.sh` exit before its first model launch produces,
  in the same run, a PR comment naming the failing line and the label
  `ai:review-blocked` on every linked issue; the stall poller does not
  re-dispatch the review for that head.
- A `review_apply_fixes.sh` exit *after* at least one model launch keeps
  today's behaviour exactly (partial finalize or "will retry").
- Two consecutive partial-finalize runs on the same head, regardless of
  event type, show `resume_round=1` then `resume_round=2`; the third ends
  `round_budget_exhausted` and the linked issues are labelled
  `ai:review-blocked`.
- After P2, "Restore review-issue ledger" reports a cache hit on the second
  run of a PR and the ledger stage reports `ledger_prior_entries>0`.
- Every new behaviour is behind a repo var that restores today's behaviour
  when set to `false`.

## Non-goals

- Fixing PR #4057's own editor isolation (`sudo -u nobody` path access);
  that is handled on `ai/issue-4056` directly.
- Changing how support scripts are staged on the source repo
  (`SCRIPT_REF=${{ github.sha }}`); PR #4057 addresses that.
- Enabling workspace reuse (`WORKSPACE_REUSE_ENABLED`) by default.
- Changing the stall poller's re-dispatch logic. It keys off a failed last
  run; once the issue carries `ai:review-blocked` the review-blocked judge
  owns the PR.
- Changing validate.yml's behavioural-smoke logic beyond the cache path.

## Constraints

- **§4** — every new env var has a default (`AUTOFIX_EDITOR_DETERMINISTIC_FAIL_SECS=120`,
  `AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED=true`,
  `REVIEW_RESUME_FROM_COMMENTS_ENABLED=true`, `EDITOR_ATTEMPT_STARTED_FILE`
  defaulting to `${RUNTIME_DIR}/editor_attempt_started`,
  `REVIEW_STATE_CACHE_DIR` defaulting to
  `${RUNNER_TEMP}/review-state/pr-${PR_NUMBER}`).
- **§5** — no reformatting; new steps sit beside the existing ones; the
  empty-noop branch, its wording, and `AUTOFIX_EDITOR_EMPTY_NOOP` are
  untouched for the non-deterministic case.
- **§6** — no identifier is renamed or removed. Cache keys, restore-keys,
  comment markers, `AUTOFIX_RESUME_*` names, and the
  `REVIEW_AUTOFIX_PARTIAL_V1` field set are preserved; P2 only appends a
  `progress_fingerprint=` line to that comment's text block. New log
  prefixes `AUTOFIX_EDITOR_DETERMINISTIC_FAILURE` and
  `AUTOFIX_RESUME_SOURCE` are added to the "Stable log prefixes" list in
  `agents.md`.
- **§9** — YAML two-space; new Python helper uses tabs.
- **§14** — `review_autofix.yml`, `validate.yml`, and `scripts/*` reach
  every repo in `.github/ai/consumer_repos.json` on the next `@stable`
  release. No wrapper template changes, so no consumer edits.
- **§15** — P2 reads `PR_ISSUE_COMMENTS_FILE` (already fetched). No new
  `gh` calls in either phase; the label/comment posts reuse the existing
  `gh_retry` + `label_helpers.sh` paths.
- **§19** — no auto-close keywords in any body this plan produces; the
  new comments reference nothing by number.
- **§20** — each phase PR carries one `changelog.d/<pr>-<slug>.md`
  fragment (`<!-- changelog: fixed -->`).
- **Security** — the deterministic comment embeds only the last 20 lines
  of the editor script's stderr, capped at 4 KiB. The script scrubs
  `GH_TOKEN` from its environment before any launch (the existing
  "Scrubbed GH_TOKEN from the review editor environment" notice), and the
  precondition failures this branch targets fire before any model or
  network call, so the tail carries bash diagnostics only. The comment is
  posted through the existing `gh_retry gh api -f body=` path.

## Approach

**P1.** Record what the editor script did, then decide in the summary
step. The editor step wraps the script call to capture exit code, elapsed
seconds, and a stderr tail into `RUNTIME_DIR`, then re-raises the exit
code so today's step failure semantics and the in-step retry gate are
unchanged. The script touches `EDITOR_ATTEMPT_STARTED_FILE` where it emits
`LaunchingAgentProcess`. The summary step adds one branch ahead of the
empty-noop short-circuit: enabled, empty summary, no commits, non-zero
script rc, marker absent, elapsed under the bound → deterministic. A
terminal-resume step turns `round_budget_exhausted` / `no_progress` on the
editor phase into the same label outcome.

**P2.** Read what the workflow already wrote. The same-head restore step
gains a comment-sourced fallback implemented in a small Python helper so
it is unit-testable; the partial-finalize comment gains a
`progress_fingerprint=` line so `no_progress` detection works from the
comment too. The three cache save/restore steps in `review_autofix.yml`
and the one in `validate.yml` point at a per-PR staging directory; two
tiny copy steps bridge staging directory and workspace.

## Phases & Merge Strategy

1. **P1 — deterministic editor-failure classification and terminal routing.**
   Scope: `scripts/review_apply_fixes.sh` marker; editor-step wrapper;
   summary-step branch; terminal-resume label step; flags; log prefixes;
   tests; changelog fragment; README/agents.md updates.
   Done when: a synthetic run with `review_apply_fixes.sh` replaced by
   `exit 1` (the existing smoke override mechanism,
   `tests/test_review_autofix_smoke_editor_override.py`) yields the new
   comment, no `AUTOFIX_EDITOR_EMPTY_NOOP`, and `ai:review-blocked` on the
   linked issue; contract tests pass; a real run where the script exits
   after `LaunchingAgentProcess` still produces today's partial-finalize
   comment.
   Rollback: set repo var `AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED=false`
   (instant, no deploy) or revert the PR; the marker file is inert without
   the YAML branch.
2. **P2 — resume round from PR comments and stable cache path.**
   Scope: `scripts/review_resume_from_comments.py` [new];
   `review_autofix.yml` restore/save steps and comment writer;
   `validate.yml` restore step; `stage_workflow_support.sh` list; tests;
   registry entry; changelog fragment; README/agents.md updates.
   Done when: two consecutive `workflow_dispatch` + `pull_request` runs on
   one head report `AUTOFIX_RESUME_ROUND` 1 then 2 with
   `AUTOFIX_RESUME_SOURCE=comment`, and "Restore review-issue ledger"
   reports a hit on the second run; unit tests for the parser pass.
   Rollback: set `REVIEW_RESUME_FROM_COMMENTS_ENABLED=false` to drop the
   comment source; revert the PR to restore the old cache paths (old
   caches under the old keys remain readable by the reverted workflow).

Neither phase depends on the other: P1 reads only files and env the
current workflow already produces; P2 changes no classification logic.
They can merge in either order.

## Implementation Steps

### P1

1. `scripts/review_apply_fixes.sh` (main tip: `emit_editor_substate`
   definition at 714; `LaunchingAgentProcess` at 2005) — define
   `EDITOR_ATTEMPT_STARTED_FILE="${EDITOR_ATTEMPT_STARTED_FILE:-${RUNTIME_DIR}/editor_attempt_started}"`
   near the other runtime-file defaults; immediately after the
   `LaunchingAgentProcess` emit, write `attempt=<n> epoch=<s>` to that file
   (`printf … >` so the file also records the latest attempt). No other
   script change.
2. `.github/workflows/review_autofix.yml` runtime export block (1713-1760)
   — add `echo "EDITOR_ATTEMPT_STARTED_FILE=${RUNTIME_DIR}/editor_attempt_started"`
   and `echo "EDITOR_SCRIPT_STDERR_TAIL_FILE=${RUNTIME_DIR}/editor_script_stderr_tail.txt"`.
3. `review_autofix.yml` top-level `env:` — add
   `AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED: ${{ vars.AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED || 'true' }}`
   and `AUTOFIX_EDITOR_DETERMINISTIC_FAIL_SECS: ${{ vars.AUTOFIX_EDITOR_DETERMINISTIC_FAIL_SECS || '120' }}`
   next to the other `AUTOFIX_*` vars.
4. `review_autofix.yml` "Apply fixes with editor model" (3822-3956) —
   replace the bare `bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh"`
   at 3870 with a wrapper: record start epoch; run the script with
   `2> >(tee -a "${EDITOR_SCRIPT_STDERR_TAIL_FILE}" >&2)` and `|| editor_script_rc=$?`;
   compute elapsed; append `AUTOFIX_EDITOR_SCRIPT_RC`,
   `AUTOFIX_EDITOR_SCRIPT_ELAPSED_SECS` to `$GITHUB_ENV`; truncate the tail
   file to its last 20 lines; `exit "${editor_script_rc}"` when non-zero.
   The in-step retry block below stays reachable only on rc 0, exactly as
   today (it was unreachable on failure because of `set -e`).
5. `review_autofix.yml` "Post editor summary comment" (4078-4112) —
   before the existing `if [ ! -s "${EDITOR_SUMMARY_FILE}" ] && [ ! -s
   "${COMMITTED_FILES_FILE:-/dev/null}" ]` branch, insert the deterministic
   branch: flag on, both files empty, `AUTOFIX_EDITOR_SCRIPT_RC` set and
   non-zero, `EDITOR_ATTEMPT_STARTED_FILE` absent, elapsed `<` bound. It
   logs `AUTOFIX_EDITOR_DETERMINISTIC_FAILURE pr=… rc=… elapsed_secs=…
   marker=absent`, posts the comment (title
   `**AI review/autofix editor precondition failure — not retrying**`,
   rc, elapsed, run link, fenced stderr tail), exports
   `AUTOFIX_EDITOR_DETERMINISTIC_FAILURE=true`, and `exit 1` **without**
   exporting `AUTOFIX_EDITOR_EMPTY_NOOP`. The existing branch and text
   remain verbatim for every other case.
6. `review_autofix.yml` — new step "Mark linked issues review-blocked
   (resume budget exhausted)" placed immediately after "Post partial
   finalize comment and persist runtime marker" (5909) and before "Save
   review-issue ledger after partial finalize" (6324). Condition:
   `always() && env.AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED == 'true' &&
   env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED == 'true' &&
   env.AUTOFIX_RESUME_TERMINAL == 'true' &&
   env.AUTOFIX_PARTIAL_FINALIZE_PHASE == 'editor' && env.PR_CLOSED != 'true'
   && env.CLAUDE_BRANCH_REVIEW_MODE != 'true'`. Body: the label-apply
   pattern of 6704-6760 (source `label_helpers.sh`, `ensure_label_exists`,
   `set_issue_phase_label_resilient` per linked issue from the cached
   linked-issue references) plus one PR comment
   (`**AI review/autofix resume budget exhausted — marking review-blocked**`
   with `resume_state`, `resume_round`, `resume_round_limit`). Log
   `AUTOFIX_EDITOR_DETERMINISTIC_FAILURE pr=… reason=resume_terminal
   resume_state=…`.
7. "Mark linked issues review-blocked (workflow failure)" (6704) and "Post
   review-blocked comment on PR (workflow failure)" (6799) — no condition
   change; they fire because `AUTOFIX_EDITOR_EMPTY_NOOP` is not set in the
   deterministic case. Add a one-line comment above 6705 pointing at step 5.
8. `agents.md` "Stable log prefixes (contractual)" — add
   `AUTOFIX_EDITOR_DETERMINISTIC_FAILURE`. `README.md` review section —
   document the two new vars, the comment title, and the label outcome.
9. Tests (see below) and `changelog.d/<pr>-deterministic-editor-failure.md`.

### P2

10. `scripts/review_resume_from_comments.py` [new] — `select_resume_from_comments(comments_json, head_sha, round_limit) -> dict | None`:
    filter comments whose body contains `<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->`,
    parse the fenced `text` block as `key=value` lines, keep entries with
    `partial_finalize=true` and `head_sha == head_sha`, return the one with
    the highest `resume_round` (ties: latest `created_at`). CLI: reads
    `PR_ISSUE_COMMENTS_FILE`, `CURRENT_HEAD_SHA`, `RESUME_ROUND_LIMIT`
    from env, prints `KEY=VALUE` lines in the same shape the marker path
    exports (`AUTOFIX_RESUME_RESTORED=true`, `AUTOFIX_RESUME_ROUND`,
    `AUTOFIX_RESUME_STATE`, `AUTOFIX_RESUME_SHOULD_CONTINUE`,
    `AUTOFIX_RESUME_HEAD_SHA`, `AUTOFIX_RESUME_COMPLETED_SCOPE`,
    `AUTOFIX_RESUME_INCOMPLETE_SCOPE`, `AUTOFIX_RESUME_PROGRESS_FINGERPRINT`,
    `AUTOFIX_RESUME_REASON`, `AUTOFIX_RESUME_PHASE`,
    `AUTOFIX_RESUME_COMMENT_POSTED=true`, `AUTOFIX_RESUME_MARKER_FILE=`,
    `AUTOFIX_RESUME_RESTORED_ARTIFACT_COUNT=0`, plus new
    `AUTOFIX_RESUME_SOURCE=comment`). Empty output on no match. Fail-open:
    any parse error prints nothing and exits 0 with a `::warning::`.
11. `review_autofix.yml` "Restore same-head partial resume state"
    (2088-2260) — after the marker scan, when `selected_payload is None`
    and `REVIEW_RESUME_FROM_COMMENTS_ENABLED` is true, invoke the helper
    and merge its exports; the marker path, when it matches, still wins
    and exports `AUTOFIX_RESUME_SOURCE=marker`. Log
    `AUTOFIX_RESUME_SOURCE pr=… source=<marker|comment|none> round=…`.
12. `review_autofix.yml` "Post partial finalize comment and persist
    runtime marker" (5909-6160) — append `progress_fingerprint=${PROGRESS_FINGERPRINT}`
    to the fenced text block (after `resume_should_continue=`); no other
    field changes. `README.md:1586` field list updated.
13. `review_autofix.yml` runtime export block — add
    `REVIEW_STATE_CACHE_DIR=${RUNNER_TEMP}/review-state/pr-${PR_NUMBER}`.
14. `review_autofix.yml` — new step "Hydrate review state from cache"
    directly after "Restore review-issue ledger" (2069): `mkdir -p` the
    three workspace targets and `cp -a` from
    `${REVIEW_STATE_CACHE_DIR}/{review_issue_ledger,review_runtime,ledger}`
    when present. New step "Stage review state for cache" directly before
    "Save review-issue ledger" (4067) and before "Save review-issue ledger
    after partial finalize" (6324): `rm -rf` then `cp -a` the three
    workspace paths into the staging directory. Change the `path:` of
    2080-2083, 4072-4075, 6329-6332 to the three staging subpaths. Keys
    and restore-keys unchanged.
15. `.github/workflows/validate.yml` 620-633 — point the restore `path:`
    at `${RUNNER_TEMP}/review-state/pr-<pr_number>/review_runtime/` and
    add the same hydrate copy into
    `${{ steps.workspace_state.outputs.workspace_path }}/.ai/review_runtime/`.
16. `scripts/stage_workflow_support.sh:45` — add
    `review_resume_from_comments.py` to `REQUIRED_BOOTSTRAP_SCRIPTS`;
    `review_autofix.yml:238-244` — add it to
    `REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS`.
17. `review_autofix.yml` top-level `env:` — add
    `REVIEW_RESUME_FROM_COMMENTS_ENABLED: ${{ vars.REVIEW_RESUME_FROM_COMMENTS_ENABLED || 'true' }}`.
18. `agents.md` — add `AUTOFIX_RESUME_SOURCE` to the stable prefixes;
    `docs/scripts-pending-removal.md` — registry entry for the helper;
    `README.md` — describe the comment-sourced resume and the staging
    directory. Tests and `changelog.d/<pr>-resume-round-from-comments.md`.

## Files & Modules

- `scripts/review_apply_fixes.sh` (P1)
- `.github/workflows/review_autofix.yml` (P1, P2)
- `.github/workflows/validate.yml` (P2)
- `scripts/review_resume_from_comments.py` [new] (P2)
- `scripts/stage_workflow_support.sh` (P2)
- `agents.md`, `README.md` (P1, P2)
- `docs/scripts-pending-removal.md` (P2)
- `changelog.d/<pr>-deterministic-editor-failure.md` [new] (P1)
- `changelog.d/<pr>-resume-round-from-comments.md` [new] (P2)
- `tests/test_review_autofix_editor_noop_cascade_contract.py` (P1)
- `tests/test_review_apply_fixes_smoke_deterministic.py` (P1)
- `tests/test_review_autofix_review_pipeline_contract.py` (P1, P2)
- `tests/test_review_resume_from_comments.py` [new] (P2)
- `tests/test_workspace_cache_maintenance.py`, `tests/test_review_synthesise_smoke.py` (P2, cache path assertions)

## Tests

**P1, unit / contract**

- `test_review_apply_fixes_smoke_deterministic.py`: the script text
  defines `EDITOR_ATTEMPT_STARTED_FILE` with the `${RUNTIME_DIR}` default
  and writes it after `LaunchingAgentProcess`; a stub run that exits before
  the loop leaves no marker, a stub run that reaches attempt 1 leaves one.
- `test_review_autofix_editor_noop_cascade_contract.py`: the summary step
  contains the deterministic branch **before** the empty-noop branch; the
  branch does not contain `AUTOFIX_EDITOR_EMPTY_NOOP=true`; it references
  `AUTOFIX_EDITOR_SCRIPT_RC`, `EDITOR_ATTEMPT_STARTED_FILE`, and
  `AUTOFIX_EDITOR_DETERMINISTIC_FAIL_SECS`; the editor step exports rc and
  elapsed and re-raises the rc; the two flags default to `true` / `120`.
- `test_review_autofix_review_pipeline_contract.py`: the new terminal step
  sits between "Post partial finalize comment…" and "Save review-issue
  ledger after partial finalize" with the stated condition.

**P1, e2e (recorded in the PR before merge)** — one dispatched run with the
smoke editor override forcing an immediate `exit 1`: comment title present,
`AUTOFIX_EDITOR_DETERMINISTIC_FAILURE` line present, linked issue labelled.

**P2, unit** — `tests/test_review_resume_from_comments.py`: picks the
highest round for the matching head; ignores other heads and non-marker
comments; tolerates a missing `progress_fingerprint` line (older
comments); malformed JSON / missing file → no output, exit 0.

**P2, contract** — cache `path:` values in both workflows reference
`REVIEW_STATE_CACHE_DIR`; hydrate/stage steps are adjacent to their
save/restore steps; the comment text block contains
`progress_fingerprint=`; `README.md:1586` lists the field.

**P2, e2e (recorded in the PR)** — two dispatched runs on one head with the
editor forced to a recoverable failure: second run logs
`AUTOFIX_RESUME_SOURCE … source=comment round=1` and posts
`resume_round=2`; "Restore review-issue ledger" logs a cache hit.

## Risks & Mitigations

- A genuinely transient crash that happens within 120 s and before the
  first launch is classified deterministic. Mitigation: the marker is
  written before any network call, so "before launch" means a
  precondition, not a model failure; the bound is a repo var; the outcome
  (review-blocked) is recoverable by the judge. ACCEPTED — bounded and
  reversible.
- Source-repo PR branches whose staged `review_apply_fixes.sh` predates
  the marker never write it. Mitigation: the elapsed bound; a slow
  failure on such a branch still takes today's path.
- `ai:review-blocked` on an orchestrator-managed issue hands the PR to the
  review-blocked judge, which may close-and-reissue. That is the intended
  terminalization. ACCEPTED.
- `2> >(tee …)` process substitution in the editor step could reorder
  interleaved stdout/stderr in the job log. Mitigation: only the stderr
  tail file is consumed programmatically; ordering in the log is
  cosmetic.
- Comment parsing depends on the workflow's own comment format.
  Mitigation: writer and reader live in the same file; a contract test
  pins the field names; the parser fails open.
- Staging directory copies add a few seconds per run. ACCEPTED.
- `validate.yml` and `review_autofix.yml` must agree on the staging path;
  a mismatch silently reverts to today's behaviour (no hit). Mitigation:
  both derive it from `RUNNER_TEMP` and the PR number only; a contract
  test compares the two literals.
- Actual permission mode of `/home/runner` on hosted runners is unknown;
  irrelevant to this plan (no identity switch here). ACCEPTED — pending
  the PR #4057 preflight result.

## Rollout

- Both phases ship on by default (D6). Per-repo off switches:
  `AUTOFIX_DETERMINISTIC_EDITOR_FAILURE_ENABLED=false`,
  `REVIEW_RESUME_FROM_COMMENTS_ENABLED=false`.
- No migration. Old cache entries under the unchanged keys are simply
  never hit again after P2 (their embedded paths were never restorable
  anyway).
- Consumers receive both phases on the next `@stable` release through the
  existing `update_workflows.yml` sync (§14); no wrapper edits, no
  consumer action.
- Rollback per phase: flag off, or revert the phase PR.

## References

- PR #4057 and runs 34257678201, 34266425657, 34283641666, 34290972060,
  34297628750, 34304993091, 34320556598
- `docs/postmortems/2026-05-18-project-2734-stall.md`
- CLAUDE.md §4, §5, §6, §14, §15, §18, §19, §20
- `agents.md` "Stable log prefixes (contractual)", "Run-substate ledger"
