# Approve read-only `for` loops over literal IDs in the `gh api` guard

Source issue: shubhodeep1/coding-workflows#4786 (https://github.com/shubhodeep1/coding-workflows/issues/4786)
Base branch: main
Security pass: run

## Summary

`.claude/hooks/gh_api_write_guard.py` makes no decision on any command with a loop, so a `for` loop that only reads past runs (`for r in 1 2 3; do gh run view $r --json …; done`) falls through to the Auto-mode classifier, which prompts. The #4707 implement session waited about 3.5 hours on one such prompt on 2026-09-28. This plan teaches the guard to **allow** exactly one shape, a `for` loop over literal tokens whose body holds only reads, and changes nothing else it decides.

## Context

- Issue #4786, operator decision Q36: A (2026-09-28). It states the allowed shape, the "must keep" list, and the test cases. This plan follows it and adds only the tightenings listed under Auto-decisions.
- Today (`gh_api_write_guard.py:648-699`), `evaluate` returns no decision at once when the command has no `gh api` text (`_RAW_GH_API_RE`, line 88). So a loop whose body is `gh run view` never reaches the guard's logic. A loop with `gh api` reads reaches `_is_approvable_command` (lines 556-602), which rejects every `$` (`_has_unsafe_shell_syntax`, lines 506-542). Either way the result is no decision.
- A write inside a loop already asks: the segment `do gh api -X PATCH …/$n -f state=closed` is a direct invocation (`_SEGMENT_PREFIX_WORDS` includes `do`), and `classify` returns `write`. The test case is `ASK_CALLS["write in loop"]` (`tests/test_gh_api_write_guard.py:235`).
- `workflow-templates/.claude/hooks/gh_api_write_guard.py` must stay byte-identical (`test_template_parity`, `tests/test_gh_api_write_guard.py:549`). `workflow-templates/CLAUDE.md` is byte-identical to `CLAUDE.md` today.
- `.claude/**` is a protected path (CLAUDE.md §28.C). The twin-first sync workflow that would let an unattended session land this change (#4785) is still open. The issue's Delivery section says so: "Until the twin-sync workflow lands, the operator's watched session applies it."

## Goals

- G1: `evaluate` returns `allow` for a command that is exactly `for VAR in TOKEN…; do BODY; done`, where every TOKEN matches `^[A-Za-z0-9._-]+$` and every BODY item is an allowed read (see Approach).
- G2: Every command outside that shape gets the same decision as today (no decision, or ask when a write is present). The existing parametrized cases in `tests/test_gh_api_write_guard.py` all still pass unchanged, and `ASK_CALLS["write in loop"]` still asks.
- G3: The guard still fails closed on an unreadable, invalid, or non-object payload and on an internal error. It issues no GitHub API calls (§15). It has no environment-variable escape hatch.
- G4: The `.claude/hooks/` file and its `workflow-templates/` twin are byte-identical. So are `CLAUDE.md` and `workflow-templates/CLAUDE.md`.
- G5: CLAUDE.md §23.H (table and decision paragraph) and §23.D.4 describe the new allow shape. `agents.md` and the hook docstring match them.

## Non-goals

- `while` / `until` loops, nested loops, loops beside other commands (`echo x; for …`), and loops over `$(...)` or globs. All of them keep today's result.
- Any other `gh` subcommand in a loop body (`gh issue view`, `gh pr list`, `gh workflow view`, …).
- Changes to `.claude/settings.json` allow rules, to the §23.B routine set, or to how single calls are classified.
- The twin-first sync workflow (#4785).

## Constraints

- §1: security first. Every new case is an **allow**, so each is narrower than the issue's wording where the wider form could run code the guard has not read (AD-3, AD-4).
- §5: minimal change. One new predicate plus a widened fast-path check in `evaluate`. The existing functions keep their behaviour.
- §6: no identifier is renamed or removed. The new names (`_READ_LOOP_RE`, `_LOOP_VAR_RE`, `_LOOP_TOKEN_RE`, `_GH_READ_SUBCOMMAND_FLAGS`, `_is_approvable_read_loop`) were checked against the module and are unused there.
- §9: tabs in Python. Markdown keeps its existing layout.
- §14: the hook reaches consumer repos through `workflow-templates/.claude/`.
- §15: no API calls. The one `git config` subprocess still runs only when a call could be routine.
- §20: one `changelog.d/` fragment (`changed`).
- §23.H: the guard fails closed, has no escape hatch, and still asks on every write.
- §28.C: the phase edits `.claude/hooks/**`, so it runs only under a recorded `Protected-path approval:` line.

## Approach

Add one predicate, `_is_approvable_read_loop(command, results)`, to the existing guard (AD-1). Call it from `evaluate`:

1. **Fast path.** Keep the current early return, but skip it when the command matches `_READ_LOOP_RE`: `^\s*for\s+\S+\s+in\s.*;\s*do\s.*;\s*done\s*$`, whose body names `gh run view`, `gh run list`, or `gh pr view`. Commands with no `gh api` text and no such loop still return at once.
2. **Writes first.** The existing hidden-call check and the per-invocation `classify` loop run unchanged, so any write in the body (including `gh api -X DELETE …/$r`) still asks before the loop predicate is consulted.
3. **Allow.** After the writes check, change `if invocations and _is_approvable_command(command)` to also allow when `_is_approvable_read_loop(command, results)` is true, where every `gh api` result must be `read` (a routine write in a loop is not approved; AD-5).

`_is_approvable_read_loop` accepts the whole command only when all of these hold:

- **Frame.** After removing each ` 2>&1` (`_REDIRECT_TO_STDERR_RE`), the command is exactly `for VAR in TOKEN [TOKEN …]; do BODY; done`, split on `;` (AD-2). It holds one `for`, one `do`, and one `done` keyword, and no newline, `(`, `)`, `<`, `>`, `||`, `&` (other than `&&`), backtick, `$(`, glob character, brace expansion, or `~` outside single quotes.
- **VAR** matches `^[a-z_][a-z0-9_]*$` (AD-3).
- **TOKENs.** At least one, each matching `^[A-Za-z0-9._-]+$` and not starting with `-` (AD-9).
- **`$` outside single quotes** appears only as `$VAR` or `${VAR}`, and only in the positions allowed below. Double quotes around such a word are accepted (AD-4).
- **BODY** is one or more items joined by `;` or `&&`. Each item is a pipeline whose head is one of the following, optionally piped into `_is_safe_filter` filters:
  - `gh api …`, which the existing `classify` marks `read`. `$VAR` / `${VAR}` may appear only in the endpoint argument, before any `?` (AD-6), never in a flag value or field.
  - `gh run view`, `gh run list`, or `gh pr view`, with every flag in that subcommand's allowlist (`_GH_READ_SUBCOMMAND_FLAGS`, AD-7). `$VAR` / `${VAR}` may appear only as a positional argument.
  - `echo` with literal words and `$VAR` / `${VAR}` only (AD-8).
- The command word is exactly `gh` (not a path) or `echo`. No other command, no nested `for` / `while` / `until`, and no `cd`, `sleep`, or `true` in the body.

The flag allowlists (value flags take the next token, which must be a literal, i.e. without `$`):

| Subcommand | Boolean flags | Value flags |
|---|---|---|
| `gh run view` | `--log`, `--log-failed`, `-v`, `--verbose`, `--exit-status` | `--json`, `-q`, `--jq`, `-t`, `--template`, `-j`, `--job`, `-a`, `--attempt`, `-R`, `--repo` |
| `gh run list` | `-a`, `--all` | `-L`, `--limit`, `-w`, `--workflow`, `-b`, `--branch`, `-u`, `--user`, `-e`, `--event`, `-s`, `--status`, `-c`, `--commit`, `--created`, `--json`, `-q`, `--jq`, `-t`, `--template`, `-R`, `--repo` |
| `gh pr view` | `-c`, `--comments` | `--json`, `-q`, `--jq`, `-t`, `--template`, `-R`, `--repo` |

`--flag=value` forms are split as `parse_gh_api_args` does. Any flag not listed (`--web` / `-w` on `gh run view` and `gh pr view` included) makes the loop not approvable: no decision, today's result.

Alternatives considered: a separate hook file (a second `PreToolUse` entry in `settings.json`, more wiring, same logic); substituting a placeholder for `$VAR` and reusing `_is_approvable_command` (it cannot express "`$VAR` only in the endpoint" or approve `gh run view`).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes exactly one phase.

1. **Phase 1: read-only loop approval in the `gh api` guard.** Protected paths: `.claude/hooks/gh_api_write_guard.py`.
   - Scope: the predicate and `evaluate` change, their byte-identical twin, the tests, the CLAUDE.md, agents.md, and docstring updates, and the changelog fragment.
   - Files: see Files & Modules.
   - Done when:
     - `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_gh_api_write_guard.py tests/test_claude_md_section_numbers.py` passes;
     - `cmp` shows both twins identical;
     - the incident command from #4786 evaluates to `allow` when piped through the hook process;
     - `for r in 1 2; do gh api -X DELETE repos/shubhodeep1/coding-workflows/issues/$r; done` evaluates to `ask`.
   - Rollback: revert the PR. The guard returns to "no decision on every loop", which prompts exactly as before.

## Implementation Steps

Phase 1:

1. `.claude/hooks/gh_api_write_guard.py`:
   - add `_READ_LOOP_RE`, `_LOOP_VAR_RE`, `_LOOP_TOKEN_RE`, `_GH_READ_SUBCOMMAND_FLAGS` and `_is_approvable_read_loop` next to `_is_approvable_command` (≈ lines 545-602);
   - widen the fast path and the allow condition in `evaluate` (≈ lines 658 and 696) as described in Approach;
   - extend the module docstring's decision list (lines 39-50) with the loop shape.
2. Copy the file byte-for-byte to `workflow-templates/.claude/hooks/gh_api_write_guard.py`.
3. `tests/test_gh_api_write_guard.py`: add the parametrized groups listed under Tests, and update the module docstring's item 2.
4. `CLAUDE.md` §23.H:
   - add a table row or sentence for the loop allow shape;
   - in the decision paragraph, replace "no `$`, backticks, globs, subshells, or loops" with the same rule plus the loop exception.

   §23.D.4: "Keep `gh api` calls out of loops" becomes "a `for` loop over literal IDs whose body holds only these reads is approved; keep other `gh api` calls out of loops". Copy the file byte-for-byte to `workflow-templates/CLAUDE.md`.
5. `agents.md` (≈ lines 1027-1042): name the approved read-loop shape beside the safe helpers.
6. `changelog.d/4786-guard-read-only-for-loops.md` (`<!-- changelog: changed -->`), following §20.D.

## Files & Modules

- `.claude/hooks/gh_api_write_guard.py` (protected path)
- `workflow-templates/.claude/hooks/gh_api_write_guard.py`
- `tests/test_gh_api_write_guard.py`
- `CLAUDE.md`
- `workflow-templates/CLAUDE.md`
- `agents.md`
- `changelog.d/4786-guard-read-only-for-loops.md` [new]

## Tests

Unit tests (all in `tests/test_gh_api_write_guard.py`, run by the existing `ci.yml` step at line 421):

- **`READ_LOOP_ALLOWED`** (expect `allow`):
  - the incident shape `for r in 36242690892 36224773465 36205375333 36078283644 35966436009; do gh run view $r --json createdAt,updatedAt,conclusion; done`;
  - `gh api repos/o/r/actions/runs/$r/jobs --jq '.jobs[].name'`;
  - `${r}` in the endpoint;
  - two reads joined by `&&`;
  - a read piped into `head -5`;
  - `gh run list -R o/r -L 5 --json databaseId` with `echo $r` before it;
  - `gh pr view $r --json state`;
  - a body ending in ` 2>&1`.
- **`READ_LOOP_NO_DECISION`** (expect `None`):
  - tokens containing `$`, `*` or quotes, or starting with `-` (`--web`);
  - `$VAR` in a flag value (`--jq $r`, `-R $r`) or a field (`-f q=$r`);
  - `$VAR` after `?` in the endpoint;
  - a file redirect (`> /tmp/x`, `2>/tmp/e`);
  - a `python3 -c` item in the body;
  - a nested loop;
  - a `while` loop;
  - `gh run view --web $r` and `gh pr view -w $r`;
  - an uppercase loop variable (`for PATH in .; do …`);
  - `gh issue view $r`;
  - a newline-separated loop;
  - a loop preceded by `echo x;`;
  - `/usr/bin/gh run view $r`;
  - `$(…)` in the token list;
  - `$OTHER` in the body;
  - a routine write in the body.
- **`READ_LOOP_ASK`** (expect `ask`):
  - `gh api -X PATCH …/$r -f state=closed` (existing `write in loop`);
  - `gh api -X DELETE repos/o/r/issues/$r`;
  - a POST in the body beside a read.
- Hook-process test: the incident command through `_run_hook` prints `permissionDecision: allow`.
- Existing tests stay unchanged, `test_template_parity` included. `tests/test_claude_md_section_numbers.py` still passes (no renumbering).

## Risks & Mitigations

- A loop variable that changes how `gh` runs (`PATH`, `IFS`, `GH_HOST`, `GH_TOKEN`): the variable must be lowercase (AD-3).
- `$VAR` expanding to something unexpected: tokens are literal `[A-Za-z0-9._-]+` and never start with `-`, so the value cannot word-split, glob, carry `/` or `?`, or turn a positional argument into a flag such as `--web` (AD-9). `$VAR` is only allowed as a positional argument or inside the endpoint path, so the worst case is a `gh` read with an odd argument.
- A flag on the read subcommands that writes or opens a browser: flags are allowlisted per subcommand, and unknown flags get no decision (AD-7).
- The guard is the only thing where the old ask rules were. Every change is an allow for a narrow shape. A bug in the predicate that returns true too often is covered by the no-decision and ask test groups above. ACCEPTED: the residual risk is limited to read-only `gh` subcommands.
- The phase needs a watched session (protected path). ACCEPTED: this is the operator-stated delivery route until #4785 lands.

## Rollout

- No flag and no migration. The change applies to the next Bash call after merge, in this repo and, on the next `@stable` sync, in consumer repos (§14).
- Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Where does the loop logic live? — Picked: A — a new predicate in the existing `gh_api_write_guard.py`, called from `evaluate`. Alternatives: B — a separate `PreToolUse` hook. Why: one file, one settings entry, and the loop path reuses `classify` and `_is_safe_filter` directly (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which separators may frame the loop? — Picked: A — `;` only, the exact shape in the issue. Alternatives: B — also newlines. Why: the issue says "exactly this shape, and nothing wider"; newline forms keep today's result. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which loop variable names are accepted? — Picked: A — lowercase names only (`^[a-z_][a-z0-9_]*$`). Alternatives: B — any plain shell name, as the issue words it. Why: §1. `for PATH in .; do gh …` would run `./gh`, and `GH_HOST` / `GH_TOKEN` change what `gh` talks to; uppercase names are where such variables live. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Is a double-quoted `"$VAR"` / `"${VAR}"` accepted? — Picked: A — yes, in the same positions as the unquoted form. Alternatives: B — unquoted only. Why: with literal tokens both expand identically, and models quote by habit. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Is a §23.B routine write in a loop body approved? — Picked: A — no: the body must hold reads only, so a routine write keeps today's result (no decision). Alternatives: B — approve routine writes too. Why: the issue allows reads only. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Where in a `gh api` endpoint may `$VAR` appear? — Picked: A — in the path only, before any `?`. Alternatives: B — anywhere in the endpoint argument. Why: the issue says "inside the endpoint path". Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-28] How are `gh run view` / `gh run list` / `gh pr view` flags vetted? — Picked: A — a per-subcommand allowlist; unknown flags make the loop not approvable. Alternatives: B — a denylist of `--web` / `-w`. Why: §1; `-w` means `--web` on two subcommands and `--workflow` on the third, and an allowlist cannot miss a new writing flag. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-28] What may `echo` print in the body? — Picked: A — literal words (quoted literals included) and `$VAR` / `${VAR}`. Alternatives: B — `$VAR` only. Why: matches "`echo <literal or $VAR>`" in the issue. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-28] May a loop token start with `-`? — Picked: A — no. Alternatives: B — yes, as the issue's regex `^[A-Za-z0-9._-]+$` alone allows. Why: §1; `for r in --web; do gh pr view $r; done` would turn a positional into a flag the allowlist rejects elsewhere. Applied in: phase 1 PR. Status: pending review

## Notes

- The security pass runs: `security_pass_skip.py --issue 4786` printed `{"skip": false, "label": null, "reason": "no skip label"}`.

## References

- Issue #4786 (this change), #4707 (the incident session), #4785 (twin-first sync, which would remove the watched-session requirement).
- CLAUDE.md §23.D, §23.H, §28.C.
