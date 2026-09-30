# Treat `gh api --help` as a read in the `gh api` permission guard

Source issue: shubhodeep1/coding-workflows#5413 (https://github.com/shubhodeep1/coding-workflows/issues/5413)
Base branch: main
Security pass: run

## Summary

An unattended session stopped at a permission prompt on
`gh api --help | grep -n -i -A3 "escape"; grep -rn … | head -20`, a pure
read. The prompt came from the `gh api` guard (CLAUDE.md §23.H): `--help` is
not in its flag table, so the call is "unreadable", classed as a write, and
the guard answers `ask`, which prompts in every permission mode. Teach the
guard that a help-only `gh api` call sends no request and is a read.

## Context

- Issue #5413 (filed by `.claude/scripts/permission_prompts.py`, CLAUDE.md
  §23.I) records one permission prompt at 2026-09-29T22:58:27Z in session
  `session_01EQuSfPUnhHK2fLTf1A3PoB`, signature `bf39f13b1865`.
- Reproduced: feeding the command to
  `.claude/hooks/gh_api_write_guard.py` prints `permissionDecision: ask` with
  `unreadable call (unknown gh api flag \`--help\`)`.
- `parse_gh_api_args` (`.claude/hooks/gh_api_write_guard.py:503-553`) raises
  `Unreadable` for any flag outside `_VALUE_FLAGS` / `_BOOL_FLAGS`
  (`:116-137`), and `evaluate` (`:769-820`) turns an unreadable call into a
  write, so the whole command asks.
- `gh` is a cobra CLI: `-h` / `--help` prints the command's usage and exits
  without running it, so no HTTP request is sent.
- No command file prescribes this call: it was an exploratory read. The
  issue's fix order, step 3, applies: for reads, extend the `gh api` guard.
- The guard has a byte-identical twin at
  `workflow-templates/.claude/hooks/gh_api_write_guard.py`, enforced by
  `tests/test_gh_api_write_guard.py::test_template_parity`. `CLAUDE.md` and
  `workflow-templates/CLAUDE.md` are identical too.

## Goals

- A `gh api` call whose only options are `-h` / `--help` (plus
  output-only flags such as `--jq`, `--paginate`, `--silent`) is classed
  `read`, so `gh api --help` alone, or piped into the safe filters, is
  allowed, and beside other commands gets no decision from the guard.
- A help flag combined with an endpoint, method, field, header, or
  `--input` stays unreadable, so the guard still asks.
- CLAUDE.md §23.H's `read` row names the help-only call, in both copies.
- Tests pin the new behaviour against the edited guard.

## Non-goals

- No new `permissions.allow` rule, no change to `.claude/settings.json`.
- No new safe filter (`grep` stays outside the approvable set: the observed
  command, after the fix, gets no guard decision and goes to the allow list
  or the Auto-mode classifier, like any other read beside unknown commands).
- No change to `permission_prompts.py` or the logger.

## Constraints

- §1: the change only moves a call that sends no request from `write` to
  `read`; every call that could send one keeps its current class (AD-2).
- §5: one parser branch, one classify branch, docs row, tests, fragment.
- §6: no identifier renamed or removed; the new parsed key `help` is local
  to the parser's dict.
- §9: Python uses tabs, as the file does.
- §20: a `changelog.d/` fragment (fewer prompts in unattended sessions).
- §28.C: the phase edits `.claude/hooks/gh_api_write_guard.py`, a protected
  path, so it runs under the interim twin-first default (edit only the
  `workflow-templates/.claude/` twin; the twin-sync blocker follows the
  phase PR).

## Approach

Extend `parse_gh_api_args` to recognise `-h` / `--help` before `--`. When
the call carries a help flag and no endpoint, method, field, header, or
input, return a parsed dict with `help: True`; when it carries a help flag
and any of those, raise `Unreadable` as today. `classify` returns
`read` with description `--help (usage only, no request)` for a help parse,
before any other check.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — help-only `gh api` calls are reads.** `protected paths:
   .claude/hooks/gh_api_write_guard.py`.
   - Files: `workflow-templates/.claude/hooks/gh_api_write_guard.py` (the
     twin; `.claude/hooks/gh_api_write_guard.py` gets the byte-identical
     copy at twin sync), `tests/test_gh_api_write_guard.py`, `CLAUDE.md`,
     `workflow-templates/CLAUDE.md`, a new
     `changelog.d/5413-gh-api-help-read.md`.
   - Done: the new tests pass against the twin; the rest of
     `tests/test_gh_api_write_guard.py` passes except `test_template_parity`
     (and any test that loads the live copy for the new cases), which turns
     green at the `[claude-twin-sync]` copy; the issue's command no longer
     yields `ask` from the twin.
   - Rollback: revert the PR.

## Implementation Steps

1. In the twin guard's `parse_gh_api_args`, add `_HELP_FLAGS = {"-h",
   "--help"}`; on such a token before `--` set `help = True` and continue.
   After the loop, if `help`: raise `Unreadable("\`--help\` combined with a
   request")` when any endpoint, method, field, header, or input was given;
   otherwise return `{"help": True, "method": "GET", "endpoints": [],
   "fields": [], "headers": [], "input": None}`.
2. In `classify`, return `(KIND_READ, "--help (usage only, no request)")`
   first when `parsed.get("help")`.
3. Tests in `tests/test_gh_api_write_guard.py`, loading the twin module:
   `gh api --help` and `gh api -h` allow; `gh api --help | head -20` allows;
   the issue's full command gets no decision (`None`); `gh api --help
   repos/a/b`, `gh api -X DELETE repos/a/b --help`, `gh api --help -f a=b`
   ask; `gh api -- --help` stays unreadable (asks).
4. CLAUDE.md and its twin, §23.H `read` row: append "; a help-only call
   (`gh api --help` / `-h`), which sends no request".
5. `changelog.d/5413-gh-api-help-read.md` (`fixed`).

## Files & Modules

- `workflow-templates/.claude/hooks/gh_api_write_guard.py`
- `.claude/hooks/gh_api_write_guard.py` (copied at twin sync)
- `tests/test_gh_api_write_guard.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `changelog.d/5413-gh-api-help-read.md` [new]

## Tests

`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_gh_api_write_guard.py -q`
(the `ci.yml` step that runs it). Before the twin sync only
`test_template_parity` is expected red.

## Risks

- A future `gh` release could make `--help` do something other than print
  usage. Mitigated by keeping help-with-a-request unreadable.
- A consumer repo gets the change on the next `@stable` `.claude/` sync; no
  wrapper change is needed.

## Rollout

Merges with the project; consumers receive it through the existing
`.claude/` sync. No scheduler wiring, no new script (§18), no DB work.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where should the fix for the `gh api --help` prompt live? — Picked: A — extend the `gh api` guard so a help-only call is a read. Alternatives: B — add a `permissions.allow` rule for `gh api --help*` (an allow rule cannot override the hook's `ask`, so it would not fix it); C — close as not planned (the prompt is not by design: the call is a read). Why: the guard's ask is the cause, and the issue's fix order names extending the guard for reads. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Should `--help` make a call a read when it also carries an endpoint, method, field, header, or `--input`? — Picked: A — no; only a help-only call is a read, and help combined with a request stays unreadable (asks). Alternatives: B — yes, any call with `--help` is a read because cobra never runs it. Why: §1 puts security first; A fixes the observed call without relying on `gh`'s help handling for calls that name a request. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should `grep` become a safe filter so the issue's full command is allowed by the guard? — Picked: A — no; the guard gives no decision for it and the allow list or the Auto-mode classifier decides. Alternatives: B — add `grep` to the safe filters. Why: §5 minimal change; `grep -r` reads files, which the safe filters deliberately exclude. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`.
