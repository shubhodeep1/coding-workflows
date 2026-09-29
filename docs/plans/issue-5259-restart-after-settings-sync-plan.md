# Verify the Claude-asset sync's settings.json is loaded before privileged work, else continue in a fresh session

Source issue: shubhodeep1/coding-workflows#5259 (https://github.com/shubhodeep1/coding-workflows/issues/5259)
Base branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start
Security pass: skip (ai:security: automation-produced issue)

## Summary

The Claude-asset sync (#4952) merges the default branch's `.claude/hooks/**` and `.claude/settings.json` into a working branch, then lets the session keep fixing and pushing. When that merge changes the hook wiring in `settings.json` (for example a new `PreToolUse` guard), nothing confirms the running session is using it. This plan records which `settings.json` the session has actually loaded, checks it after the sync, and, when the new wiring is not confirmed, stops before any further privileged tool call and hands the same work to a fresh session.

## Context

- Security audit finding `asset-sync-settings-not-active` (high, confidence 9/10) at `.claude/commands/implement-plan-claude.md:186` on the #4952 project branch: step 6 of `### Claude-asset sync` only records `settings.json changed by the asset sync; new hook wiring applies from the next session`, and the session carries on.
- What Claude Code does (docs, checked 2026-09-29): "Claude Code watches your settings files and reloads them when they change, so it applies most edits to the running session without a restart, including edits to `permissions`, `hooks`" (settings.md, "When edits take effect"), but "If they haven't appeared after a few seconds, the file watcher may have missed the change: restart your session to force a reload" (hooks-guide.md). It runs the `ConfigChange` hook "for each settings-file change it detects", before the change is applied; `project_settings` is the source for `.claude/settings.json`. SessionStart input carries `source` (`startup`, `resume`, `clear`, `compact`, `fork`). Every hook gets `session_id`, which is the session's transcript id (`CLAUDE_CODE_SESSION_ID`).
- So the reload is normal but not guaranteed, and #4952's step 6 text ("the harness reads `settings.json` only at session start") is inaccurate. A session cannot tell today whether a merged `settings.json` is active.
- Precedent: `.claude/hooks/permission_prompt_logger.py` records hook events to `~/.claude/permission-prompts/<session id>.jsonl`, outside the repository, and an allowlisted `.claude/scripts/` helper reads them (CLAUDE.md §23.I).
- Binding rules: CLAUDE.md §1 (security first), §5, §6 (new identifiers unique; nothing renamed), §9 (tabs; JSON/YAML keep their indentation), §15 (no GitHub API calls), §18 (no manual scripts; the recorder is a hook, the check runs inside the commands), §20 (changelog fragment), §28.C (a second failure is an escalation; `.claude/**` edits run twin-first under the interim default).

## Goals

- A hook records, per session, the sha256 of the `.claude/settings.json` the session loaded: at SessionStart for `startup` / `resume` / `fork` (a process that read the file at start), and at every `ConfigChange` for `project_settings` (a change the watcher detected). `clear` and `compact` record nothing.
- An allowlisted check compares that record with the working tree's `.claude/settings.json` and prints one JSON line: `current` true/false with the reason; no record means not current.
- Step 6 of the Claude-asset sync runs the check whenever the sync merge changed `settings.json`. Current → continue. Not current (after one re-run) → no further commit, push, claim, comment, label change, or dispatch for that work in this session; the same stage or fix continues in a fresh session.
- A session that is already such a restart and is still not current stops as a failure escalation (`Status: BLOCKED`, or a hold in `/fix-claude-pr`).
- No GitHub API calls; neither the hook nor the check can block a tool call or a config change.

## Non-goals

- Changing what the sync merges or when (steps 1–5 of `### Claude-asset sync` stay as #4952 shipped them).
- Wiring outside `.claude/settings.json` (user, local, managed settings).
- Forcing a reload inside a running session.

## Constraints

- §6: new identifiers `.claude/hooks/settings_load_recorder.py`, `.claude/scripts/loaded_settings_check.py`, the record directory `~/.claude/loaded-settings/`, the `### Settings restart` section, the `Asset-sync restart:` resume line, and the `— settings restart` argument marker were checked unique on the base branch. The existing SessionStart `session-start.sh` entry stays first and unchanged.
- §15: local file reads and hashing only.
- §23.I: the check is an allowlisted helper (exact `permissions.allow` rules, as its own Bash call), so unattended sessions do not stop at a prompt.
- §28.C: protected paths → twin-first (edit `workflow-templates/.claude/**`, then `[claude-twin-sync]`).

## Approach

1. **Recorder hook** `settings_load_recorder.py` (wired under `SessionStart` and under `ConfigChange` with matcher `project_settings`): hashes `.claude/settings.json` (the `config_file_path` of a `ConfigChange`; `$CLAUDE_PROJECT_DIR`, else the payload `cwd`, for SessionStart; `absent` when the file is missing) and overwrites `~/.claude/loaded-settings/<session id>.json` with `sha256`, `event`, `source`, and `ts`. Prints nothing, exits 0, swallows every error.
2. **Check** `loaded_settings_check.py [--session-id <id>] [--settings <path>]` (the id defaults to `CLAUDE_CODE_SESSION_ID`): prints `{"current": …, "loaded_sha256": …, "file_sha256": …, "record": …, "reason": …}`; exit 0 current, 1 not current (including no record), 2 on a usage error.
3. **Step 6 of the sync**, rewritten: states the watcher behaviour accurately; when the merge changed `settings.json`, run the check; not current → run it once more (the watcher can lag a few seconds); still not current → [Settings restart](#settings-restart).
4. **`### Settings restart`** (new, after the sync section): the sync merge stays local and unpushed and nothing else is written; `/implement-plan-claude` starts a fresh stage session with the two-step start (title `implement-plan <slug> — <stage> — settings restart`, the same stage's `— resume.` block with `Previous stage session:` and `Asset-sync restart:` set to this session's id), and the new session's Claims check also looks past that id; `/fix-claude-pr` starts a fresh fixer with `/fix-claude-pr <PR URL> — kind <kind> — head <sha> — claim <this session id> — settings restart` (the existing `— claim` hint). A session that is itself a restart stops instead; a session without the claude-code-remote tools stops and asks for a new session.

Alternatives considered: restart whenever the sync changes `settings.json` (the fresh session repeats the same merge and loops, and it proves nothing, because the watcher reloads the branch's file on checkout anyway); record only at SessionStart (wrong under hot reload: the checkout reloads the branch's older file); hash only the `hooks` key (misses permission-rule changes); treat the finding as void because the watcher normally reloads (the docs say it can miss a change, and §1 puts security first).

## Phases & Merge Strategy

This plan has exactly **one** phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — record, check, and restart.** Files: see [Files & Modules](#files--modules). Done when: the recorder and the check exist as twins, `settings.json` wires the recorder under `SessionStart` (after `session-start.sh`) and `ConfigChange` (`project_settings`) and allowlists the check, step 6 and `### Settings restart` are in both command twins, `/fix-claude-pr` step 5 routes a not-current check to the restart, and the listed suites pass with the twins copied over `.claude/`. Rollback: revert the phase PR; step 6 falls back to #4952's note. protected paths: `.claude/hooks/settings_load_recorder.py`, `.claude/scripts/loaded_settings_check.py`, `.claude/settings.json`, `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/seed-repo.md`.

## Implementation Steps

1. `workflow-templates/.claude/hooks/settings_load_recorder.py` [new]: `SETTINGS_EVENTS = ("SessionStart", "ConfigChange")`, `build_record(payload, now, project_dir)`, `write_record(record, directory)`, `main()`; the style of `permission_prompt_logger.py`.
2. `workflow-templates/.claude/scripts/loaded_settings_check.py` [new]: reads the record, hashes the file, prints the JSON line, exit codes as above.
3. `workflow-templates/.claude/settings.json`: a second `SessionStart` entry and a `ConfigChange` entry (matcher `project_settings`) running `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/settings_load_recorder.py` with `timeout: 10`; `Bash(python3 .claude/scripts/loaded_settings_check.py *)` and its `PYTHONDONTWRITEBYTECODE=1` form in `permissions.allow`.
4. `workflow-templates/.claude/commands/implement-plan-claude.md`: step 6 of the sync, `### Settings restart`, the optional `Asset-sync restart:` line in the Stage Sessions template, the Claims note, and the "No side sessions" rule.
5. `workflow-templates/.claude/commands/fix-claude-pr.md`: the `— settings restart` marker in the arguments paragraph; step 5 runs the Settings restart on a not-current check.
6. `workflow-templates/.claude/commands/seed-repo.md`: list the two new files in the seeded `.claude/` set.
7. Tests: `tests/test_settings_load_recorder.py` [new] (recorder events and sources, overwrite, absent file, bad payloads, no output, wiring in both `settings.json` copies, check exit codes and JSON, twin parity); extend `tests/test_claude_asset_sync_command.py` (step 6 and restart text in both command copies, fix-claude-pr routing, the one-restart cap, no `$(`); `.github/workflows/ci.yml` adds the new file to the Claude-asset sync step.
8. `agents.md` (Claude-asset sync bullet, the record path), `.ai/.workspace_source_manifest.txt` (the four new paths), `changelog.d/5259-verify-loaded-settings-after-asset-sync.md` (`security`).

## Files & Modules

- `workflow-templates/.claude/hooks/settings_load_recorder.py` [new] (and `.claude/` copy via `[claude-twin-sync]`)
- `workflow-templates/.claude/scripts/loaded_settings_check.py` [new] (and `.claude/` copy)
- `workflow-templates/.claude/settings.json` (and `.claude/` copy)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (and `.claude/` copy)
- `workflow-templates/.claude/commands/fix-claude-pr.md` (and `.claude/` copy)
- `workflow-templates/.claude/commands/seed-repo.md` (and `.claude/` copy)
- `tests/test_settings_load_recorder.py` [new], `tests/test_claude_asset_sync_command.py`
- `.github/workflows/ci.yml`, `agents.md`, `.ai/.workspace_source_manifest.txt`
- `changelog.d/5259-verify-loaded-settings-after-asset-sync.md` [new]

## Tests

- Unit (pytest, temp dirs, no network): recorder and check behaviour, `settings.json` wiring and allow rules in both copies, command text in both copies, twin parity.
- Existing suites still pass with the twins copied over `.claude/`: `tests/test_claude_asset_sync_command.py`, `tests/test_session_start_claude_assets_drift.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_update_workflows_guardrails.py`, `tests/test_pr_merge_status_guard.py`, `tests/test_permission_prompts.py`, `tests/test_gh_api_write_guard.py`, `tests/test_pr_watch_guard.py`, `tests/test_pr_check_in_reminder.py`.
- On the branch as pushed (twin-first), only `.claude/` parametrizations and twin-parity checks fail until the `[claude-twin-sync]` commit.

## Risks & Mitigations

- The watcher reports a change a few seconds after the merge → the check re-runs once before restarting; a false "not current" costs one fresh session, never a push under unconfirmed wiring. ACCEPTED.
- A future `ConfigChange` hook that blocks changes would make a recorded hash describe a change that was not applied. Mitigation: the recorder's docstring and `agents.md` say the recorder must stay the only `ConfigChange` hook, and the wiring test asserts it.
- `CLAUDE_CODE_SESSION_ID` unset (a local session) → no record found → not current → the session stops and asks for a new session. ACCEPTED — fail closed; a human is at a local keyboard.
- A branch with its own `settings.json` edits in an environment whose watcher never fires → the fresh session is not current either and the project stops. ACCEPTED — rare, and a human decides.
- One extra session link per restart; a restarted session never restarts again, so the depth budget (CLAUDE.md §26.B step 1c) absorbs it.

## Rollout

Ships with the #4952 project branch when its final PR #4995 merges into `main`, and reaches consumer repos through the `@stable` `.claude/` sync (the new hook and script are in the mirrored `workflow-templates/.claude/` tree). Rollback: revert the phase PR.

## References

- Issue #5259, audit tracker #3576, parent project #4952 (final PR #4995, phase PR #5010).
- https://code.claude.com/docs/en/settings.md ("When edits take effect"), https://code.claude.com/docs/en/hooks.md (`ConfigChange`, SessionStart input, common input fields), https://code.claude.com/docs/en/hooks-guide.md (troubleshooting).

## Auto-decisions

- AD-1 [plan, 2026-09-29] Claude Code's docs say settings edits normally hot-reload; what does this issue ship? — Picked: A — verify the reload and hand the work to a fresh session when it is not confirmed. Alternatives: B — close the finding as not reproducible and only correct step 6's text; C — always restart after a `settings.json` change. Why: the docs say the watcher can miss a change, §1 puts security first, and C loops without proving anything. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does a session know which `settings.json` it has loaded? — Picked: A — a recorder hook on SessionStart (`startup` / `resume` / `fork`) and `ConfigChange` (`project_settings`) writes the file's sha256 to `~/.claude/loaded-settings/<session id>.json`. Alternatives: B — SessionStart only, through `CLAUDE_ENV_FILE`; C — the model hashes the file in step 0. Why: B and C miss the watcher's reloads (a checkout reloads the branch's older file), and C is lost on a container restart. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Where does the comparison live? — Picked: A — an allowlisted `.claude/scripts/loaded_settings_check.py` helper. Alternatives: B — three shell commands the model compares by eye. Why: a deterministic verdict, the §23.I helper pattern, and no `$` expansion in the command. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Hash the whole `settings.json` or only its `hooks` key? — Picked: A — the whole file. Alternatives: B — `hooks` only. Why: §1, permission-rule changes matter too, and A needs no JSON canonicalisation. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] No record for this session? — Picked: A — not current (fail closed). Alternatives: B — current. Why: §1; unconfirmed wiring must not push. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How many restarts per stage? — Picked: A — one; a restarted session that is still not current stops as a §28.C failure escalation. Alternatives: B — unbounded. Why: bounds the loop and the session depth. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What happens to the unpushed sync merge on a restart? — Picked: A — it stays local and is dropped with this session; the fresh session repeats the checkout and the sync. Alternatives: B — push it first. Why: B is a privileged push under unconfirmed wiring, which the finding forbids. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] #4952's step 6 says the harness reads `settings.json` only at session start, which the docs contradict. Fix the text here? — Picked: A — yes, rewrite it accurately in the same step. Alternatives: B — leave it. Why: stale docs that mislead readers are a §12.B fix, and the step is being rewritten anyway. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5259` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
