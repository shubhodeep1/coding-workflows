# gh api guard: deny jq command-line options passed to `--jq` instead of prompting

Source issue: shubhodeep1/coding-workflows#4891 (https://github.com/shubhodeep1/coding-workflows/issues/4891)
Base branch: main
Security pass: run

## Summary

An unattended session stopped at a permission prompt for a read-only loop of `gh api` calls that passed jq's own command-line options to `gh api --jq` (`--jq --arg r "$r" '<expr>'`). `gh api` has no `--arg`: its `--jq` flag swallows `--arg` as the expression and `gh` rejects the call before sending any request, so the prompt guarded a command that could never work. This plan makes `.claude/hooks/gh_api_write_guard.py` return `deny` with a corrective reason for that mistake class, so the session fixes the command in the same turn instead of waiting for a human.

## Context

- #4891 (filed by `permission_prompts.py`, pattern `for * ; do * --jq --arg * ; done`, sig `a5be406f8c21`): one `PermissionRequest` at 2026-09-28T04:38:31Z in session `session_01Pqd1mbhdV8mCxriki9onge` (the #4707 implement session, ad hoc CI job-timing reads; no command file produced the call).
- Replaying the command through the guard gives `permissionDecision: ask`, reason `unreadable call (expected one endpoint, found 4)`: `parse_gh_api_args` (`.claude/hooks/gh_api_write_guard.py:503-553`) takes `--arg` as the `--jq` value and counts `r`, `"$r"`, and the jq program as extra endpoints; every unreadable call is a write (`evaluate`, lines 805-816), and a write asks.
- `gh` agrees the call is broken (gh 2.101.0, 2026-09-29): `gh api repos/<o>/<r> --jq -r .default_branch` fails with `accepts 1 arg(s), received 2`; `--jq -r` alone fails in jq (`function not defined: r/0`); `--jq '(-.size)'` works.
- The same loop written correctly (`for r in 1 2; do gh api "repos/.../runs/$r/jobs?per_page=50" --jq '.jobs[].name'; done`) gets no decision from the guard (loops are left to the Auto-mode classifier, §23.H), and a single plain read is allowed. The loop shape is not what prompted; the malformed call is.
- Precedent for a deny with a corrective reason instead of an ask: #4858 (inline-interpreter edits), whose issue text notes that a deny needs no human while an ask blocks an unattended session.
- #4912 (`for * ; do * ; done`, a `gh api` inside a double-quoted `$(...)`) is a different cause (the hidden-call rule) and is out of scope.
- `.claude/hooks/gh_api_write_guard.py` and `workflow-templates/.claude/hooks/gh_api_write_guard.py` are byte-identical (checked 2026-09-29), as are `CLAUDE.md` and `workflow-templates/CLAUDE.md`; `tests/test_gh_api_write_guard.py::test_template_parity` enforces the hook copy.

## Goals

- G1: A `gh api` call whose `-q`/`--jq` value is option-shaped (matches `^--?[A-Za-z]`, e.g. `--arg`, `-r`, `--raw-output`, `-c`), in any of the forms `--jq <v>`, `--jq=<v>`, `-q <v>`, `-q<v>`, makes the guard return `permissionDecision: deny` with a reason that names the mistake and the fix (put the value into the jq program, or pipe the output to `jq` with its own options).
- G2: The deny covers the whole Bash call and wins over `ask` and `allow`. The hidden-call `ask` (`has_hidden_gh_api`) and the unparseable-command `ask` are checked first and are unchanged.
- G3: Every other decision is unchanged: the existing test suite passes unmodified, and a jq program that starts with `-` but is not option-shaped (`-.size`, `-1`) is not denied.
- G4: The #4891 command is denied; the corrected loop still gets no decision.
- G5: The root and `workflow-templates/` copies of the hook, and of `CLAUDE.md`, stay byte-identical.

## Non-goals

- Denying other unreadable calls (wrong endpoint count from plain extra words, unknown flags, missing values): shell expansion and future `gh` flags make those uncertain, so they keep asking.
- `-t`/`--template` values.
- The loop shape and `$VAR` handling (left to the classifier, §23.H), and #4912's hidden-call case.
- An environment-variable kill switch (§23.H: the guard deliberately has none).
- `permission_prompts.py` / `permission_prompt_logger.py` changes, unless the phase finds that a hook deny raises a `PermissionRequest` or `PermissionDenied` event (AD-5).

## Constraints

- §1: security first. A deny runs nothing, so it is strictly narrower than today's ask; no permission is widened (the issue's "How to fix" forbids widening for anything but reads and routine writes).
- §5: the smallest change that turns this class into a self-correcting error.
- §6: no identifier renamed or removed. New names (`DECISION_DENY`, a helper such as `jq_cli_option`) are checked for clashes in the module and the tests.
- §9: tabs in Python; 2-space YAML.
- §14 / §20: mirror under `workflow-templates/.claude/`; one `changelog.d/` fragment.
- §15: the guard stays API-free.
- §23.H: fail closed stays; the documented outcome table gains the deny row.
- §28.C: phase 1 edits `.claude/**`, a protected path, and does not start without a recorded `Protected-path approval: phase 1` line.

## Approach

In `parse_gh_api_args`, when the flag's role is `jq`, check the value against `^--?[A-Za-z]`; if it matches, raise a new `Unreadable` subclass (for example `MalformedJq`) carrying the offending value. `evaluate` catches it before the generic `Unreadable` handler and records a `deny` result; after the loop, any deny makes the decision `deny` with a reason such as: `gh api guard (CLAUDE.md §23.H): gh api --jq takes a jq program, not jq's command-line options (got "--arg"). gh api has no --arg/-r: inline the value in the program, or pipe the output to jq with its own options. Nothing ran.` Alternatives considered: deny every unreadable call (rejected, AD-1 B); guidance text only (rejected, AD-1 C).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes exactly one phase.

1. **Phase 1 — deny jq CLI options passed to `--jq`.** protected paths: `.claude/hooks/gh_api_write_guard.py`.
   - Files: see Files & Modules.
   - Done: `tests/test_gh_api_write_guard.py` passes with the new cases; the #4891 command (replayed through the hook) prints `permissionDecision: deny`; the corrected loop prints nothing; `test_template_parity` holds; `CLAUDE.md` §23.H and `agents.md` describe the deny; the changelog fragment exists.
   - Rollback: revert the phase PR; malformed `--jq` calls ask again.

## Implementation Steps

1. `.claude/hooks/gh_api_write_guard.py`: add `DECISION_DENY = "deny"`, the option-shaped check on `jq` values in `parse_gh_api_args`, the deny path in `evaluate` (deny > ask > allow), and the docstring's outcome list. Delivery follows the answer to the protected-path question (for example the twin-first route: edit `workflow-templates/.claude/hooks/gh_api_write_guard.py` and list the twin → root copy).
2. Copy to the other hook twin so both are byte-identical.
3. `tests/test_gh_api_write_guard.py`: deny cases (`--jq --arg r "$r" '...'` inside a `for` loop — the #4891 command verbatim; `--jq -r .x`; `--jq=--raw-output`; `-q -c`; `-q-r`; a write call with `--jq -r`), no-deny cases (`--jq '-.size'`, `--jq '(-.size)'`, `--jq .a`), precedence (a hidden-call command still asks; deny beats ask when one call is a write and another is malformed), and the corrected loop returns no decision.
4. `CLAUDE.md` §23.H (and `workflow-templates/CLAUDE.md`): add the deny outcome to the class table and the decision paragraph.
5. `agents.md` permissions paragraph: one sentence on the deny.
6. `changelog.d/4891-gh-api-guard-deny-jq-cli-options.md` (`changed`).

## Files & Modules

- `.claude/hooks/gh_api_write_guard.py` (protected path)
- `workflow-templates/.claude/hooks/gh_api_write_guard.py`
- `tests/test_gh_api_write_guard.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `agents.md`
- `changelog.d/4891-gh-api-guard-deny-jq-cli-options.md` [new]

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_gh_api_write_guard.py` (its own `ci.yml` step), with the cases in step 3.
- Replay: the #4891 command and the corrected loop through the hook's stdin, as in the Context section.
- Regression: the existing cases in `tests/test_gh_api_write_guard.py` pass unchanged; `test_template_parity` holds.

## Risks & Mitigations

- A valid jq program that starts with `-` followed by a letter (negating a function call, `-length`) would be denied. Mitigation: the reason says to wrap it in parentheses (`(-length)`), which `gh` accepts; ACCEPTED — rare, and the deny is self-correcting.
- A hook deny might raise a `PermissionDenied` event and get filed as a new `ai:permission-prompt` issue. Mitigation: AD-5, verified in the phase.
- Consumer repos receive the hook on the next `@stable` sync (§14); ACCEPTED — the change only narrows behaviour.

## Rollout

No flag. The hook ships to consumers with the `.claude/` sync. Rollback is a revert of the phase PR.

## References

- #4891 (this issue), #4858 (deny-with-reason precedent), #4912 (sibling loop prompt, different cause)
- CLAUDE.md §23.D, §23.H, §23.I, §28.C

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the prompt on a malformed `gh api --jq` call be fixed? — Picked: A — the guard returns `deny` with a corrective reason for a `--jq`/`-q` value that is a jq command-line option. Alternatives: B — deny every unreadable call; C — CLAUDE.md §23.D guidance only. Why: deterministic, narrower than today, and fixes the observed shape; B would deny valid calls whose word count depends on shell expansion; C is ignored under load (#4858). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Which `--jq` values count as jq options? — Picked: A — values matching `^--?[A-Za-z]`. Alternatives: B — any value starting with `-`; C — an explicit list of jq's options. Why: catches every jq option, current and future, without denying valid programs such as `-.size` or `-1`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does the deny combine with other calls in the same Bash command? — Picked: A — deny the whole call; it wins over ask and allow, after the unchanged hidden-call and parse-failure asks. Alternatives: B — keep ask and only improve the reason text. Why: a hook decides once per tool call, a deny runs nothing, and B still stalls unattended sessions. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Add an environment-variable kill switch for the deny? — Picked: A — no. Alternatives: B — `CLAUDE_GH_API_GUARD_DENY=off`. Why: §23.H states the guard deliberately has no escape hatch, and the deny only narrows behaviour. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Do the permission-prompt logger or filer need changes? — Picked: A — no, unless the phase finds from the Claude Code hooks documentation that a `PreToolUse` deny raises `PermissionRequest` or `PermissionDenied`; then `permission_prompts.py` skips records whose reason starts with the guard's deny prefix, and the phase records a plan deviation. Alternatives: B — add the filer skip now. Why: §5. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Is #4891 a duplicate of an open fix issue? — Picked: A — no; implement it. Alternatives: B — treat it as a duplicate of #4912. Why: #4912 is a newer issue with a different cause (the hidden-call rule), and no open issue fixes malformed `--jq` calls. Applied in: no code change. Status: pending review

## Notes

- The session that implements this runs in Auto mode (`permission_mode: auto`).
