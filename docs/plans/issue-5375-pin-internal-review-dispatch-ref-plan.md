# Pin the pre-approved internal-review.yml dispatch to the default branch

Source issue: shubhodeep1/coding-workflows#5375 (https://github.com/shubhodeep1/coding-workflows/issues/5375)
Base branch: claude/implement-plan-issue-4985-skip-marker-review-stall
Security pass: skip (ai:security: automation-produced issue)

## Summary

Project #4985 pre-approved `internal-review.yml` dispatches in three places so
`/fix-claude-pr` can re-dispatch a stalled review without a permission prompt.
None of the three pins the git ref, so a session can be led into dispatching a
pushable branch's copy of the workflow, which runs with inherited secrets and
write permissions. This plan limits the unprompted path to the dispatch helper,
makes the helper dispatch `internal-review.yml` only from the default branch it
reads from GitHub, and accepts only a numeric `pr_number` as input.

## Context

- Security audit finding `untrusted-ref-review-dispatch` (issue #5375, severity
  high, confidence 9/10, `Refs #3576`), raised against the project branch of
  issue #4985 (`claude/implement-plan-issue-4985-skip-marker-review-stall`,
  draft final PR #5031).
- The three surfaces #4985 added:
  - `.claude/settings.json` allow rule `Bash(gh workflow run internal-review.yml *)`.
    The trailing `*` matches `--ref <any branch>`, and `gh workflow run` keeps the
    last `--ref`, so no narrower glob can pin the ref.
  - `.claude/scripts/dispatch_workflow.py` `DISPATCHABLE_WORKFLOWS` gained
    `internal-review.yml`; `--ref` is passed through unchecked, and inputs are
    free-form.
  - `.claude/hooks/gh_api_write_guard.py` `DISPATCHABLE_WORKFLOWS` gained
    `internal-review.yml`, so `gh api … actions/workflows/internal-review.yml/dispatches -f ref=<any>`
    is classified `routine` and approved without a prompt.
- Precedent: issue #4618 fixed the same class of defect for
  `review_autofix_sweep.yml`, which now dispatches `internal-review.yml` only
  from the default branch and passes only a validated PR number.
  `check_in_status.py` already counts a dispatched review run only when its
  `head_branch` is the default branch (`_is_pr_dispatched_review_run`), so a
  default-branch dispatch is also the only one the stall detection recognises.
- The only documented caller is `/fix-claude-pr` (`review-stalled` case), which
  runs `dispatch_workflow.py --workflow internal-review.yml --input pr_number=<N>`
  with no `--ref`, so it keeps working unchanged.

## Goals

- No pre-approved path dispatches `internal-review.yml` from a ref other than
  the repository's default branch.
- `dispatch_workflow.py --workflow internal-review.yml` reads the default branch
  from the REST API, refuses a `--ref` that differs from it (exit 1,
  `"dispatched": false`, no POST), and dispatches on that branch.
- The helper accepts exactly one input for `internal-review.yml`, `pr_number`,
  and only as a positive decimal integer; anything else is refused before any
  POST.
- `gh workflow run internal-review.yml …` is no longer on the allow list, and a
  raw `gh api` dispatch of `internal-review.yml` makes the §23.H guard ask.
- Docs (CLAUDE.md §23.H/§23.I, agents.md, `/implement-plan-claude` dispatch
  helper section, `/fix-claude-pr`) describe the pinned behaviour.

## Non-goals

- The six other `Bash(gh workflow run <file> *)` rules that already exist on
  `main` (`security-audit.yml`, `ai-security-audit.yml`, `internal-validate.yml`,
  `ai-validate.yml`, `review_autofix.yml`, `ai-review.yml`). They share the
  unpinned-`--ref` shape but predate this finding; see `## Notes` (AD-4).
- Changing `internal-review.yml` itself, `review_autofix_sweep.yml`, or
  `check_in_status.py`.

## Constraints

- §1: security first; every open choice below takes the more restrictive option
  when it costs no documented caller anything.
- §5: minimal change; only the three surfaces the finding names, their twins,
  tests, and the docs that describe them.
- §6: no identifier is renamed or removed. `DISPATCHABLE_WORKFLOWS` keeps its
  name in both files. The helper adds one new constant,
  `DEFAULT_BRANCH_ONLY_WORKFLOWS` (checked unique in both `.claude/scripts/` and
  `.claude/hooks/`), and one new input-key constant.
- §15: the helper already reads the repository once when `--ref` is omitted; for
  `internal-review.yml` it now makes that read every time (at most one extra REST
  read, none when `--ref` is omitted). The guard stays API-free.
- §20: security fix → one `changelog.d/` fragment.
- §28.C / protected paths: every `.claude/**` file this phase changes has a
  `workflow-templates/.claude/**` twin, so the phase runs under the interim
  twin-first default: only the twins are edited, and the phase PR is held for
  the `[claude-twin-sync]` copy.

## Approach

1. **Allow rule (AD-1).** Remove `Bash(gh workflow run internal-review.yml *)`.
   The already-allowlisted `dispatch_workflow.py` becomes the only unprompted
   path.
2. **Dispatch helper (AD-3, AD-5).** Add
   `DEFAULT_BRANCH_ONLY_WORKFLOWS = frozenset({"internal-review.yml"})` (a subset
   of `DISPATCHABLE_WORKFLOWS`) and
   `DEFAULT_BRANCH_ONLY_INPUT_KEYS = frozenset({"pr_number"})`. For those
   workflows, `dispatch()` validates the inputs first (only `pr_number`, matching
   `^[1-9][0-9]{0,9}$`), always reads `default_branch`, refuses a `--ref` that
   differs from it, and dispatches on it. Every other workflow behaves exactly as
   before.
3. **API guard (AD-2).** Drop `internal-review.yml` from the guard's
   `DISPATCHABLE_WORKFLOWS`, so a raw `gh api` dispatch of it is a `write` and
   asks. The guard's set stays equal to the `gh workflow run` allow rules, which
   its test already asserts.
4. **Tests** read the twins, so they pass before the sync; the template-parity
   tests stay red until the `[claude-twin-sync]` commit.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
three surfaces must change together, since a pinned helper beside an unpinned
allow rule or guard leaves the finding open.

1. **Phase 1 — pin the internal-review.yml dispatch to the default branch.**
   - Files: see `## Files & Modules`.
   - protected paths: `.claude/settings.json`, `.claude/scripts/dispatch_workflow.py`,
     `.claude/hooks/gh_api_write_guard.py`, `.claude/commands/fix-claude-pr.md`,
     `.claude/commands/implement-plan-claude.md` (twin-first).
   - Done when: the twin allow list has no `internal-review.yml` rule; the twin
     helper refuses a non-default `--ref`, a missing or non-numeric `pr_number`,
     and any other input for `internal-review.yml` with no POST, and dispatches a
     valid request on the default branch; the twin guard asks on a raw
     `internal-review.yml` dispatch; `tests/test_dispatch_workflow.py` and
     `tests/test_gh_api_write_guard.py` pass except the template-parity checks
     that wait on the twin sync; docs updated; changelog fragment added.
   - Rollback: revert the phase PR; the previous (unpinned) behaviour returns.

## Implementation Steps

1. `workflow-templates/.claude/settings.json`: remove the
   `Bash(gh workflow run internal-review.yml *)` allow rule.
2. `workflow-templates/.claude/scripts/dispatch_workflow.py`: add the two
   constants, input validation, and the default-branch pin in `dispatch()`;
   update the module docstring (surfaces, API calls).
3. `workflow-templates/.claude/hooks/gh_api_write_guard.py`: remove
   `internal-review.yml` from `DISPATCHABLE_WORKFLOWS`, with a comment naming
   issue #5375.
4. `workflow-templates/.claude/commands/implement-plan-claude.md` (Dispatch
   helper section) and `workflow-templates/.claude/commands/fix-claude-pr.md`
   (`review-stalled` case): say the helper dispatches `internal-review.yml` only
   from the default branch with a numeric `pr_number`.
5. `CLAUDE.md` and `workflow-templates/CLAUDE.md` §23.H routine row and §23.I
   helper row; `agents.md` stall re-dispatch paragraph.
6. Tests: `tests/test_dispatch_workflow.py` (new behaviour against the twin
   script, allow-rule equality per settings/script pair),
   `tests/test_gh_api_write_guard.py` (raw `internal-review.yml` dispatch asks).
7. `changelog.d/5375-pin-internal-review-dispatch-ref.md` (`security`).

## Files & Modules

- `workflow-templates/.claude/settings.json`
- `workflow-templates/.claude/scripts/dispatch_workflow.py`
- `workflow-templates/.claude/hooks/gh_api_write_guard.py`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/**` counterparts of the five files above (via the `[claude-twin-sync]` copy only)
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `agents.md`
- `tests/test_dispatch_workflow.py`
- `tests/test_gh_api_write_guard.py`
- `changelog.d/5375-pin-internal-review-dispatch-ref.md` [new]

## Tests

- Unit (`tests/test_dispatch_workflow.py`, twin script):
  - `internal-review.yml` with `pr_number=123` and no `--ref` dispatches on the
    default branch read from the API.
  - `--ref` equal to the default branch is accepted; any other `--ref` (for
    example `claude/unreviewed-head`) raises before any POST.
  - missing `pr_number`, non-numeric / zero / signed / whitespace / overlong
    values, and extra inputs (`allow_workflow_edits`, `ref`) raise before any
    read or POST.
  - other workflows keep an explicit `--ref` and skip the repository read.
  - `main([...])` reports a refused ref with exit 1 and `"dispatched": false`.
  - allow-rule equality: for each settings/script pair, the `gh workflow run`
    rules equal `DISPATCHABLE_WORKFLOWS - DEFAULT_BRANCH_ONLY_WORKFLOWS`, and the
    twin settings carry no `internal-review.yml` rule.
- Unit (`tests/test_gh_api_write_guard.py`, twin hook): a raw
  `internal-review.yml` dispatch (default or other ref) asks.
- Existing suites: the full `tests/test_dispatch_workflow.py`,
  `tests/test_gh_api_write_guard.py`, and the repo's template-parity tests; the
  parity checks stay red until the twin sync, then everything passes.

## Risks & Mitigations

- A tag named like the default branch could make the dispatch ref ambiguous.
  ACCEPTED — the helper sends the branch name GitHub reports as default, as
  `review_autofix_sweep.yml` does since #4618; creating such a tag needs the
  same push access as a branch (AD-5).
- An operator who typed `gh workflow run internal-review.yml …` by hand now gets
  a prompt. ACCEPTED — the helper is the documented path; the prompt is the
  point of the fix.
- Twin-first leaves the live `.claude/` copy unpinned until the
  `[claude-twin-sync]` commit. Mitigation: the phase PR is held and the
  twin-sync blocker is posted on the issue; the fix reaches `main` only with
  the final PR of this project, after the sync.

## Rollout

Ships through this project's final PR into the base branch
(`claude/implement-plan-issue-4985-skip-marker-review-stall`), then with
project #4985's final PR #5031 into `main`, and to consumer repos with the next
`@stable` `.claude/` sync. Consumers have no `internal-review.yml`, so for them
only the helper's refusal logic ships and nothing changes in practice.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should the `Bash(gh workflow run internal-review.yml *)` allow rule be restricted? — Picked: A — remove it; `dispatch_workflow.py`, which pins the ref, is the only pre-approved path. Alternatives: B — narrow it and add deny rules for `--ref` / `-r`; C — keep it. Why: a glob cannot pin `--ref` (`gh` keeps the last one), so B is bypassable and C leaves the finding open. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How should the §23.H API guard treat `gh api … internal-review.yml/dispatches`? — Picked: A — not routine: it asks, in every mode. Alternatives: B — routine only when `ref` equals a default branch derived from local git state. Why: the guard makes no API calls and local git state is not a verified default branch; the helper is the verified path. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Which inputs may the helper pass to `internal-review.yml`? — Picked: A — only `pr_number`, as a positive decimal integer (at most 10 digits). Alternatives: B — also `allow_workflow_edits` (`true` / `false`). Why: the only caller passes `pr_number` alone; the smallest accepted surface. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Should the other six pre-approved `gh workflow run <file> *` rules be pinned in this project too? — Picked: A — no; fix `internal-review.yml`, which the finding names, and record the others in `## Notes`. Alternatives: B — pin all seven. Why: §5; those rules predate the finding, and changing them touches §23.C/§23.H contracts and several commands. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] Which ref string does the helper send for `internal-review.yml`? — Picked: A — the bare default branch name from the REST API. Alternatives: B — `refs/heads/<default>`. Why: A is what `review_autofix_sweep.yml` sends since #4618 and what `check_in_status.py` matches on `head_branch`; B's run metadata is unverified. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- AD-4 follow-up: the six pre-existing `Bash(gh workflow run <file> *)` rules on `main` accept an unpinned `--ref` in the same way. A separate issue is the place to decide whether to pin them.

## References

- Issue #5375; audit tracker #3576; issue #4985 and its final PR #5031; issue #4618.
- CLAUDE.md §1, §5, §6, §15, §20, §23.C, §23.H, §23.I, §28.C.
