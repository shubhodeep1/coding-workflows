# Approve read-only `$(gh api …)` substitutions in `echo`, and name the approved shape when the `gh api` guard asks

Source issue: shubhodeep1/coding-workflows#4909 (https://github.com/shubhodeep1/coding-workflows/issues/4909)
Base branch: claude/implement-plan-issue-4786-guard-allow-read-loops
Security pass: run

## Summary

`.claude/hooks/gh_api_write_guard.py` asks on every `gh api` call inside a double-quoted `$(...)`, including pure reads such as `echo "$n: $(gh api repos/o/r/issues/$n --jq …)"`. An unattended session waited on that prompt on 2026-09-29. This plan approves one narrow shape: a double-quoted substitution that holds exactly one `gh api` GET/HEAD read, used as an `echo` argument at top level or inside a #4786-approved read loop. When the guard still asks because of a substitution or a loop, its reason names the approved equivalent, so the session can retry in that shape.

## Context

- Issue #4909 (owner-authored spec). It gives the two parts, the "still ask" list, the test cases, and the delivery constraints this plan follows.
- The incident command:
  `for n in 3576 3895 4638 4703 4811 4894; do echo "$n: $(gh api repos/shubhodeep1/coding-workflows/issues/$n --jq '[.labels[].name]|join(",")' 2>&1)"; done`
  It asked with "a gh api call could run hidden inside a $(...) …".
- Today on `main` (`gh_api_write_guard.py`, after #4704):
  - `substitution_bodies` (lines 345-417) returns the body of every double-quoted `$(...)` and every backtick substitution.
  - `has_hidden_gh_api` (lines 450-500) returns true when any such body mentions `gh api`.
  - `evaluate` (lines 788-793) then asks before classifying anything.
  - An unquoted `$(gh api …)` is split into its own segment by `shell_segments`, classified as a direct call, and gets no decision, because `_is_approvable_command` (lines 677-723) rejects `$` and `(`. See `NO_DECISION_COMMANDS`, `tests/test_gh_api_write_guard.py:279`.
- **Dependency: #4786.** #4786 adds the `for` loop frame (`_is_approvable_read_loop`, plan `docs/plans/issue-4786-guard-allow-read-loops-plan.md`). Its frame rule rejects any `$(`. #4909 must reuse that frame without duplicating its parser. So this project is built on #4786's project branch (AD-1). As of 2026-09-29, #4786 is blocked before its phase 1: no phase PR exists, and its project branch holds only its log.
- `.claude/hooks/**` is a protected path (CLAUDE.md §28.C). The issue directs the interim twin-first rule (operator Q40: A, `docs/operations/master-session.md`). The phase edits only `workflow-templates/.claude/hooks/gh_api_write_guard.py`. The supervising session copies it into `.claude/hooks/` with the operator's one-time approval (Q62/Q64, as for #4755).
- The twin files must stay byte-identical: `test_template_parity` (`tests/test_gh_api_write_guard.py`) for the hook, and `workflow-templates/CLAUDE.md` for `CLAUDE.md`.

## Goals

- G1: `evaluate` returns `allow` for the incident command, and for `echo "$(gh api repos/o/r/issues/1 --jq .title)"`.
- G2: `evaluate` still returns `ask` for each "still ask" case in the issue:
  - `-X POST` inside the substitution;
  - `$(gh api … | sh)`;
  - `$(gh api …; rm x)`;
  - a nested `$(`;
  - a backtick;
  - `$(gh api --input f …)`.
- G3: Every command outside the new shape gets the same decision and the same reason as before, apart from the extra hint text (G4). The existing test cases pass unchanged, and so do #4786's.
- G4: When the guard asks because of a hidden substitution, or because of a write in a command that holds a `for` loop, the reason names the approved equivalent shapes.
- G5: The guard still fails closed, issues no GitHub API calls (§15), and has no escape hatch. The hook and its twin, and `CLAUDE.md` and its twin, are byte-identical after the sync.
- G6: CLAUDE.md §23.H (the table's `write` row and the decision paragraph) and §23.D.4 describe the new shape. So do `agents.md` and the hook docstring.

## Non-goals

- Unquoted `$(gh api …)` and backtick substitutions (AD-4).
- Substitutions outside `echo` arguments: assignments, `git commit -m "$(…)"`, field values (AD-3).
- GraphQL queries inside a substitution (AD-5).
- Pipes, filters, or other commands inside the substitution, even safe ones such as `| head -1`. The issue says a pipe asks.
- Any change to #4786's loop frame rules, `.claude/settings.json`, the §23.B routine set, or how top-level calls are classified.
- The twin-first sync workflow (#4785).

## Constraints

- §1: security first. The change only turns today's `ask` into `allow` for the narrow shape. Every other input keeps today's full evaluation (AD-6).
- §5: minimal change. One rewrite step in `evaluate`, one predicate for the substitution body, and two hint strings. Existing functions keep their behaviour.
- §6: no identifier is renamed or removed. New names are checked for clashes against the module as it stands after #4786, at implementation time. Proposed names: `_READ_SUBSTITUTION_PLACEHOLDER`, `_read_substitution_call`, `_rewrite_echo_read_substitutions`, `_APPROVED_SHAPES_HINT`.
- §9: tabs in Python.
- §14: the hook reaches consumer repos through `workflow-templates/.claude/`.
- §15: no API calls. The single `git config` subprocess still runs only when a call could be routine.
- §20: one `changelog.d/` fragment (`changed`).
- §23.H: fail closed, no escape hatch, every write asks. It stays authoritative, so its text changes in the same PR, byte-for-byte in both `CLAUDE.md` copies.
- §28.C: protected path, delivered twin-first per Q40 (see Notes).

## Approach

Everything stays in the existing guard (AD-2), as a rewrite step in `evaluate` that runs before the hidden-call check.

1. **Find candidate substitutions.** Walk `stripped_command` with the same quote rules `substitution_bodies` uses. Record the start and end of every double-quoted `$(...)` whose body mentions `gh api`. If any backtick substitution mentions `gh api`, skip the rewrite (today's result).
2. **Vet each body** (`_read_substitution_call(body, loop_var)`), which returns the parsed call or `None`. It accepts the body only when all of these hold:
   - After removing one trailing ` 2>&1`, the body tokenizes, with the `shell_segments` rules, into exactly one segment and no separators. That rules out `|`, `;`, `&&`, `||`, `&`, newlines, and parentheses.
   - The segment's command word is exactly `gh`, followed by `api`. No prefix words or assignments are allowed.
   - The body holds no backtick, no nested `$(`, and no redirect other than that trailing `2>&1`. All of these are checked outside single quotes.
   - `parse_gh_api_args` accepts the arguments.
   - The method is `GET` or `HEAD`, the path is not `graphql`, and there is no `--input`.
   - No `-F` / `--field` value starts with `@`.
   - Every header matches `_SAFE_HEADER_RE`.
   - `$` outside single quotes appears only as `$<loop_var>` / `${<loop_var>}`, only in the endpoint, and only before any `?` (AD-7). At top level (`loop_var` is `None`), no `$` may appear at all.
3. **Rewrite.** If every candidate is accepted and the command does not already contain the placeholder text, replace each `$(...)` span with `_READ_SUBSTITUTION_PLACEHOLDER`, a fixed literal word. Keep each parsed call. The loop variable is taken from #4786's frame, `for VAR in …`, when the command matches it.
4. **Decide on the rewritten command.** Allow only when all of these hold:
   - every placeholder sits in an argument of a segment whose command word is exactly `echo` (not an assignment, not another command);
   - the rewritten command passes the existing hidden-call check, the existing write check, and either `_is_approvable_command` or #4786's `_is_approvable_read_loop`;
   - each placeholder counts as one `read` result, so the "at least one `gh api` call" requirement is met by the substitution.

   Otherwise, fall back to evaluating the **original** command exactly as today (AD-6). A read substitution in a context that is not approved therefore still asks as a hidden call.
5. **Hints (part 2).** `_APPROVED_SHAPES_HINT` is a fixed string (AD-8), for example:

   > Approved shapes: `for n in <literal IDs>; do echo $n; gh api repos/<o>/<r>/issues/$n --jq …; done`, or `echo "$(gh api <GET endpoint> --jq …)"` with one read call and nothing else in the `$(...)`.

   It is appended to the hidden-call ask reason, and to the write ask reason when the command matches #4786's loop frame (`_READ_LOOP_RE`) or holds `for … in`. Other ask reasons are unchanged.

Alternatives considered:
- A separate `PreToolUse` hook: a second settings entry and a copy of the parser (rejected).
- Treating a read substitution as a read wherever it appears: this would approve `X="$(gh api …)"; <anything>` shapes the guard has not read (rejected, §1).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes exactly one phase.

1. **Phase 1: read-only `echo` substitutions and approved-shape hints in the `gh api` guard.**
   - Protected paths: `.claude/hooks/gh_api_write_guard.py`, delivered twin-first per Q40.
   - **Precondition:** #4786's phase 1 has merged into `claude/implement-plan-issue-4786-guard-allow-read-loops`, so `_is_approvable_read_loop` exists on this project's base after the step 2 sync (or #4786 has merged into `main` and this project's base moved there). If not, the stage stops `BLOCKED` on #4909 before writing anything.
   - Scope: the rewrite step, the body predicate, the hints, the tests, the CLAUDE.md, `agents.md`, and docstring updates, and the changelog fragment.
   - Done when:
     - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_gh_api_write_guard.py tests/test_claude_md_section_numbers.py` passes against the twin (a scratch copy of the tests pointed at `TEMPLATE_GUARD_PATH`), and, after the `[claude-twin-sync]` commit, against `.claude/hooks/`;
     - `cmp` shows both `CLAUDE.md` copies identical;
     - the incident command through the hook process prints `permissionDecision: allow`;
     - `echo "$(gh api -X POST repos/o/r/issues/1/comments -f body=x)"` prints `ask`.
   - Delivery (Q40): push the phase PR with only the twin changed under `workflow-templates/.claude/hooks/`, post a `hold` claim (`claude_fix_claim.py post --kind hold`), and stop `BLOCKED` with an `ai:claude-blocked:v1` comment listing the twin file and its `sha256sum`. The supervising session reviews the hook diff with the operator, commits `[claude-twin-sync]`, pushes (which lifts the hold), and comments `/reclarify`.
   - Rollback: revert the PR. The guard returns to asking on every `$(gh api …)`.

## Implementation Steps

Phase 1 (after the precondition holds):

1. `workflow-templates/.claude/hooks/gh_api_write_guard.py`, as it stands after #4786:
   - Add `_READ_SUBSTITUTION_PLACEHOLDER`, `_read_substitution_call`, `_rewrite_echo_read_substitutions`, and `_APPROVED_SHAPES_HINT` next to `substitution_bodies` and `has_hidden_gh_api`.
   - In `evaluate`, run the rewrite before the hidden-call check. When it returns a rewritten command and placeholder results, run the existing steps on the rewritten command, and allow only per Approach step 4; otherwise use today's path on the original command.
   - Append the hint to the two ask reasons.
   - Extend the module docstring (the "write" item and the decision list).
2. `tests/test_gh_api_write_guard.py`: add the groups listed under Tests, and update the module docstring.
3. `CLAUDE.md`:
   - §23.H: the `write` row gets the exception ("…in a backtick or double-quoted `$(...)` substitution Bash would run, except one `gh api` GET/HEAD read as an `echo` argument at top level or in an approved read loop…"), and the decision paragraph names the shape.
   - §23.D.4: name the approved `echo "$(gh api …)"` shape beside #4786's loop wording.
   - Copy the file byte-for-byte to `workflow-templates/CLAUDE.md`.
4. `agents.md` (the guard paragraph, ≈ lines 1036-1052): name the approved substitution shape.
5. `changelog.d/4909-guard-echo-read-substitutions.md` (`<!-- changelog: changed -->`), following §20.D.
6. After the `[claude-twin-sync]` commit (by the supervising session): `.claude/hooks/gh_api_write_guard.py` is byte-identical to the twin.

## Files & Modules

- `workflow-templates/.claude/hooks/gh_api_write_guard.py`
- `.claude/hooks/gh_api_write_guard.py` (protected path; synced by the supervising session per Q40)
- `tests/test_gh_api_write_guard.py`
- `CLAUDE.md`
- `workflow-templates/CLAUDE.md`
- `agents.md`
- `changelog.d/4909-guard-echo-read-substitutions.md` [new]

## Tests

Unit tests in `tests/test_gh_api_write_guard.py`, run by the existing `ci.yml` step (`.github/workflows/ci.yml:421`):

- **`ECHO_READ_SUBSTITUTION_ALLOWED`** (expect `allow`):
  - the incident command;
  - `echo "$(gh api repos/o/r/issues/1 --jq .title)"`;
  - `cd /x && echo "a: $(gh api repos/o/r/pulls/2 --jq .state)"`;
  - two such substitutions in one `echo`;
  - a loop body `echo $n; gh api …/$n --jq …` beside `echo "$(gh api …/$n --jq …)"`;
  - `-X GET -f q=x` inside the substitution;
  - `-H 'Accept: application/vnd.github+json'` inside the substitution.
- **`ECHO_READ_SUBSTITUTION_ASK`** (expect `ask`):
  - `-X POST` inside;
  - `$(gh api … | sh)`;
  - `$(gh api …; rm x)`;
  - `$(gh api … && rm x)`;
  - a nested `$(`;
  - a backtick inside;
  - `$(gh api --input f …)`;
  - `-F q=@file`;
  - a `X-HTTP-Method-Override` header;
  - `2>/tmp/e` inside;
  - `$OTHER` inside;
  - `$n` after `?` inside the loop;
  - `$n` in `--jq` inside the loop;
  - `$n` at top level (no loop);
  - a GraphQL query inside;
  - `$(/usr/bin/gh api …)`;
  - `$(env gh api …)`;
  - `X="$(gh api repos/o/r)"`;
  - `git commit -m "$(gh api repos/o/r --jq .id)"`;
  - `echo "$(gh api repos/o/r)" > /tmp/x`;
  - `echo "$(gh api repos/o/r)"; python3 x.py`;
  - a command that already contains the placeholder text.
- **Unchanged:** `echo $(gh api repos/a/b --jq .id)` stays no decision (`NO_DECISION_COMMANDS`), and every existing `ASK_CALLS` substitution case still asks.
- **Hint:** the hidden-call ask reason and a write-in-loop ask reason contain the approved-shape text. A plain `-X DELETE` ask reason does not.
- **Hook process:** the incident command through `_run_hook` prints `permissionDecision: allow`.
- `test_template_parity` fails between the phase push and the `[claude-twin-sync]` commit. That is expected under Q40, and the hold covers it. `tests/test_claude_md_section_numbers.py` still passes, because nothing is renumbered.

## Risks & Mitigations

- A substitution that runs more than one command: the body must tokenize into exactly one `gh api` segment with no separators, pipes, nested substitutions, or backticks. Anything else falls back to today's ask.
- A read that is really a write (method override, file-backed field, `--input`, GraphQL): only a REST `GET` / `HEAD` with safe headers, no `@` field values, and no `--input` is accepted (AD-5).
- A placeholder collision letting user text pose as a vetted substitution: the rewrite is skipped when the command already contains the placeholder text.
- Output of the read landing somewhere harmful: the placeholder must be an `echo` argument in a command the existing approval checks accept, so no redirect other than `2>&1`, no assignment, and no executor.
- #4786's parser changes shape before it merges: this plan names #4786's functions from its plan. ACCEPTED: the phase adapts to the merged code, and the tests pin the behaviour.
- The project waits on #4786. ACCEPTED: the issue requires building on #4786's frame without duplicating it.

## Rollout

- No flag and no migration. The change applies to the next Bash call once it reaches `main`. Consumer repos get it on the next `@stable` sync (§14).
- This project's final PR merges into #4786's project branch while #4786 is open. If #4786 merges into `main` first, this project's base moves to `main` (`/implement-plan-claude` Issue Mode, "A base branch that merges moves the project").
- Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which branch is this project built on? — Picked: A — #4786's project branch `claude/implement-plan-issue-4786-guard-allow-read-loops`, with the phase gated on #4786's phase 1 having merged there. Alternatives: B — `main`, waiting for the whole #4786 project to merge; C — `main` now, with a copy of the loop frame. Why: the issue allows "on top of its branch" and forbids duplicating the parser (rules out C); A starts as soon as the frame exists, and the base-move rule carries the project to `main` if #4786 merges first. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the substitution logic live? — Picked: A — a rewrite step in the existing `gh_api_write_guard.py` `evaluate`, reusing `_is_approvable_command` and #4786's `_is_approvable_read_loop`. Alternatives: B — a separate `PreToolUse` hook. Why: one file and one settings entry, no duplicated parser (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What happens to a read substitution outside an `echo` argument (assignment, `git commit -m`, field value)? — Picked: A — today's result (ask, as a hidden call). Alternatives: B — no decision. Why: the issue allows it only inside `echo` arguments; §1. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Is an unquoted `$(gh api …)` approved too? — Picked: A — no; only the double-quoted form, per the issue. The unquoted form keeps today's no-decision. Alternatives: B — approve both. Why: an unquoted substitution word-splits and globs its output. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Which calls count as a read inside the substitution? — Picked: A — REST `GET` / `HEAD` only, no `--input`, no `-F …=@file`, only `Accept` / `X-GitHub-Api-Version` headers; GraphQL queries keep asking. Alternatives: B — also non-mutation GraphQL queries. Why: the issue names GET/HEAD; GraphQL is a POST and the proxy refuses it anyway. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] What happens when the rewritten command is not approvable (a file redirect, another command beside it)? — Picked: A — fall back to evaluating the original command exactly as today (ask, as a hidden call). Alternatives: B — no decision. Why: §1; the change only ever turns today's ask into allow for the approved shapes. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Where may `$` appear inside the substitution? — Picked: A — nowhere at top level; inside an approved loop only the loop variable, in the endpoint before any `?`, as #4786's AD-6. Alternatives: B — any `$VAR`. Why: §1; the value of any other variable is unknown. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] How is the approved equivalent named in the ask message? — Picked: A — a fixed hint string naming both shapes, appended to the hidden-call ask and to a write ask whose command holds a `for` loop. Alternatives: B — a hint rebuilt from the session's own command. Why: a fixed string cannot echo attacker-shaped text and is simple to test (§5). Applied in: phase 1 PR. Status: pending review

## Notes

- The security pass runs: `security_pass_skip.py --issue 4909` printed `{"skip": false, "label": null, "reason": "no skip label"}`.
- Protected-path approval: phase 1 — twin-first per Q40, as the issue's Constraints direct ("use the interim twin-first rule (operator Q40: A)"; owner-authored spec, 2026-09-29).

## References

- Issue #4909 (this change), #4786 (the loop frame; plan `docs/plans/issue-4786-guard-allow-read-loops-plan.md`, draft final PR #4796), #4704 (quote-aware substitutions, merged), #4785 (twin-first sync), #4755 (the previous hook sync).
- CLAUDE.md §23.D, §23.H, §28.C; `docs/operations/master-session.md` (Q40, Q62/Q64).
