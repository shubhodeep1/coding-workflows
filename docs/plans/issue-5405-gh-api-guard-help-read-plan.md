# gh api guard: treat a bare `gh api --help` / `gh api -h` as a read

Source issue: shubhodeep1/coding-workflows#5405 (https://github.com/shubhodeep1/coding-workflows/issues/5405)
Base branch: main
Security pass: run

## Summary

The `gh api` permission guard (`gh_api_write_guard.py`, CLAUDE.md §23.H) forces a permission prompt on `gh api --help`, because its flag parser does not know `--help` and classes the call as unreadable. This plan classes a `gh api` call whose arguments are exactly `--help` or exactly `-h` as a **read**, and changes nothing else, so unattended stages stop prompting on a command that only prints usage text.

## Automation & Wiring (§18.E)

- **Scripts:** no new script. The change extends the existing `PreToolUse` hook `workflow-templates/.claude/hooks/gh_api_write_guard.py` (the twin of `.claude/hooks/gh_api_write_guard.py`).
- **Scheduler / PR-push entry point:** none new. The hook is already wired in `.claude/settings.json` (`PreToolUse`, matcher `Bash`), and `tests/test_gh_api_write_guard.py` already runs in its own `ci.yml` step. Consumer repos get the hook through the existing `.claude/` sync.
- **Supervisor (§18.C):** none needed.
- **DB work (§18.D):** none.
- **Removal registry (§18.F):** no entry; nothing single-use or long-running is added.

## Context

- The issue observed the prompt on 2026-09-30 in the #5093 conformance 2/3 stage. The command `gh api --help | grep -n -i -A3 "escape"; grep -rn …` got `gh api guard (CLAUDE.md §23.H): not a read or a §23.B routine write: unreadable call (unknown gh api flag \`--help\`).`
- Root cause: `parse_gh_api_args` (`gh_api_write_guard.py:503-553`) raises `Unreadable` for any flag outside `_VALUE_FLAGS` / `_BOOL_FLAGS`; `evaluate` (`:805-810`) turns `Unreadable` into `KIND_WRITE`, and any write makes the whole call `ask` (`:812-816`).
- `gh api --help` and `gh api -h` print usage and never contact GitHub, so the prompt protects nothing.
- Operator decision (issue body, master session Q1: A): make the smallest loosening that removes this prompt. The other prompt patterns stay on their own issues (#4786, #4891, #4909, #4858).
- #5174 (guard differential check, `scripts/guard_differential.py`, final PR #5185) is **not** on `main` yet. Its issue says an intended loosening is declared in an `Intended loosening:` section of the PR body.

## Goals

- G1: `gh api --help` and `gh api -h`, standing alone, are classed **read** and the hook answers **allow** (one simple read, per the existing whole-command rule).
- G2: Any other `gh api` argument list that contains `--help` or `-h` (an endpoint, `-X`, `-f`, `--input`, `--`, a second `--help`) is still **unreadable → ask**.
- G3: The hidden-call checks (substitutions, executors, heredocs) are unchanged and still win: `bash -c 'gh api --help'` and `echo "$(gh api --help)"` still ask.
- G4: `gh api --help | grep x` gets **no decision** (not allow): `grep` is not on the §23.H safe-helper list, and the list is not widened.
- G5: CLAUDE.md §23.H's "read" row, the hook docstring, and the README.md / agents.md guard notes describe the new read case; one `changelog.d/` fragment (`changed`) records it.

## Non-goals

- Treating `--help` combined with anything else as a read, or relying on `gh` ignoring the other arguments.
- Widening the safe-helper list (`grep` stays off it).
- Any other prompt pattern (#4786, #4891, #4909, #4858), or `gh <other command> --help`.
- Editing `.claude/**` directly (twin-first, see Constraints).
- Adding `scripts/guard_differential.py` or its corpus (#5174 owns them).

## Constraints

- **Protected paths (CLAUDE.md §28.C, operator rule Q40):** twin-first. Edit only `workflow-templates/.claude/hooks/gh_api_write_guard.py`; the `.claude/hooks/` copy arrives in a `[claude-twin-sync]` commit by the supervising session. `test_template_parity` stays red until that sync.
- **§1 security first:** the loosening is exact-match only; every existing ask path is kept. The guard stays fail-closed.
- **§5 minimal change:** one constant and one short branch in `evaluate`; `parse_gh_api_args` is untouched, so its `Unreadable` for every other `--help` / `-h` combination is preserved.
- **§6 naming:** no identifier is renamed or removed. The new constant name (`_HELP_ONLY_GH_API_ARGS`) is checked unique in the module; the new test module handle (`template_guard`) is checked unique in the test file.
- **§15:** the hook still issues no GitHub API calls.
- **§20:** one fragment, `changelog.d/5405-gh-api-guard-help-read.md`, section `changed`.
- **§23.H:** the "read" row of the class table changes, so CLAUDE.md and its twin `workflow-templates/CLAUDE.md` change together (they are byte-identical today).

## Approach

In `evaluate`, before calling `parse_gh_api_args`, check whether the invocation's argument list is exactly `["--help"]` or exactly `["-h"]` (compared as a tuple against a new module constant `_HELP_ONLY_GH_API_ARGS = (("--help",), ("-h",))`). If so, append `(KIND_READ, "gh api <flag> (usage text only)")` and continue. Every other argument list goes through `parse_gh_api_args` as today.

Because the result is a plain read, the whole-command decision needs no change: `_is_approvable_command` already allows a lone `gh api …` item (optionally piped into the safe filters) and returns False for anything piped into `grep`, which yields no decision. Hidden-call detection runs before classification and is untouched.

Alternatives considered:
- Teach `parse_gh_api_args` a `--help` flag: rejected, because it would have to special-case "no endpoint" and risks accepting `--help` beside other arguments.
- Add `--help` to `_BOOL_FLAGS`: rejected, it would make `gh api -X DELETE repos/o/r --help` a readable DELETE (an ask), but also `gh api --help repos/o/r` a GET read, which the issue forbids.

## Phases & Merge Strategy

This plan has exactly **one** phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — bare `gh api --help` / `-h` is a read.**
   - Files: `workflow-templates/.claude/hooks/gh_api_write_guard.py`, `tests/test_gh_api_write_guard.py`, `CLAUDE.md`, `workflow-templates/CLAUDE.md`, `agents.md`, `README.md`, `changelog.d/5405-gh-api-guard-help-read.md` [new].
   - Protected paths: `.claude/hooks/gh_api_write_guard.py` (reached through its twin only; twin sync required).
   - Done when: the new tests pass against the twin, every other test in `tests/test_gh_api_write_guard.py` passes except `test_template_parity` (red until the twin sync, by design), and the docs and fragment are in place.
   - Rollback: revert the phase PR; the guard returns to asking on `gh api --help`.

## Implementation Steps

### Phase 1

1. `workflow-templates/.claude/hooks/gh_api_write_guard.py`: add `_HELP_ONLY_GH_API_ARGS` next to the flag tables with a comment citing issue #5405; in `evaluate`'s classification loop, class an exact match as `KIND_READ` before `parse_gh_api_args`; update the module docstring's `read` entry.
2. `tests/test_gh_api_write_guard.py`: load the twin as a second module (`template_guard`, with the same local-slug fixture) and add:
   - bare `gh api --help` and `gh api -h` → allow, with a reason naming the read;
   - `--help` / `-h` with an endpoint, `-X`, `-f`, `--input`, `--`, or twice → ask;
   - `gh api --help | grep x` → no decision;
   - `gh api -h | head -5` and `gh api --help 2>&1` → allow (safe helpers unchanged);
   - `bash -c 'gh api --help'` and `echo "$(gh api --help)"` → ask (hidden-call checks still win).
3. `CLAUDE.md` and `workflow-templates/CLAUDE.md` §23.H: extend the `read` row with the bare `--help` / `-h` case.
4. `agents.md` (guard note near line 1090) and `README.md` (hook note near line 1041): one clause each naming the new read case.
5. `changelog.d/5405-gh-api-guard-help-read.md` [new]: `<!-- changelog: changed -->` entry per §20.D.

## Files & Modules

- `workflow-templates/.claude/hooks/gh_api_write_guard.py` (edit)
- `.claude/hooks/gh_api_write_guard.py` (twin sync only, not edited by this project)
- `tests/test_gh_api_write_guard.py` (edit)
- `CLAUDE.md`, `workflow-templates/CLAUDE.md` (edit, kept identical)
- `agents.md`, `README.md` (edit)
- `changelog.d/5405-gh-api-guard-help-read.md` [new]

## Tests

- Unit: the new cases in `tests/test_gh_api_write_guard.py` run against the twin module (`template_guard`), so they pass before the twin sync. After the sync they would pass against `guard` too, and `test_template_parity` turns green.
- Regression: the full existing `tests/test_gh_api_write_guard.py` suite (against `.claude/`, unchanged) keeps passing; `test_template_parity` is the one expected red until the sync.
- Doc parity: whatever tests compare `CLAUDE.md` with `workflow-templates/CLAUDE.md` keep passing (both edited identically).

## Risks & Mitigations

- A future `gh` release could make `--help` do something else → ACCEPTED: `--help` is the cobra convention across every `gh` command and prints usage only.
- A loosening slips past the #5174 differential check once it lands → the phase and final PR bodies carry an `Intended loosening:` section naming `gh api --help` and `gh api -h` with the reason, as #5174 specifies.
- The twin sync is a manual step until #4785 → the chain posts the twin-sync blocker and a `hold` claim, per the interim default; the project waits on it.

## Rollout

Ships with the next `@stable` release through the existing `.claude/` sync. No flag, no migration. Rollback is a revert of the phase PR (or the final PR).

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where should the bare-help check live? — Picked: A — a small exact-match branch in `evaluate` before `parse_gh_api_args`. Alternatives: B — special-case `--help` inside `parse_gh_api_args`; C — add `--help`/`-h` to `_BOOL_FLAGS`. Why: A leaves the parser, and so every other `--help` combination's `Unreadable`, unchanged (§5); C would make `gh api --help repos/o/r` a read, which the issue forbids. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How do the new tests exercise the behaviour before the twin sync? — Picked: A — load the twin (`workflow-templates/.claude/hooks/gh_api_write_guard.py`) as a second module and run the new cases against it. Alternatives: B — run them against `.claude/hooks/` (red until the sync); C — parametrize over both copies. Why: the interim twin-first rule says tests of new `.claude/` behaviour read the twin so they pass before the sync. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Where is the loosening declared while #5174's `scripts/guard_differential.py` is not on the base branch? — Picked: A — an `Intended loosening:` section in the phase PR body and the final PR body (the format #5174 names), plus this plan's Notes. Alternatives: B — hold the project until #5174 merges; C — add a corpus file ahead of #5174. Why: A records the reason where #5174 will look without creating files #5174 owns (§5). Applied in: phase 1 PR and final PR bodies. Status: pending review
- AD-4 [plan, 2026-09-30] Which doc copies change? — Picked: A — `CLAUDE.md` and its byte-identical twin `workflow-templates/CLAUDE.md`, plus `agents.md` and `README.md`. Alternatives: B — root `CLAUDE.md` only. Why: the two CLAUDE.md copies are identical today and consumer repos receive the twin; B would let them drift. Applied in: phase 1 PR. Status: pending review

## Notes

- The issue's example command (`gh api --help | grep … ; grep …`) gets **no decision** after this change, not allow. That is intended: the Auto-mode classifier decides it, and the safe-helper list is not widened.
- #5174's guard differential check is not on `main` (checked 2026-09-30: no `scripts/guard_differential.py`), so there is nothing in the tree to register the loosening in; see AD-3.

## References

- Issue #5405; incident session https://claude.ai/code/session_01EQuSfPUnhHK2fLTf1A3PoB (#5093 conformance 2/3)
- CLAUDE.md §23.H, §28.C; issue #5174 / PR #5185 (guard differential check); #4785 (twin sync)
- Related prompt issues: #4786, #4891, #4909, #4858
