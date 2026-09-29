# Guard: deny inline-interpreter file edits instantly and redirect to the Edit tool

Source issue: shubhodeep1/coding-workflows#4858 (https://github.com/shubhodeep1/coding-workflows/issues/4858)
Base branch: main
Security pass: run

## Summary

Add a deterministic `PreToolUse` hook on `Bash`, `inline_edit_guard.py`, that answers `permissionDecision: "deny"` with a redirect message whenever a command edits files through an inline interpreter (`python3 - <<EOF … write_text(…)`, `python3 -c`, `sed -i`, `perl -pi`, …). A deny needs no human: the session reads the reason and retries with the Edit or Write tool in the same turn, instead of stalling at a permission prompt nobody answers.

## Context

- On 2026-09-28 the operator answered at least 8 permission prompts by hand for inline-interpreter file edits (#4755, #4786, #4791, #4813, #4707, and phase 3 of the convergence project). None of the target files is a protected path, so Edit/Write would have run without a prompt.
- #4678 adds the written rule ("never edit a file with an inline interpreter") to CLAUDE.md §23.I and `agents.md`. It is still in flight on `claude/implement-plan-issue-4678-edit-files-without-python-heredocs` and is not on `main` yet. This issue turns that rule into an enforced one.
- #4785 (twin-first `.claude/**` delivery plus the Actions sync PR) is in flight on `claude/implement-plan-issue-4785-twin-first-claude-sync` (final PR #4804, draft). The issue says this hook "lands via the twin-first path". On `main` today, CLAUDE.md §28.C still stops any unattended phase that edits `.claude/**`.
- Existing patterns this change follows: `.claude/hooks/gh_api_write_guard.py` (quote-aware tokenizing: `strip_heredoc_bodies`, `shell_segments`, `_command_word_index`, `_outside_single_quotes`; the `SETTINGS_MATCHER` constant asserted by tests), `.claude/hooks/pr_watch_guard.py` (fail-open hook with a `systemMessage`), `.claude/hooks/permission_prompt_logger.py` (`build_record` / `append_record`), `.claude/scripts/permission_prompts.py` (`load_records`, `group_patterns`, `file_patterns`).

## Goals

- A Bash command that writes files through an inline interpreter gets `permissionDecision: "deny"` with the exact redirect message from the issue, in every permission mode.
- Read-only interpreter use, `pytest`, scripts run from a file path, and interpreter text that is only data get no decision.
- An unreadable payload or an internal error gets no decision (fail open).
- `CLAUDE_INLINE_EDIT_GUARD=off` disables the guard (default on).
- Every deny writes `INLINE_EDIT_GUARD action=deny …` to stderr and one record to the permission-prompt log; `permission_prompts.py` never files those records as `ai:permission-prompt` issues.
- The hook ships in both `.claude/` and `workflow-templates/.claude/`, byte-identical, wired in both `settings.json` copies, documented in CLAUDE.md §23.I and `agents.md`, and covered by `tests/test_inline_edit_guard.py` in its own `ci.yml` step.

## Non-goals

- No change to `gh_api_write_guard.py` behaviour. Its tokenizer is imported or mirrored, never modified.
- No deny for interpreter writes to protected paths beyond what the redirect message says; no attempt to detect writes made by scripts run from a file.
- No change to Claude Code's built-in protected-path handling.

## Constraints

- §1: a false deny costs one retry and a false negative costs one prompt, so matching stays conservative. Fail open (issue item 5) is deliberate and differs from `gh_api_write_guard.py`, which fails closed.
- §4: the new env var `CLAUDE_INLINE_EDIT_GUARD` defaults to on.
- §6: no existing identifier is renamed. New names (`inline_edit_guard.py`, `INLINE_EDIT_GUARD`, `CLAUDE_INLINE_EDIT_GUARD`, `tests/test_inline_edit_guard.py`) were checked against the repo and are unused.
- §9: tabs in Python; `settings.json` keeps its existing indentation.
- §14: `workflow-templates/.claude/**` changes reach consumer repos on the next `@stable` sync; no new consumer.
- §15: the hook issues no API calls.
- §20: one changelog fragment, `changelog.d/4858-inline-edit-guard.md`.
- §23.H: the guard sits next to `gh_api_write_guard.py` under the same `Bash` matcher and does not change its decisions. When both hooks decide, Claude Code applies the most restrictive decision (deny beats ask beats allow). That is the intended outcome for a `python3` heredoc that also carries a `gh api` call.
- §28.C: on `main` this phase edits protected paths (`.claude/hooks/**`, `.claude/settings.json`, and `.claude/scripts/permission_prompts.py`), so it is never started unattended without a recorded `Protected-path approval:` line. See Risks.

## Approach

1. **Detection.** Strip heredoc bodies and tokenize with the same quote-aware helpers as `gh_api_write_guard.py`. For every segment whose command word (past `env`, assignments, `sudo`, and the other prefix words) is an interpreter:
   - `python3` / `python` (any `pythonX.Y`) with `-` and a heredoc, or with `-c <program>`: deny when the program text matches a write pattern: `write_text`, `write_bytes`, `open(` with a `'w'`/`'a'`/`'x'`/`'r+'` mode (positional or `mode=`), `.replace(` together with a write, `os.replace`, `shutil.`, `.unlink(`, `os.remove`.
   - `sed -i` / `--in-place`, `perl -i` / `-pi` (clustered flags too), `ruby -i`, `awk -i inplace` / `gawk -i inplace`: deny outright, since these flags only exist to edit files in place.
   - `python3 <file path>`, `python3 -m pytest`, and `pytest` are never inspected.
   - Interpreter text that is an argument of another command (`git commit -m "…python3 - …"`, `echo`, `grep`, a quoted string) is data: only a segment's command word counts, which is how the `gh api` guard already treats data.
2. **Output.** A deny prints `hookSpecificOutput` with `permissionDecision: "deny"` and the issue's message verbatim as `permissionDecisionReason`, writes `INLINE_EDIT_GUARD action=deny kind=<python|sed|perl|ruby|awk> session=<id>` to stderr, and appends one record through `permission_prompt_logger.build_record` / `append_record` (imported from the sibling hook file). The record's `event` is `PermissionDenied` and it carries `source: "inline_edit_guard"`.
3. **Filing exclusion.** `permission_prompts.py` skips records whose `source` is `inline_edit_guard` when grouping patterns to file, and counts them separately in its report (`expected_denies: <n>`), so they are visible but never filed.
4. **Fail open.** An unreadable, invalid, or non-object payload, an unparseable command, or any internal exception returns no decision (a `systemMessage` warning for internal errors, as in `pr_watch_guard.py`). Empty input is allowed silently.
5. **Delivery.** Add the hook and wiring to `workflow-templates/.claude/**` and `.claude/**` together and keep them byte-identical. If #4785's twin-first rule is on the base when the phase runs, edit only the twins and let the sync PR land the root copies.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the hook, its wiring, its logging and filing exclusion, docs, and tests are one reviewable unit, and a hook without its wiring or tests is not shippable on its own.

1. **Phase 1 — inline-edit guard hook, wiring, logging, docs, tests.**
   - Files: see Files & Modules.
   - Protected paths: `.claude/hooks/inline_edit_guard.py`, `.claude/settings.json`, `.claude/scripts/permission_prompts.py`.
   - Done: `tests/test_inline_edit_guard.py` passes locally and in its own `ci.yml` step. The existing `tests/test_gh_api_write_guard.py`, `tests/test_permission_prompts.py`, and `tests/test_pr_watch_guard.py` still pass. `diff -r` shows the hook, `permission_prompts.py`, and `settings.json` byte-identical between `.claude/` and `workflow-templates/.claude/`. CLAUDE.md §23.I and `agents.md` document the guard.
   - Rollback: revert the PR, or set `CLAUDE_INLINE_EDIT_GUARD=off` in the session environment with no code change.

## Implementation Steps

1. `workflow-templates/.claude/hooks/inline_edit_guard.py` [new] and its byte-identical `.claude/hooks/inline_edit_guard.py` [new]: `SETTINGS_MATCHER = "Bash"`, `ENV_KILL_SWITCH = "CLAUDE_INLINE_EDIT_GUARD"`, `DENY_MESSAGE` (the issue's text verbatim), `evaluate(payload) -> (decision | None, reason, kind)`, `main()`. Load the tokenizer from `gh_api_write_guard.py` by file path with `importlib.util`, since hooks are not a package, and fall back to no decision if the import fails.
2. Both `settings.json` copies: add a `PreToolUse` entry `{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/inline_edit_guard.py", "timeout": 30}]}` after the `gh_api_write_guard.py` entry.
3. Both `permission_prompts.py` copies: skip `source == "inline_edit_guard"` records in pattern grouping and filing, and report their count as `expected_denies`.
4. `tests/test_inline_edit_guard.py` [new]: covers the cases listed under Tests. `tests/test_permission_prompts.py`: one new case for the filing exclusion.
5. `.github/workflows/ci.yml`: add a step `Inline edit guard tests` running `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_inline_edit_guard.py`, next to the `test_gh_api_write_guard.py` step.
6. CLAUDE.md §23.I and `workflow-templates/CLAUDE.md` §23.I: add a paragraph for the guard (match rules, deny, kill switch, fail open, logging, filing exclusion). If #4678's rule text is on the base by then, turn it into "enforced by `inline_edit_guard.py`"; otherwise add the rule sentence together with the enforcement. `agents.md`: one bullet in the hooks section.
7. `changelog.d/4858-inline-edit-guard.md` [new], `<!-- changelog: added -->`.

## Files & Modules

- `.claude/hooks/inline_edit_guard.py` [new] (protected)
- `workflow-templates/.claude/hooks/inline_edit_guard.py` [new]
- `.claude/settings.json` (protected)
- `workflow-templates/.claude/settings.json`
- `.claude/scripts/permission_prompts.py` (protected)
- `workflow-templates/.claude/scripts/permission_prompts.py`
- `.claude/commands/seed-repo.md` (protected) and `workflow-templates/.claude/commands/seed-repo.md` (AD-8)
- `tests/test_inline_edit_guard.py` [new]
- `tests/test_permission_prompts.py`
- `.github/workflows/ci.yml`
- `CLAUDE.md`
- `workflow-templates/CLAUDE.md`
- `agents.md` (hooks bullets near the `gh_api_write_guard.py` and `permission_prompt_logger.py` entries)
- `changelog.d/4858-inline-edit-guard.md` [new]

## Tests

- Denied: the three 2026-09-28 command shapes from #4755, #4786, and #4791 (taken from those issues' permission-prompt records), plus `sed -i`, `perl -pi`, `python3 -c "open('x','w').write('y')"`, and a `pathlib` `write_text` heredoc.
- No decision: a read-only heredoc (`python3 - <<'EOF'\nimport json; print(json.load(open('x')))\nEOF`), `pytest -q`, `python3 -m pytest`, `python3 scripts/x.py`, `python3 .claude/scripts/x.py`, `git commit -m "fix python3 - <<EOF write_text"`, and `echo "sed -i"`.
- Kill switch: `CLAUDE_INLINE_EDIT_GUARD=off` gives no decision for a denied shape.
- Fail open: invalid JSON, a non-object payload, an unparseable command, and a forced internal error each give no deny.
- Logging: a deny appends one `source: "inline_edit_guard"` record under a temporary `HOME`, and stderr carries `INLINE_EDIT_GUARD action=deny`.
- Filing: `permission_prompts.py` groups no pattern from those records and reports `expected_denies`.
- Wiring: both `settings.json` copies carry the entry under `SETTINGS_MATCHER`, and the hook files are byte-identical.

## Risks

- **Protected paths (§28.C).** On `main` this phase edits `.claude/**`, so `/implement-plan-claude` stops before it starts unless a `Protected-path approval:` line is recorded. The issue expects #4785's twin-first delivery, which is not merged yet.
- **Dependency on #4678.** The §23.I rule text this plan enforces is on #4678's project branch, not on `main`. Step 6 covers both orders.
- **False positives.** A heredoc that builds a string containing `write_text` without writing is denied. The session retries with Edit/Write, which is the desired behaviour anyway. Kept conservative.
- **Hook interplay.** With two `Bash` `PreToolUse` hooks, a deny from this guard overrides an allow from `gh_api_write_guard.py` for the same command. That is correct for a heredoc that writes files.

## Rollout

Ships with the final PR into `main`; consumer repos receive it on the next `@stable` sync (§14). Kill switch: `CLAUDE_INLINE_EDIT_GUARD=off`.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does a deny get recorded for `permission_prompts.py`? — Picked: A — the guard appends a `PermissionDenied` record with `source: "inline_edit_guard"` through `permission_prompt_logger.py`'s own functions, and `permission_prompts.py` skips that source when filing. Alternatives: B — rely on Claude Code firing `PermissionDenied` for hook denies (not documented, so it may never be logged); C — a separate log file (a second format to maintain). Why: it reuses the existing log and makes the exclusion explicit and testable. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How should `sed -i` / `perl -i` / `ruby -i` / `awk -i inplace` be matched? — Picked: A — deny on the in-place flag alone. Alternatives: B — also require a write pattern in the program text (in-place flags always write, so this adds nothing). Why: the flag is the write. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How does the new hook reuse `gh_api_write_guard.py`'s tokenizer? — Picked: A — load it by path with `importlib.util` and fall back to no decision if that fails. Alternatives: B — copy the functions (two copies drift); C — extract a shared module (§5 wider change and a rename risk, §6). Why: it is the smallest change that keeps one tokenizer. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] The issue asks for twin-first delivery (#4785) and for turning the #4678 rule into an enforced one, but neither is on `main`. Which base does this project use? — Picked: A — the default branch, as `/implement-issue-claude` step 3 prescribes when the issue names no `Integration branch:`/`Target branch:`, and write steps 5–6 so they work whether or not #4785 and #4678 have merged by then. Alternatives: B — base on #4785's project branch (the issue does not name it, and it would tie this project to that one's lifecycle). Why: base resolution is rule-defined. The dependency is surfaced in Risks and on the issue. Applied in: no code change. Status: pending review
- AD-5 [phase 1/1, 2026-09-29] Which commands stand for "the three 2026-09-28 command shapes from #4755, #4786 and #4791" in the tests? — Picked: A — the outer shapes of the inline-interpreter `ai:permission-prompt` records #4843, #4857, #4881 and #4905, each with a representative write in place of the heredoc body the record omits. Alternatives: B — made-up shapes. Why: those records are the only surviving evidence of the prompts, and they omit heredoc bodies by design. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-29] Does any `shutil.` call count as a write? — Picked: A — only the mutating calls (`copy`, `copy2`, `copyfile`, `copyfileobj`, `copytree`, `copymode`, `copystat`, `move`, `rmtree`). Alternatives: B — any `shutil.` text, as the issue lists it. Why: B would deny a read-only `shutil.which` with a reason that tells the session to use the Edit tool, which is wrong (§1 correctness). Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1, 2026-09-29] Should `permission_prompts.py` also skip a `PermissionDenied` record that carries the guard's reason but no `source`? — Picked: A — yes, as a backstop to AD-1. Alternatives: B — `source` only. Why: if Claude Code logs the hook's deny through its own `PermissionDenied` event, that record has no `source` and would otherwise be filed as a new issue. Applied in: phase 1 PR. Status: pending review
- AD-8 [phase 1/1, 2026-09-29] Should the `/seed-repo` file list name the new hook? — Picked: A — add `hooks/inline_edit_guard.py` to the list in `seed-repo.md`. Alternatives: B — leave the list alone. Why: the list says it is the set the sync mirrors, and the sync already copies the hook, so B leaves it stale. Applied in: phase 1 PR. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py` exited 2 in the planning session (`gh` is not installed there), so `Security pass: run`. The issue carries no skip label either.
