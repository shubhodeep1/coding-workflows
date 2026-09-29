# Implement-Plan Log — Issue-mode sessions never end a turn on an in-session question

- Plan: docs/plans/issue-4911-unattended-question-guard-plan.md
- Source issue: shubhodeep1/coding-workflows#4911 (https://github.com/shubhodeep1/coding-workflows/issues/4911)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4911-unattended-question-guard   Final PR: draft (opened right after this commit; number in the issue progress comment)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened; implementing phase 1 under the interim twin-first rule.

## Phases
1. [ ] Phase 1 — unattended question guard (`Stop` + `AskUserQuestion` hook, issue-mode marker)   — protected paths: `.claude/hooks/unattended_question_guard.py` [new], `.claude/settings.json`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/implement-plan-claude.md`, `.claude/commands/seed-repo.md` (all edited through their `workflow-templates/.claude/` twins)
   - [ ] `workflow-templates/.claude/hooks/unattended_question_guard.py`: `mark` CLI, `Stop` block with cap 2, `AskUserQuestion` deny, fail open
   - [ ] `workflow-templates/.claude/settings.json`: `Stop` + `PreToolUse` `AskUserQuestion` wiring, two `mark` allow rules
   - [ ] `workflow-templates/.claude/commands/implement-issue-claude.md` step 0 + `implement-plan-claude.md` step 1 / Issue Mode: run `mark`
   - [ ] `workflow-templates/.claude/commands/seed-repo.md`: list the hook
   - [ ] CLAUDE.md §28.G, README.md, agents.md, `.github/workflows/ci.yml` step, `changelog.d/4911-unattended-question-guard.md`
   - [ ] `tests/test_unattended_question_guard.py`
   - Done: new tests pass with the twins synced; existing settings/command/hook suites still pass

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How does the hook know a session is an unattended issue-mode session? — Picked: A — a local marker `~/.claude/unattended-issue-mode/<CLAUDE_CODE_REMOTE_SESSION_ID>.json`, written by the hook file's own `mark` subcommand from the issue-mode preflight; the hook requires the env id and a marker for that id. Alternatives: B — piggyback on #4755's `permission_prompts.py session-meta`; C — infer from the session title or transcript. Why: #4755 is not on `main`, and titles are not visible to hooks. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the `mark` writer live? — Picked: A — in the hook file itself. Alternatives: B — a new `.claude/scripts/unattended_issue_mode.py`. Why: one marker format in one module, one file to sync (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which sessions write the marker? — Picked: A — `/implement-issue-claude` step 0 and every `/implement-plan-claude` session whose plan carries `Source issue:`. Alternatives: B — `/implement-issue-claude` only; C — also checkers and `/fix-claude-pr`. Why: the §28.A issue-mode scope (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How does the hook tell that the blocked comment was posted, with no API calls? — Picked: A — scan `transcript_path` from the current turn's start for a tool call whose input contains `<!-- ai:claude-blocked:v1 -->` and whose result is not an error. Alternatives: B — a "posted" helper that updates the marker; C — a `PostToolUse` recorder hook. Why: nothing extra to remember and no extra wiring. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What counts as "a question" or "a permission ask"? — Picked: A — a `Q<n>:` line with at least two lettered-choice lines, or the inline `Q<n>: A/B` form; permission asks by a fixed phrase list. Alternatives: B — any line ending in `?`. Why: it matches the §2 format and both observed failures without blocking ordinary reports. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How is the cap counted, and what does the third stop log? — Picked: A — 2 `Stop` blocks per session in `<id>.state.json`; the third is allowed with a `unattended-question-guard: cap reached` `systemMessage` and a line in `~/.claude/unattended-issue-mode/stop-guard.jsonl`. Alternatives: B — 2 per distinct question. Why: the issue says "at most twice per session". Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Is the `AskUserQuestion` denial capped? — Picked: A — no, always deny in a marked session. Alternatives: B — the same cap of 2. Why: allowing stalls at a prompt nobody answers; the `Stop` cap bounds the turn. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] How is `AskUserQuestion` denied? — Picked: A — `permissionDecision: "deny"` JSON. Alternatives: B — exit 2 with stderr. Why: an explicit deny, as the issue asks. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_012hHLRNELGddsYm76muyywq`) in session `session_01MD1KoBnW8eNXCRP4Jn1Ysg`; permission mode auto.
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "label": null, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — interim twin-first rule (operator #4750 Q40: A, restated in the #4911 body: "use the interim twin-first rule (Q40: A). The operator approves the hook sync once") (2026-09-29). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy.
- Session tooling: the repo was attached mid-session, so the SessionStart hook had not run and `gh` was missing; the session ran `.claude/hooks/session-start.sh` to install it. No `mcp__github__*` tools in this session: GitHub writes use REST through the agent proxy.
