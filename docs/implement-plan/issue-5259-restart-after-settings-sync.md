# Implement-Plan Log — Verify the Claude-asset sync's settings.json is loaded before privileged work, else continue in a fresh session

- Plan: docs/plans/issue-5259-restart-after-settings-sync-plan.md
- Source issue: shubhodeep1/coding-workflows#5259
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5259-restart-after-settings-sync   Final PR: #5283 draft
- Status: BLOCKED
- Stage: conformance 1/3
- Activation: not started
- Waiting on: PR #5425: twin sync (`[claude-twin-sync]` of `workflow-templates/.claude/commands/implement-plan-claude.md`), then `/reclarify` on #5259
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (twin-first hold; the stage `/reclarify` resumes arms the wait on PR #5425 with project checker session_01AZ9NYMGehcX3kmHiQv4Se3, reusable)
- Last updated: 2026-09-30
- Last note: conformance run 1: CONFORMANT with one EVIDENCE-BASED concern fixed twin-first in conformance-fix PR #5425; hold claim posted and the twin-sync blocker posted on #5259.

## Phases
1. [x] Phase 1 — record, check, and restart — PR #5301 merged 2026-09-29 (0f61e1a) without its `.claude/` copies; twin-sync PR #5350 merged 2026-09-30 (5121ec3; carries them, f8b44fd); review rounds: 1 (on #5350: 3 findings fixed in 2771e3b, 2 rejected; round-1 twins synced in 9b385c5 after the second twin-sync blocker was answered by `/reclarify` on #5259); interventions: 0 — protected paths: `.claude/hooks/settings_load_recorder.py`, `.claude/scripts/loaded_settings_check.py`, `.claude/settings.json`, `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/seed-repo.md`
   - Recorder hook `settings_load_recorder.py` (SessionStart `startup`/`resume`/`fork`, ConfigChange `project_settings`) writing `~/.claude/loaded-settings/<session id>.json`.
   - Allowlisted check `loaded_settings_check.py` (exit 0 current, 1 not current, 2 usage error).
   - `settings.json`: recorder wiring and the check's allow rules.
   - `implement-plan-claude.md`: sync step 6 rewrite, `### Settings restart`, `Asset-sync restart:` resume line, Claims note, "No side sessions" rule.
   - `fix-claude-pr.md`: `— settings restart` marker and step 5 routing; `seed-repo.md` file list.
   - Tests: `tests/test_settings_load_recorder.py` [new], `tests/test_claude_asset_sync_command.py`; `ci.yml` step.
   - `agents.md`, `.ai/.workspace_source_manifest.txt`, `changelog.d/5259-verify-loaded-settings-after-asset-sync.md`.
   - Done: all present and the plan's listed suites pass with the twins copied over `.claude/`.

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — fix PR #5425 (pre-security; twin-first, `[claude-twin-sync]` pending). Finding fixed: step 2's project-branch sync merge pushed without pointing at the sync's step 6 settings check. Not fixed (HYPOTHESIS): see `## Notes`.

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Claude Code's docs say settings edits normally hot-reload; what does this issue ship? — Picked: A — verify the reload and hand the work to a fresh session when it is not confirmed. Alternatives: B — close the finding as not reproducible and only correct step 6's text; C — always restart after a `settings.json` change. Why: the docs say the watcher can miss a change, §1 puts security first, and C loops without proving anything. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does a session know which `settings.json` it has loaded? — Picked: A — a recorder hook on SessionStart (`startup` / `resume` / `fork`) and `ConfigChange` (`project_settings`) writes the file's sha256 to `~/.claude/loaded-settings/<session id>.json`. Alternatives: B — SessionStart only, through `CLAUDE_ENV_FILE`; C — the model hashes the file in step 0. Why: B and C miss the watcher's reloads (a checkout reloads the branch's older file), and C is lost on a container restart. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Where does the comparison live? — Picked: A — an allowlisted `.claude/scripts/loaded_settings_check.py` helper. Alternatives: B — three shell commands the model compares by eye. Why: a deterministic verdict, the §23.I helper pattern, and no `$` expansion in the command. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Hash the whole `settings.json` or only its `hooks` key? — Picked: A — the whole file. Alternatives: B — `hooks` only. Why: §1, permission-rule changes matter too, and A needs no JSON canonicalisation. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] No record for this session? — Picked: A — not current (fail closed). Alternatives: B — current. Why: §1; unconfirmed wiring must not push. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How many restarts per stage? — Picked: A — one; a restarted session that is still not current stops as a §28.C failure escalation. Alternatives: B — unbounded. Why: bounds the loop and the session depth. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What happens to the unpushed sync merge on a restart? — Picked: A — it stays local and is dropped with this session; the fresh session repeats the checkout and the sync. Alternatives: B — push it first. Why: B is a privileged push under unconfirmed wiring, which the finding forbids. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] #4952's step 6 says the harness reads `settings.json` only at session start, which the docs contradict. Fix the text here? — Picked: A — yes, rewrite it accurately in the same step. Alternatives: B — leave it. Why: stale docs that mislead readers are a §12.B fix, and the step is being rewritten anyway. Applied in: phase 1. Status: pending review
- AD-9 [phase 1/1 — review round 1, 2026-09-30] Commit the round-1 progress-log update on twin-sync PR #5350? — Picked: A — no; the log update rides the next stage's branch. Alternatives: B — commit it on #5350. Why: a docs-only push to #5350 restarts its review on a new head for no code change. Applied in: no code change. Status: pending review

## Lessons
- [source:conformance] When a new check gates a push, name it in every step that pushes the same merge: a step that says "push" and only cross-references the section holding the check is a sibling path a literal reader skips. (files: .claude/commands/implement-plan-claude.md)
- [source:plan-deviation] Check the harness's documented reload behaviour before designing around "read only at session start": Claude Code's file watcher hot-reloads `settings.json` (hooks and permissions) and runs `ConfigChange` per detected change, so a load check must follow `ConfigChange`, not just SessionStart. (files: .claude/hooks/settings_load_recorder.py, .claude/commands/implement-plan-claude.md)

## Notes
- Started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5259: start`, trig_01S9RqpmwPtkyb24Ypi2gKzk) into session `session_01He3eCz8uiMKn6xvEm4y8Lu` (permission mode `auto`).
- Base branch: `claude/implement-plan-issue-4952-sync-claude-assets-at-session-start` (the issue's `Integration branch:` line); its final PR #4995 is an open draft into `main`, so this project ends after its own final merge (`Activation: n/a (base …)`) and the issue is closed explicitly by the final-merge stage.
- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Twin-first verification (2026-09-29): in a scratch copy with the six twins copied over `.claude/`, the 60 test files that reference the changed paths ran one file at a time with the same result as the unmodified base branch: `test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean` fails on both, `test_orchestrate_poll_process.py` exceeds the 600 s per-file timeout on both, everything else passes. `tests/inventory_parity.py` and `tests/test_session_start_extract_repo_slug.py` pass. On the branch as pushed, only the 37 `.claude/` copy and parity cases fail, as expected until the sync.
- Plan deviation: the plan's first draft recorded the hash only at SessionStart through `CLAUDE_ENV_FILE`; Claude Code's documented file watcher reloads `settings.json` on checkout and merge, so the recorder also runs on `ConfigChange` and writes `~/.claude/loaded-settings/<session id>.json` (AD-2). The committed plan already carries the revised design.
- Twin-sync PR #5350 merged at 2026-09-30T03:40:48Z (merge commit 5121ec323f22501612f19d0fb21826af0d588d92); the project checker session_01AZ9NYMGehcX3kmHiQv4Se3 was reused for its wait and started `conformance 1/3`.
- Conformance run 1 (2026-09-30), not fixed (HYPOTHESIS, CONCERN): Claude Code runs `ConfigChange` hooks before a change is applied, from the settings currently loaded. A working branch whose `settings.json` predates the recorder has no `ConfigChange` hook, so after it is checked out the sync merge's `settings.json` change is never recorded; the check reports "not current" even when the watcher applied the merge, and the restarted session repeats the same sequence and escalates. Fail-closed, and limited to branches cut before this project reaches the default branch. A `claude -p` run in a scratch repo fired no `ConfigChange` within 8 s, so the watcher could not be exercised; listed for the final PR's whole-project review.
- Protected-path approval: conformance fix 1 runs under `phase 1 — twin-first`: only `workflow-templates/.claude/commands/implement-plan-claude.md` changes on PR #5425.
