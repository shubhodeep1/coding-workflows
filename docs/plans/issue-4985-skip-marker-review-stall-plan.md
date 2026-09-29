# Honour only an intentional skip-AI marker, log every gate skip, and hand stalled claude/* reviews to a fixer

Source issue: shubhodeep1/coding-workflows#4985 (https://github.com/shubhodeep1/coding-workflows/issues/4985)
Base branch: main
Security pass: run

## Summary

The review gate in `.github/workflows/review_autofix.yml` skips a PR whenever
the marker `[skip ai]` appears anywhere in its title or body. PR #4807 only
*quoted* the marker in its description, so its review was skipped silently,
and the §26 / project checkers waited about 16 hours because
`check_in_status.py` cannot see a review that never happened. This project:

- makes the gate (and the two sweeps that mirror it) honour only an
  intentional marker;
- logs every gate skip and makes a marker skip on a `claude/*` PR visible
  with one comment per head;
- teaches `check_in_status.py --hand-back` to report a head that was never
  reviewed as `review-stalled`, which a fixer answers by re-dispatching the
  review.

## Context

- Issue #4985 (OWNER), filed by the master session under the operator's fix
  rule. Incident: PR #4807 (phase 1 of #4785), run 36414886805,
  `should_run=false`, `skip_reason=skip_ai_marker`; the body said
  "AD-3: \`[skip ai]\` plus a head-ref skip…".
- The gate: `review_autofix.yml` step "Evaluate review gate", lines 539–545:
  `elif printf "%s" "${PR_TITLE} ${PR_BODY}" | grep -Fq "[skip ai]"`.
  `PR_TITLE` / `PR_BODY` are the wrapper inputs `pr_title` / `pr_body`
  (empty on `workflow_dispatch`, so a dispatched run never honours the
  marker). `pr_skip_ai` is a precomputed flag; every in-repo wrapper passes
  `false`.
- The same substring rule is copied in `.github/workflows/review_autofix_sweep.yml`
  (lines 283–290, "parity with review_autofix.yml gate behaviour") and in
  `scripts/claude_pr_sweep.py` `list_candidates` (line 124), which is the
  CLAUDE.md §26.H catch-all. So the catch-all also skipped PR #4807.
- The gate logs `AUTOFIX_GATE_SKIP reason=…` only for three reasons
  (`self_triggered_autofix`, `claude_fixer_awaiting_session`,
  `terminal_same_head`). The outputs are written at lines 1524–1554.
- Every reviewed `claude/*` head leaves one of two traces:
  - a workflow hand-off comment for that head
    (`scripts/review_autofix_step_claude_fixer_handoff.sh`, findings or
    conflict; a clean ledger without a fresh check snapshot also posts one);
  - PR auto-merge enabled (a clean review, `CLAUDE_FIXER_ZERO_FINDINGS`, or
    the deterministic skip).
  The PR object (`pulls/N`: `draft`, `auto_merge`, labels, title, body) and
  the comments are already read by `check_pr_hand_back`, so a stall can be
  told apart with no new read type (§15).
- `ai:review-skipped` is defined in `.github/ai/label_contract.v1.json` as
  "Reviewer panel + editor cycle skipped by deterministic gate (doc-only or
  under size threshold)". `docs/how-it-works.md` maps it to the linked issue's
  `ai:ready-to-merge` transition.
- `ai:merge-queued` (`scripts/review_merge_train.sh`) defers a review behind
  an older PR on purpose.
- `.claude/scripts/dispatch_workflow.py` allows six workflows.
  `internal-review.yml` is not one of them. Its dispatched runs are the only
  runs `check_in_status.py` binds to a PR by title (`[pr:N]`,
  `_is_pr_dispatched_review_run`) and counts as active
  (`_active_run_count`). `.claude/settings.json` mirrors the allowlist, and
  `tests/test_dispatch_workflow.py` asserts the two match.
- The interim twin-first rule (#4750 Q40: A, restated in this issue:
  "edit the `workflow-templates/.claude/**` twin first, Q40") applies:
  - `.claude/**` changes go only into their `workflow-templates/.claude/**`
    twins;
  - the phase PR gets a `hold` claim, and the stage stops BLOCKED with an
    `ai:claude-blocked:v1` comment listing the files to copy;
  - the supervising session syncs them as `[claude-twin-sync]`. The
    `settings.json` twin needs the operator's approval window (Q62/Q64,
    batched per Q67).

## Goals

- A PR whose body quotes the marker (in backticks, in a code fence, or mid
  sentence) is reviewed; the title marker and a body line holding only the
  marker still skip. Same rule in the gate, `review_autofix_sweep.yml`, and
  `claude_pr_sweep.py`, pinned by one parity test.
- Every `should_run=false` gate outcome logs
  `AUTOFIX_GATE_SKIP reason=<skip_reason> pr=<n> head_sha=<sha>`.
- A non-draft `claude/*` PR skipped for `skip_ai_marker` or
  `draft_or_skip_ai` gets one comment per head naming the reason and how to
  undo it, with a machine-readable marker.
- `check_in_status.py --hand-back` reports `state: review-stalled`
  (`action: hand_back_fixer`, `kind: review`) for a `claude/*` head that:
  - has no hand-off, no auto-merge, no gate skip comment, no intentional
    marker, and no workflow run queued / running / pending;
  - is not a draft and not `ai:merge-queued`;
  - is older than `CLAUDE_REVIEW_STALL_HOURS` (default 2).
  Before that threshold, or when any of those signals is present, it reports
  `wait`. The catch-all sweep treats `review-stalled` as due.
- `/fix-claude-pr` answers `review-stalled` by claiming the head and
  re-dispatching the review through `dispatch_workflow.py`
  (`internal-review.yml --input pr_number=<N>` here, `ai-review.yml` in
  consumer repos). A head it already re-dispatched is held and asked about
  instead.

## Non-goals

- No change to the project checker's plain `--pr` mode (see AD-12): the
  hourly §26.H catch-all runs hand-back mode over every `claude/*` PR,
  `claude/implement-plan-*` included.
- No new label; `ai:review-skipped` keeps its contract (AD-4).
- No comment for other skip reasons. They are logged only.
- No `run-name` for the consumer `ai-review.yml` wrapper, so a dispatched
  consumer review still cannot be bound to its PR by title. That gap
  already exists; see Risks.
- No change to `pr_skip_ai`, `pr_title`, `pr_body`, `skip_ai_marker`,
  `draft_or_skip_ai`, or `AUTOFIX_SWEEP_SKIP … reason=skip_ai_marker` (§6).

## Constraints

- §6: keep every existing reason name, input, output, log key, claim kind
  and state. `review-stalled`, `CLAUDE_REVIEW_STALL_HOURS`,
  `AUTOFIX_GATE_SKIP_NOTICE`, `stall_redispatched` and the marker
  `ai:claude-fixer-review-skipped:v1` are new names, checked unique across
  the repo.
- §4: `CLAUDE_REVIEW_STALL_HOURS` defaults to 2 in the script and in the
  sweep job (`vars.CLAUDE_REVIEW_STALL_HOURS || '2'`).
- §15: the new stall check reads nothing before its zero-call exclusions.
  After them it adds at most the head-commit read and the active-run reads
  that `check_pr_hand_back` already budgets. The gate's per-head comment
  dedupe reuses `gate_fetch_marker_comments` (one `GET /user` plus one
  paginated comments call, shared with the Claude-fixer checks).
- §27: `review_autofix.yml` is 454,700 bytes. The inline additions bring it to
  460,048 bytes, about 20 KB under the 480,000 guard.
- §8: structured `AUTOFIX_GATE_SKIP` / `AUTOFIX_GATE_SKIP_NOTICE` lines.
- §19: PR bodies use `Refs #4985` except the final PR (`Fixes #4985`).
  PR titles and bodies never carry the literal marker, because the gate on
  `main` still matches it anywhere.
- §20: one `changelog.d/4985-…` fragment. §28 / Q40: twin-first for `.claude/**`.
- §9: tabs in Python and Markdown code, 2-space YAML.

## Approach

1. **One marker rule.** A title containing the marker counts, as today. A
   body line counts only when, outside a ``` / ~~~ fenced block, it matches
   `^ {0,3}\[skip ai\][ \t]*$` (a trailing `\r` is ignored).
   - Python: `has_skip_ai_marker(title, body)` in `check_in_status.py`. The
     catch-all sweep already loads that module, so both use one copy.
   - Bash: one `awk` program, identical in the gate and
     `review_autofix_sweep.yml`.
   - `tests/test_skip_ai_marker_rule.py` runs both awk copies (extracted
     from the YAML) and the Python function over one case table, and
     requires the same answers.
2. **Gate.**
   - Replace the substring check with the rule.
   - Before the outputs, log `AUTOFIX_GATE_SKIP reason=… pr=… head_sha=…`
     whenever `SHOULD_RUN=false`.
   - For a PR-backed `claude/*` head skipped for `skip_ai_marker`, or for
     `draft_or_skip_ai` on a non-draft PR (`pr_skip_ai=true`), post the
     comment below unless the workflow account already posted one for this
     head. Every failure only warns.
   - Comment: "## Review skipped: <reason>", the head, and how to undo it
     (remove the marker and push, or dispatch the review workflow for the
     PR, which ignores the PR text). It ends with
     `<!-- ai:claude-fixer-review-skipped:v1 reason=<reason> head=<sha> -->`.
     The `ai:claude-fixer-` prefix makes the existing gate comment fetch
     return it.
3. **Checker** (`workflow-templates/.claude/scripts/check_in_status.py`). In
   `check_pr_hand_back`, where a clean `claude/*` head returns `open`, call
   `_review_stall_verdict`. Zero-call exclusions come first:
   - draft, `auto_merge` set, or `ai:merge-queued`;
   - the intentional marker in the PR text;
   - any trusted hand-off for the head;
   - a trusted gate-skip comment for the head.
   Then the head commit's age against `CLAUDE_REVIEW_STALL_HOURS`, then
   `_active_run_count(..., include_pending=True, pr_number=N)`. A stalled
   head is due with `since` = the head commit time, `kind: review`, and
   `stall_redispatched` = a trusted `review` claim on this head by a
   claimant not in `--ignore-claim-by`.
   - Route: `hand_back` → `review-stalled` → `hand_back_fixer`.
   - The catch-all adds `review-stalled` to `DUE_STATES`, and its
     `list_candidates` uses `has_skip_ai_marker`.
4. **Fixer** (`workflow-templates/.claude/commands/fix-claude-pr.md`), for
   `review-stalled`:
   - `stall_redispatched` true → hold and ask;
   - otherwise claim `--kind review`, then dispatch the review once with
     `dispatch_workflow.py` (no push). Its hand-back step stays as is.
   - `internal-review.yml` joins `DISPATCHABLE_WORKFLOWS` and the
     `settings.json` allow list (twins).
5. **Docs.**
   - CLAUDE.md §26.C / §26.D / §26.H, plus its `workflow-templates/` copy;
   - README (env var table, the gate and §26.H prose);
   - `agents.md`;
   - the changelog fragment.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A, `/implement-issue-claude`) makes
every issue a single-phase plan. The pieces are also coupled:
- the gate comment and `check_in_status.py` share the marker;
- the sweeps share the marker rule;
- the fixer rule consumes the new state.

1. **Phase 1 — intentional-marker rule, skip logging and comment, review-stall
   detection and fixer re-dispatch.**
   - Files: see [Files & Modules](#files--modules).
   - Done when:
     - the new and updated tests pass against the twins;
     - `ruff check` is clean on the changed Python;
     - `review_autofix.yml` stays under 480,000 bytes;
     - `yamllint` is clean on the changed workflows;
     - the stage stops BLOCKED for the `[claude-twin-sync]`.
     The twin-parity tests (`test_template_copies_match`,
     `test_dispatch_workflow` settings parity, `test_update_workflows_guardrails`)
     pass only after the sync lands on the phase branch.
   - Rollback: revert the phase PR. Every piece is additive, except the
     marker rule, whose revert restores substring matching.

## Implementation Steps

1. `workflow-templates/.claude/scripts/check_in_status.py`:
   - add `SKIP_AI_MARKER`, `has_skip_ai_marker`, `REVIEW_SKIP_MARKER_RE`,
     `DEFAULT_REVIEW_STALL_HOURS`, `REVIEW_DEFERRED_LABELS`, and
     `_review_stall_verdict`;
   - `HAND_BACK_KIND_BY_STATE["review-stalled"] = "review"`;
   - `CHECKER_ROUTE_TABLE["hand_back"]["review-stalled"]`;
   - `read_fix_claims` also returns `stall_redispatched`;
   - update the docstrings (rules, budget, route table).
2. `workflow-templates/.claude/scripts/dispatch_workflow.py`: add
   `internal-review.yml`. `workflow-templates/.claude/settings.json`: add
   `Bash(gh workflow run internal-review.yml *)`.
   `workflow-templates/.claude/hooks/gh_api_write_guard.py`: add it to
   `DISPATCHABLE_WORKFLOWS`, which a test keeps equal to the allow rules.
   `workflow-templates/.claude/commands/implement-plan-claude.md`: the
   helper now allows seven workflows.
3. `workflow-templates/.claude/commands/fix-claude-pr.md`: the
   `review-stalled` route (step 2), claim (step 4), and fix branch (step 5).
4. `.github/workflows/review_autofix.yml` gate:
   - the marker rule (lines 542–544);
   - the skip log and `claude/*` comment before the outputs (line ~1524);
   - update the `pr_title` / `pr_body` input descriptions.
5. `.github/workflows/review_autofix_sweep.yml`:
   - the same awk rule (lines 283–290) and the header note (line 61);
   - `CLAUDE_REVIEW_STALL_HOURS` in the `claude-pr-catch-all` env.
6. `scripts/claude_pr_sweep.py`: `DUE_STATES` gains `review-stalled`;
   `list_candidates` uses `check_in_status.has_skip_ai_marker`.
7. Tests (see [Tests](#tests)).
8. Docs:
   - `CLAUDE.md` and `workflow-templates/CLAUDE.md`, §26.C table and step 4,
     §26.D, §26.H "When a fix is due";
   - `README.md`: env table row, §26.H catch-all prose, the gate/marker
     prose;
   - `agents.md`: check-in states;
   - `changelog.d/4985-skip-marker-review-stall.md`.

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py` (twin of `.claude/scripts/check_in_status.py`)
- `workflow-templates/.claude/scripts/dispatch_workflow.py` (twin)
- `workflow-templates/.claude/settings.json` (twin; operator approval window for the sync)
- `workflow-templates/.claude/hooks/gh_api_write_guard.py` (twin; its `DISPATCHABLE_WORKFLOWS` is kept equal to the settings allow rules by `tests/test_gh_api_write_guard.py`; operator approval window for the sync)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin; "six" → "seven" workflows in the dispatch helper description)
- `.github/workflows/ci.yml` (a step for `tests/test_skip_ai_marker_rule.py`)
- `workflow-templates/.claude/commands/fix-claude-pr.md` (twin)
- `.github/workflows/review_autofix.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `scripts/claude_pr_sweep.py`
- `tests/test_skip_ai_marker_rule.py` [new]
- `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`, `tests/test_dispatch_workflow.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`, `README.md`, `agents.md`
- `changelog.d/4985-skip-marker-review-stall.md` [new]

## Tests

- `tests/test_skip_ai_marker_rule.py` [new]. One case table checked against
  `has_skip_ai_marker` and against the awk program extracted from both
  workflow files:
  - skips: title marker; a body line holding only the marker; up to 3
    leading spaces, trailing spaces, CRLF;
  - does not skip: a backtick-quoted marker; a mid-sentence mention; a
    marker inside a ``` or ~~~ fence; 4-space indentation; a list item or
    heading holding the marker; empty text.
  Contract checks on `review_autofix.yml`:
  - the old substring check is gone;
  - the end-of-gate `AUTOFIX_GATE_SKIP reason=${SKIP_REASON} pr=… head_sha=…`
    line exists before the outputs;
  - the comment block is limited to `claude/*`, the two reasons, and
    non-drafts, carries the marker, dedupes per head through
    `gate_fetch_marker_comments`, and cannot fail the step.
- `tests/test_check_in_status_hand_back.py` (runs against the twin until the
  sync). Cases:
  - `review-stalled` after 2 h with no signals, with `hand_back_fixer`,
    `kind: review`, and `since` = the head commit time;
  - `wait` before the threshold;
  - `wait` for each exclusion: draft, `auto_merge`, `ai:merge-queued`,
    title marker, trusted gate-skip comment for this head (an untrusted or
    other-head one does not count), answered hand-off, active run;
  - the env override;
  - `stall_redispatched` with and without `--ignore-claim-by`;
  - the calls stay within budget.
  The existing `test_clean_claude_pr_is_open` is updated for the new head
  read.
- `tests/test_claude_pr_sweep.py`:
  - a body-quoted marker is a candidate; a title marker is not;
  - `review-stalled` is queued with kind `review`.
- `tests/test_dispatch_workflow.py`: the settings/allowlist parity covers
  `internal-review.yml`.
- Existing suites: `test_check_in_status.py`,
  `test_review_autofix_claude_fixer_mode.py`,
  `test_review_autofix_review_pipeline_contract.py`,
  `test_review_autofix_sweep_zero_candidate_fast_exit.py`,
  `test_workflow_file_size_limit.py`, `test_update_workflows_guardrails.py`,
  plus `yamllint` and `ruff check`.

## Risks & Mitigations

- **The comment is posted on every skipped head.** Mitigation: one per head,
  `claude/*` non-drafts only, deduped against the workflow account's own
  marker comments.
- **A re-dispatch loops on a head the gate keeps skipping for a reason that
  leaves no trace.** Mitigation:
  - `stall_redispatched` makes the second fixer hold;
  - the §26 checker hands a (head, state) pair back only once;
  - the claim lease spaces attempts by 3 hours.
- **Auto-merge enabled on an older head hides a later unreviewed head.**
  ACCEPTED: `auto_merge` has no head field. A synchronize run reviews the
  new head. A lost event there is the only miss, and the head still merges
  only through the existing head-bound authorization.
- **A dispatched consumer `ai-review.yml` run cannot be bound to its PR by
  title**, so its hand-off stays "waiting for verified completed review
  run". ACCEPTED: this is pre-existing (the step-7a convergence dispatch has
  it too). The review itself still runs, and a clean one auto-merges.
- **Twin-first: CI parity tests fail until `[claude-twin-sync]`.** ACCEPTED:
  Q40 by design. The hold claim keeps fixers off the PR until the sync.
- **Drafts, including `/implement-plan-claude`'s draft final PR, get no
  comment.** ACCEPTED (AD-5): GitHub shows the draft state, and
  `ready_for_review` starts the review.

## Rollout

- No flag. The gate change reaches consumers with the next `@stable`, since
  `review_autofix.yml` is reusable. `check_in_status.py` / `fix-claude-pr.md`
  reach them through the `.claude/` sync.
- `CLAUDE_REVIEW_STALL_HOURS` is optional (default 2).
- Rollback: revert the PR.

## References

- Issue #4985. Incident PR #4807, project #4785, run 36414886805.
- Related: #4910 (checker liveness), #4952 (stale branches), #4618
  (dispatched-run binding), #4622 (claim trust), #4750 (Q40 twin-first).

## Auto-decisions

- AD-1 [plan, 2026-09-29] How does phase 1 change `.claude/**`? — Picked: A — twin-first per Q40, as the issue says (edit only the `workflow-templates/.claude/**` twins, hold, stop BLOCKED for `[claude-twin-sync]`). Alternatives: B — edit `.claude/**` directly. Why: the operator's standing rule and the issue text. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What exactly counts as an intentional body marker? — Picked: A — outside ``` / ~~~ fences, a line matching `^ {0,3}\[skip ai\][ \t]*$` (CR ignored); the title keeps substring matching. Alternatives: B — also list items / headings that hold the marker; C — honour the title only. Why: the issue's "a body line that consists of the marker alone"; 4+ spaces is a Markdown code block. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Do the two sweeps adopt the same rule? — Picked: A — yes: `review_autofix_sweep.yml` (awk, parity-tested) and `claude_pr_sweep.py` (shared Python function). Alternatives: B — gate only. Why: with B the §26.H catch-all still skips the very PR a checker would hand off (#4807 was skipped there too), and the review sweep never re-dispatches a PR the gate would now review. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Add `ai:review-skipped` to a marker-skipped PR? — Picked: A — no; the hidden comment marker is the machine-readable signal. Alternatives: B — add the label. Why: the label contract defines it as the deterministic doc-only / size skip, and `docs/how-it-works.md` ties it to `ai:ready-to-merge`; the issue allows it only "if the label contract allows". Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Comment on drafts (`draft_or_skip_ai` because the PR is a draft)? — Picked: A — no: comment for `skip_ai_marker` and for `draft_or_skip_ai` only when the PR is not a draft (`pr_skip_ai=true`); drafts get the log line, and the checker never calls a draft stalled. Alternatives: B — comment on drafts too. Why: GitHub already shows the draft state, `ready_for_review` starts the review, and `/implement-plan-claude`'s draft final PR would get one comment per phase merge. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Where do the skip log and comment live? — Picked: A — inline at the end of the gate step. Alternatives: B — a new `scripts/review_autofix_step_*.sh`. Why: the gate job has no support-script checkout; the addition leaves the file at 460,048 bytes (§27 guard 480,000). Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What proves a head was reviewed or deliberately not reviewed? — Picked: A — any trusted hand-off for the head (answered or not), `auto_merge` set, a trusted gate-skip comment for the head, the intentional marker in the PR text, a draft, `ai:merge-queued`, or an active run. Alternatives: B — also match review check-run names. Why: all read from data `check_pr_hand_back` already fetches; check runs of dispatched runs do not attach to the PR head. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] What starts the stall clock? — Picked: A — the head commit's committer date, threshold `CLAUDE_REVIEW_STALL_HOURS` (default 2), which is also the verdict's `since`. Alternatives: B — the PR's `updated_at`. Why: `updated_at` moves on every comment; `since` = head time lets the catch-all act at 2 hours as the acceptance criterion asks. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-29] Which claim kind does `review-stalled` use? — Picked: A — the existing `review` kind. Alternatives: B — a new `stall` kind. Why: no change to the claim format, `claude_fix_claim.py`, or the `claude_pr_fix.v1` queue payload (§6, §5). Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-29] How are repeated re-dispatches on one head bounded? — Picked: A — the verdict carries `stall_redispatched` (a trusted `review` claim on this head by a claimant the caller does not ignore); the fixer holds and asks instead of dispatching again. Alternatives: B — no guard. Why: a skip that leaves no trace would otherwise re-dispatch every lease period. Applied in: phase 1. Status: pending review
- AD-11 [plan, 2026-09-29] Which workflow does the fixer re-dispatch? — Picked: A — `internal-review.yml --input pr_number=<N>` here (added to `DISPATCHABLE_WORKFLOWS` and the `settings.json` allow list, twins), `ai-review.yml` in consumer repos. Alternatives: B — `review_autofix.yml` directly (already allowed). Why: the issue names `internal-review.yml`; only its dispatched runs are bound to the PR by title, so the checker counts them as active and verifies their hand-offs. Applied in: phase 1. Status: pending review
- AD-12 [plan, 2026-09-29] Does the project checker's plain `--pr` mode also report stalls? — Picked: A — no, hand-back mode only, as the issue states; `/implement-plan-claude` PRs are covered by the §26.H catch-all, which runs hand-back mode over every `claude/*` PR. Alternatives: B — also plain mode, routed `hand_back`. Why: §5; no change to `/implement-plan-claude`'s routing table. Applied in: phase 1. Status: pending review
- AD-13 [plan, 2026-09-29] Log every skip even when an earlier `AUTOFIX_GATE_SKIP` line exists? — Picked: A — yes, one uniform end-of-gate line for every `should_run=false` (`head_sha=unknown` when absent). Alternatives: B — only when no earlier line. Why: one searchable line per run, as the issue specifies; earlier lines keep their extra fields. Applied in: phase 1. Status: pending review

## Notes

- Protected-path approval: phase 1 — twin-first per Q40 (issue #4985 body, owner-authored: "In `check_in_status.py` (edit the `workflow-templates/.claude/**` twin first, Q40)", 2026-09-29).
- `security_pass_skip.py`: `{"skip": false, "reason": "no skip label"}`.
