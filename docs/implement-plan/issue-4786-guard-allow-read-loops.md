# Implement-Plan Log — Approve read-only `for` loops over literal IDs in the `gh api` guard

- Plan: docs/plans/issue-4786-guard-allow-read-loops-plan.md
- Source issue: shubhodeep1/coding-workflows#4786 (https://github.com/shubhodeep1/coding-workflows/issues/4786)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4786-guard-allow-read-loops   Final PR: #4796 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: [claude-twin-sync] of `.claude/hooks/gh_api_write_guard.py` on the phase 1 PR (operator's supervising session)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (blocked before the first wait; no checker armed)
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first (Protected-path approval A): the guard change is in `workflow-templates/.claude/hooks/gh_api_write_guard.py` only, tests pass against the twin (227 passed), and the root `.claude/` copy waits for the operator's `[claude-twin-sync]`. The phase PR carries a hold claim; twin-parity and the new root-guard tests fail until the sync, as expected.

## Phases
1. [ ] Phase 1 — read-only loop approval in the `gh api` guard — protected paths: `.claude/hooks/gh_api_write_guard.py`
   - [ ] `_is_approvable_read_loop` and the widened fast path / allow condition in `.claude/hooks/gh_api_write_guard.py` (plus docstring)
   - [ ] byte-identical twin `workflow-templates/.claude/hooks/gh_api_write_guard.py`
   - [ ] `tests/test_gh_api_write_guard.py`: `READ_LOOP_ALLOWED`, `READ_LOOP_NO_DECISION`, `READ_LOOP_ASK`, hook-process case
   - [ ] CLAUDE.md §23.H table and paragraph, and §23.D.4 wording, mirrored byte-for-byte to `workflow-templates/CLAUDE.md`
   - [ ] `agents.md` guard paragraph
   - [ ] `changelog.d/4786-guard-read-only-for-loops.md`
   - Done when the pytest run in the plan passes, both twins `cmp` identical, the incident command → `allow`, and a `-X DELETE` loop → `ask`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Where does the loop logic live? — Picked: A — a new predicate in the existing `gh_api_write_guard.py`, called from `evaluate`. Alternatives: B — a separate `PreToolUse` hook. Why: one file, one settings entry, and the loop path reuses `classify` and `_is_safe_filter` directly (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which separators may frame the loop? — Picked: A — `;` only, the exact shape in the issue. Alternatives: B — also newlines. Why: the issue says "exactly this shape, and nothing wider"; newline forms keep today's result. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which loop variable names are accepted? — Picked: A — lowercase names only (`^[a-z_][a-z0-9_]*$`). Alternatives: B — any plain shell name, as the issue words it. Why: §1. `for PATH in .; do gh …` would run `./gh`, and `GH_HOST` / `GH_TOKEN` change what `gh` talks to. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Is a double-quoted `"$VAR"` / `"${VAR}"` accepted? — Picked: A — yes, in the same positions as the unquoted form. Alternatives: B — unquoted only. Why: with literal tokens both expand identically. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Is a §23.B routine write in a loop body approved? — Picked: A — no, reads only; a routine write keeps today's result (no decision). Alternatives: B — approve routine writes too. Why: the issue allows reads only. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Where in a `gh api` endpoint may `$VAR` appear? — Picked: A — in the path only, before any `?`. Alternatives: B — anywhere in the endpoint argument. Why: the issue says "inside the endpoint path". Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-28] How are `gh run view` / `gh run list` / `gh pr view` flags vetted? — Picked: A — a per-subcommand allowlist; unknown flags make the loop not approvable. Alternatives: B — a denylist of `--web` / `-w`. Why: §1; `-w` means `--web` on two subcommands and `--workflow` on the third. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-28] What may `echo` print in the body? — Picked: A — literal words (quoted literals included) and `$VAR` / `${VAR}`. Alternatives: B — `$VAR` only. Why: matches "`echo <literal or $VAR>`" in the issue. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-28] May a loop token start with `-`? — Picked: A — no. Alternatives: B — yes, as the issue's regex alone allows. Why: §1; `for r in --web; do gh pr view $r; done` would turn a positional into a flag. Applied in: phase 1 PR. Status: pending review
- AD-10 [phase 1/1, 2026-09-28] How to "build on #4704" (operator note on #4786) while #4704 is still open? — Picked: A — merge #4704's head into the phase branch, then merge `main` once #4704 merges. Alternatives: B — build on the project branch alone and reconcile at #4704's merge. Why: the operator asked to build on it; #4704 merged 2026-09-28 and the phase branch now carries it through `main` (operator relay Q7: A). Applied in: phase 1 PR. Status: pending review
- AD-11 [phase 1/1, 2026-09-28] May `$VAR` form the first path segment of a `gh api` endpoint (`gh api $r`)? — Picked: A — no, the first segment must be literal. Alternatives: B — yes, anywhere before `?`. Why: §1; a first-segment variable could pick `graphql` or any other top-level endpoint. Applied in: phase 1 PR. Status: pending review
- AD-12 [phase 1/1, 2026-09-28] May the lowercase loop variable name a proxy (`https_proxy`, `no_proxy`, …)? — Picked: A — no, any name containing `proxy` is rejected. Alternatives: B — any lowercase name, as the plan's AD-3 alone allows. Why: §1; assigning to an already exported lowercase proxy variable re-routes `gh`'s traffic. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A lowercase-only rule for a shell variable the guard lets a loop assign is not enough: exported lowercase variables such as `https_proxy` / `no_proxy` still change how `gh` connects, so reject proxy names too. (files: workflow-templates/.claude/hooks/gh_api_write_guard.py)

## Notes
- Started by the Claude issue dispatcher (trigger `trig_01UiUhRwpsg5tCsavLazmm7R`) in session `session_01TnvYqW1gqscFF4mBgRJHdk`, Auto mode.
- Security pass: run (`security_pass_skip.py` printed `skip: false`, reason `no skip label`).
- Step 4 protected-path stop: phase 1 is marked `protected paths:` at step 3, and there is no `Protected-path approval: phase 1` line. The block is recorded in this first commit because the step 3a log commit is the only direct push before phase 1, and nothing is written after the stop. The question is on #4786 (`<!-- ai:claude-blocked:v1 -->`, label `ai:claude-blocked`).
- Once #4785 (twin-first edits plus an Actions sync PR) lands, §28.C no longer blocks this phase: it would edit only `workflow-templates/.claude/hooks/`, and the sync PR's owner approval would carry the hook into `.claude/hooks/`.
- Protected-path approval: phase 1 — A (twin-first, 2026-09-28). Operator comment on #4786 (standing Q40: A): edit only `workflow-templates/.claude/hooks/gh_api_write_guard.py`, never `.claude/**`; push the phase PR, post a `hold` claim, stop BLOCKED; the supervising session copies the twin into `.claude/hooks/` as `[claude-twin-sync]`.
- Resumed 2026-09-28 by `/reclarify` in session `session_01MgikEs3YDowQVZL9F3ubPr` (dispatch trigger `trig_01KoML4yQeqmeRtdepeE24go`).
- 2026-09-29: merged `origin/main` (with #4704) into the project and phase branches on the operator relay Q7: A (trigger `trig_01XVS43dbsJrNCdJnuf9fGrC`); no conflicts. The root `.claude/hooks/gh_api_write_guard.py` now equals `main`'s, and the twin is `main`'s plus this phase's additions only.
- `tests/test_family_node_runtime.py` fails locally on `origin/main` too (2 tests); unrelated to this phase.
