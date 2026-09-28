# Stop classifier-outage denials from stalling stage preflight and filing permission-prompt issues

Source issue: shubhodeep1/coding-workflows#4750 (https://github.com/shubhodeep1/coding-workflows/issues/4750)
Base branch: main
Security pass: run

## Summary

Issue #4750 reports an Auto-mode denial of `get_session` (claude-code-remote
MCP) with the reason `Classifier unavailable`. The call was already
allowlisted. The denial came from a transient outage of the Auto-mode
classifier, not from a missing rule. The operator made #4750 the single fix
for the whole classifier-outage cluster (decision Q39, issue comment
5868311173). This plan:

- makes `.claude/scripts/permission_prompts.py` leave classifier no-verdict
  and outage denials out of filing and out of the pattern list, and report
  them once as `classifier outage` instead;
- makes every interactive session retry a call the classifier refused without
  a verdict once, and, when it is refused again, wait about 30 minutes and try
  again instead of asking, filing, or stopping at `Status: BLOCKED`
  (new CLAUDE.md §23.J);
- makes `/implement-plan-claude` resume stages stop calling `get_session` on
  themselves in step 0, and applies the §23.J rule to the `get_session` reads
  step 0 still makes;
- adds no allow rules.

## Context

- The issue body: event `PermissionDenied`, tool
  `mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__get_session`, input `{}` (the
  self lookup), reason `Classifier unavailable`. There were 5 occurrences in
  session `session_011pBDarB1ebFsodR5wUM4YS` (07:36:09Z–07:37:11Z), and
  comments add 5 more in `session_01X34Rkb13B5HJabtRPdz8ni` and 3 in
  `session_01EZHGCdV7TY945CtCcrSunG` (09:03Z). The first two sessions are
  `/implement-plan-claude` resume stages, both denied during step 0.
- The call is already allowlisted, exactly and as a whole server:
  `.claude/settings.json:96` (`mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a`) and
  `.claude/settings.json:104` (`…__get_session`).
  `.claude/commands/implement-plan-claude.md:372` already records that the
  Auto-mode classifier "has been seen to prompt on `get_session` in a
  checker". That is why the checker reads its own id from
  `CLAUDE_CODE_REMOTE_SESSION_ID` (`implement-plan-claude.md:235,249`; also
  `fix-claude-pr.md:13` and CLAUDE.md §26.B / §26.D).
- It was an outage, not a per-command gap. Every `ai:permission-prompt` issue
  filed on 2026-09-28 gives the same reason, `Classifier unavailable`: #4749,
  #4751, #4759, #4760, #4761, #4762, #4767, #4779, #4780 (closed by the
  operator as duplicates of #4750), and #4808 (open, filed after Q39). They
  are plain Bash reads like `git status`, `git fetch`, and `grep`.
  `permission_prompts.py` (`group_patterns`, `file_patterns`) files each one
  as a separate `ai:permission-prompt` + `ai:claude` issue, and each issue
  starts a full `/implement-issue-claude` Opus project for something no
  repository change can fix.
- `/implement-plan-claude` step 0 (`implement-plan-claude.md:9`) tells every
  session, resume stages included, to call `get_session` with no
  `session_id` to record its id and `permission_mode`. A resume stage skips
  the Auto-mode check (`:10`), and the invoking session already recorded the
  mode in the log's `Stage model: … Permission mode: <mode>` line (log
  format, `:289`). A resume stage therefore needs neither value from
  `get_session`.
- `.claude/hooks/permission_prompt_logger.py:64-73` stores the reason Claude
  Code gives (`reason` or `decision_reason`) on every record, so the filer can
  tell an outage denial apart.
- Operator decisions on #4750 (comment 5868311173, 2026-09-28):
  - Q39 A: #4750 is the single fix for the cluster, and the scope is the
    three items in the Summary, with no new allow rules.
  - Q1 A with Q40 A, the interim twin-first rule until #4785 lands: every
    `.claude/**` change is made only in its `workflow-templates/.claude/**`
    twin, and `.claude/**` is never edited by the chain. Every other change is
    made as usual. The affected tests run locally against the twin versions.
    The phase PR is pushed with its twin-parity checks failing (expected), a
    `hold` claim is posted on its head, and the chain stops BLOCKED with an
    `ai:claude-blocked:v1` comment listing every twin file to copy. The
    supervising session copies the twins into `.claude/**` on the PR branch
    as `[claude-twin-sync]`, runs the tests, pushes (which lifts the hold),
    and comments `/reclarify`.

## Goals

- `permission_prompts.py report` and `file` leave every classifier
  no-verdict or outage denial (`is_classifier_outage`: a `PermissionDenied`
  whose reason matches `CLASSIFIER_OUTAGE_REASON_RE`) out of `patterns`,
  `total`, `filed-state.json`, and filing. They report those records once,
  under a new `outage_denials` summary key
  (`{label: "classifier outage", count, tools, first_ts, last_ts}`). `file`
  never opens an issue or posts a comment for them, and an outage-only log
  makes no API call.
- New CLAUDE.md §23.J (Auto-mode classifier outages), in both copies: a call
  refused without a verdict is retried once; refused again, the session does
  not ask, file, or stop at `Status: BLOCKED` on it, and instead arms one
  `send_later` about 30 minutes out that repeats the refused step (or, when a
  checker already holds its wait, leaves the wait to the checker) and ends the
  turn. It applies to every tool, `get_session` and the other
  claude-code-remote reads included, and no allow rule is added for it.
- `/implement-plan-claude` step 0 gives a resume stage its own session id
  from Bash (`echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`) and its
  permission mode from the log's `Permission mode:` line, so a resume stage
  makes no self `get_session` call. It falls back to `get_session` only when
  the log has no such line. Every `get_session` step 0 still makes follows
  §23.J.
- The stage report's `Permission prompts:` line shows outage denials once as
  `classifier outage: <n> (not filed)`.

## Non-goals

- No `.claude/settings.json` change and no new allow rule (Q39 item 3).
- No edit to any `.claude/**` file by the chain (Q40 twin-first rule). The
  `.claude/**` copies change only through the supervising session's
  `[claude-twin-sync]` commit.
- No change to `claude-issue-pickup.md`, `implement-issue-claude.md`,
  `fix-claude-pr.md`, or the checker prompt template (AD-4). §23.J lives in
  CLAUDE.md, which every interactive session in the repo loads, checker and
  pickup sessions included.
- No change to `permission_prompt_logger.py`. It keeps recording every event.
- No closing of #4808, which was filed after Q39. Closing an issue this
  session did not open is CLAUDE.md §23.C; the report names it for the
  operator.

## Constraints

- §5 minimal change set: only the filer's outage split, the new §23.J, the
  step-0 text, the report line, and the docs that describe them.
- §6 naming immutability: existing summary keys (`total`, `patterns`,
  `filed`, `commented`, `errors`, `skipped`, `dry_run`) keep their names;
  section numbers are not renumbered (§23.J is new). New identifiers
  (`CLASSIFIER_OUTAGE_REASON_RE`, `CLASSIFIER_OUTAGE_LABEL`,
  `is_classifier_outage`, `split_classifier_outages`,
  `classifier_outage_summary`, `summarize_permission_records`, summary key
  `outage_denials`) must not collide
  with anything in `permission_prompts.py` or the `check_in_status` module it
  loads.
- §7 / §23.I: CLAUDE.md §23.I, `agents.md` (permission prompts section), and
  `README.md` describe what the filer files, so they are updated in the same
  PR.
- §9 style: tabs in Python, matching the file.
- §15: the filer issues fewer API calls (none for an outage-only log), never
  more.
- §20: observable behaviour changes, so the PR adds a `changelog.d/`
  fragment.
- §28.C / Q40: protected paths are edited only in their
  `workflow-templates/.claude/**` twins.

## Approach

1. Filer (twin only): add `CLASSIFIER_OUTAGE_REASON_RE`,
   `is_classifier_outage(record)`, `split_classifier_outages(records)`, and
   `classifier_outage_summary(outages)`. `report()` and `file_patterns()`
   load and split the log once and share
   `summarize_permission_records(patterns, outages)`, so they group only the
   records that are not outages, and the summary adds `outage_denials`.
   The regex matches, case-insensitively, `classifier` followed by
   `unavailable`, `error`, `timed out`, `timeout`, or `overloaded` (with an
   optional `is`), or a no-verdict phrase (`no verdict`, `without a verdict`,
   `did not return a verdict`, `could not reach a verdict`) with the word
   `classifier` on the same line (AD-8). It matches only on
   `PermissionDenied`, never on a `PermissionRequest` (AD-5).
2. CLAUDE.md §23.J (both copies), and a one-line pointer in §23.I.
3. Command file (twin only): step 0 resume stages take their id from the
   environment and their mode from the log; a `get_session` refused without a
   verdict follows §23.J; the Permission prompt report section and the
   Output Format `Permission prompts:` line describe the outage count.

Alternatives considered (see Auto-decisions): an allow rule, which already
exists and did not prevent the denial (and Q39 excludes); filing one shared
outage issue, which would still start a Claude project for something that
cannot be fixed; and adding the retry rule to every command file, which
CLAUDE.md already reaches.

## Phases & Merge Strategy

This plan is **one phase**. Issue mode (CLAUDE.md §28.A) authorises a
single-phase plan for a standalone issue.

1. **Phase 1 — classifier-outage handling: filer split, §23.J retry rule,
   and outage-tolerant step 0.**
   - Files: the list under Files & Modules.
   - Protected paths: `.claude/commands/implement-plan-claude.md`,
     `.claude/scripts/permission_prompts.py`. Run under the recorded Q1 A /
     Q40 A approval: changed only in their `workflow-templates/.claude/`
     twins, copied by the supervising session.
   - Done when:
     - the twin `permission_prompts.py` excludes classifier-outage denials
       from patterns and filing and reports `outage_denials`;
     - the twin `implement-plan-claude.md` step 0 carries the env-id,
       log-mode, and §23.J rules, and its report line shows the outage count;
     - CLAUDE.md §23.J exists in both copies, byte-identical;
     - `agents.md`, `README.md`, and the changelog fragment are updated;
     - `tests/test_permission_prompts.py` and
       `tests/test_implement_plan_claude_command.py` cover the change and pass
       locally against the twin versions (a scratch copy of the tree with the
       twins in place of `.claude/**`);
     - the PR is pushed, a `hold` claim is posted on its head, and the
       `ai:claude-blocked:v1` twin-copy comment is on #4750.
   - Rollback: revert the phase PR. There is no data or state migration, and
     `filed-state.json` keys are unchanged.

## Implementation Steps

Phase 1:
1. `workflow-templates/.claude/scripts/permission_prompts.py`: the outage
   split (Approach 1) and the module docstring.
2. `workflow-templates/.claude/commands/implement-plan-claude.md`: step 0,
   the Permission prompt report section, and the Output Format line.
3. `CLAUDE.md` and `workflow-templates/CLAUDE.md`: §23.J and the §23.I
   pointer.
4. `agents.md` permission-prompt bullets and the `README.md` note.
5. Tests: `tests/test_permission_prompts.py` (outage-only log files nothing
   and calls no API; a mixed log files only the real pattern and reports the
   outage; `report` shows `outage_denials`; a non-outage denial reason and a
   `PermissionRequest` with outage text are still filed; §23.J documented) and
   `tests/test_implement_plan_claude_command.py` (step 0 rules).
6. `changelog.d/4750-classifier-outage-denials.md` (§20, section `fixed`).
7. Run the tests against the twins in a scratch copy, push, post the hold,
   and stop BLOCKED with the twin-copy list (Q40).

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `CLAUDE.md`
- `workflow-templates/CLAUDE.md`
- `agents.md`
- `README.md`
- `tests/test_permission_prompts.py`
- `tests/test_implement_plan_claude_command.py`
- `changelog.d/4750-classifier-outage-denials.md` [new]
- Copied by the supervising session, not edited here:
  `.claude/scripts/permission_prompts.py`,
  `.claude/commands/implement-plan-claude.md`.

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest
  tests/test_permission_prompts.py tests/test_implement_plan_claude_command.py
  -q`, run in a scratch copy of the tree where the two twins replace their
  `.claude/**` copies.
- Parity: in the real tree, `test_template_parity` in both files fails until
  the `[claude-twin-sync]` commit, which is expected (Q40 step 2). CLAUDE.md
  parity (`tests/test_update_workflows_guardrails.py` and similar) passes.
- The other `ci.yml` Python test steps that read CLAUDE.md, run in the real
  tree.

## Risks & Mitigations

- A real permission gap whose reason happens to match the outage regex would
  go unfiled. Mitigation: the regex needs `classifier` next to an outage word
  or a no-verdict phrase, matches only `PermissionDenied`, and the count stays
  visible in every stage report's `Permission prompts:` line.
- A long classifier outage wakes a waiting session every 30 minutes.
  ACCEPTED (Q39 item 2): one wake per 30 minutes costs far less than one
  Opus project per command shape, and the chain's 24-hour safety net and the
  checker still run.
- `send_later` can itself be refused during the outage. Mitigation: §23.J
  says to end the turn anyway and name the refused step in the report; the
  stage's safety net or the checker picks the wait up.
- A resume stage's log could lack a `Permission mode:` line (older projects).
  Mitigation: step 0 falls back to a self `get_session` under §23.J.
- CI on the phase PR is red on template parity until the twin sync. Accepted
  by Q40; the hold keeps the checker and the §26.H sweep from starting a
  fixer for it.

## Rollout

The change lands on `main` through the project's final PR and reaches consumer
repos on the next `@stable` sync (the `workflow-templates/` copies). There is
no flag. Rollback is a revert.

## References

- Issue #4750 and operator decisions Q1, Q39, Q40 (comment 5868311173).
- Duplicates closed by the operator: #4749, #4751, #4759, #4760, #4761,
  #4762, #4767, #4779, #4780. Filed later: #4808.
- #4785 (twin-first `.claude/` changes, not landed yet).
- CLAUDE.md §23.I, §26.B, §28.
- `.claude/commands/implement-plan-claude.md:9-12,164,356,372`.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should #4750 be fixed, given the call was
  already allowlisted and the reason was `Classifier unavailable`?
  - Picked: A. Treat it as a classifier outage. Remove the resume stage's
    self `get_session` from step 0, cap `get_session` retries at one, and
    stop filing outage denials.
  - Alternatives: B. Only the command-file change. C. Only the filer change.
    D. Add an allow rule; it already exists at `.claude/settings.json:104`.
  - Why: the command change removes the observed denials, and the filer
    change stops one outage from starting N Opus projects.
  - Applied in: phase 1 PR. Status: pending review.
- AD-2 [plan, 2026-09-28] What should the filer do with classifier-outage
  denials?
  - Picked: A. Report them as a count under a new `outage_denials` key and
    never file them.
  - Alternatives: B. File one shared outage issue and comment on it at each
    outage. C. Keep filing them per pattern.
  - Why: no repository change can fix an outage, and every filed issue is
    routed to a Claude project. Q39 item 1 confirms it.
  - Applied in: phase 1 PR. Status: pending review.
- AD-3 [plan, 2026-09-28] How is an outage denial recognised?
  - Picked: A. A `PermissionDenied` record whose reason matches
    `classifier unavailable` (case-insensitive).
  - Alternatives: B. Any `PermissionDenied` whose reason mentions
    "classifier". C. Any `PermissionDenied`.
  - Why: the exact text in all eight issues.
  - Applied in: phase 1 PR. Status: superseded by AD-5 (operator Q39 item 1
    names "no-verdict or outage" errors, wider than A).
- AD-4 [plan, 2026-09-28] Which command files change?
  - Picked: A. Only `/implement-plan-claude` step 0 and its template mirror,
    where every observed denial came from.
  - Alternatives: B. Also `implement-issue-claude.md` and
    `claude-issue-pickup.md`.
  - Why: §5 minimal change set. The general retry rule (Q39 item 2) goes in
    CLAUDE.md §23.J, which every interactive session loads (AD-6).
  - Applied in: phase 1 PR. Status: pending review.
- AD-5 [phase 1/1, 2026-09-28] Which reasons count as a classifier
  no-verdict or outage error (Q39 item 1)?
  - Picked: A. `PermissionDenied` only, reason matching `classifier` followed
    by `unavailable`, `error`, `timed out`, `timeout`, or `overloaded` (with
    an optional `is`), or a no-verdict phrase (`no verdict`, `without a
    verdict`, `did not return a verdict`, `could not reach a verdict`).
  - Alternatives: B. Only `classifier unavailable` (AD-3). C. Any
    `PermissionDenied` that mentions "classifier".
  - Why: covers the no-verdict and outage wording Q39 names while a real
    block, which carries the classifier's reason for blocking, still files.
  - Applied in: phase 1 PR. Status: pending review.
- AD-6 [phase 1/1, 2026-09-28] Where does the retry-then-wait rule (Q39
  item 2) live?
  - Picked: A. A new CLAUDE.md §23.J next to §23.I, plus step 0 of
    `/implement-plan-claude` for the `get_session` reads it makes.
  - Alternatives: B. A new top-level §29. C. Every command file that calls a
    tool.
  - Why: §23.I already governs permission prompts, CLAUDE.md reaches every
    interactive session, and no section is renumbered (§6).
  - Applied in: phase 1 PR. Status: pending review.
- AD-7 [phase 1/1, 2026-09-28] How long does a session wait after a second
  refusal, and is there a cap?
  - Picked: A. One `send_later` with `delay_minutes: 30` per refusal, no cap;
    if `send_later` is refused too, end the turn and name the refused step in
    the report.
  - Alternatives: B. Back off (30, 60, 120 minutes). C. Stop BLOCKED after
    the fourth outage wake.
  - Why: Q39 item 2 says about 30 minutes and never BLOCKED; C contradicts
    it, and B adds state a stage does not keep.
  - Applied in: phase 1 PR. Status: pending review.

## Notes

- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`.
  The security pass runs.
- Protected-path approval: phase 1 — A, under the interim twin-first rule
  (Q40 A), recorded from the operator's comment 5868311173 on 2026-09-28.
