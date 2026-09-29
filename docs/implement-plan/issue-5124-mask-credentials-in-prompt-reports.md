# Implement-Plan Log — Mask credentials in permission-prompt reports before they are posted

- Plan: docs/plans/issue-5124-mask-credentials-in-prompt-reports-plan.md
- Source issue: shubhodeep1/coding-workflows#5124 (https://github.com/shubhodeep1/coding-workflows/issues/5124)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4755-report-blocking-permission-prompts
- Project branch: claude/implement-plan-issue-5124-mask-credentials-in-prompt-reports   Final PR: #5166 (draft)
- Status: BLOCKED
- Stage: phase 1/1 (BLOCKED on the operator's [claude-twin-sync] of the phase twin)
- Activation: not started
- Waiting on: PR #5217: twin sync (operator copies the twin into .claude/, then /reclarify on #5124)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: Phase 1 implemented twin-first in PR #5217 (workflow-templates/.claude/scripts/permission_prompts.py only); held with a `hold` claim for the operator's [claude-twin-sync]; no wait armed until /reclarify.

## Phases
1. [ ] Phase 1 — sanitize every posted command fail-closed   — protected paths: .claude/scripts/permission_prompts.py   — PR #5217 open (held for twin sync); review rounds: 0; interventions: 0
   - workflow-templates/.claude/scripts/permission_prompts.py: `_sanitize_bash_command()` (fail-closed shlex parse; masks credential flag values, header values, URL userinfo, credential-named assignments; withholds text and shows the shape otherwise), `_mask_credential_keys()` for non-Bash input, `record_example()` wiring, new `REDACTION_PATTERNS`, safe unparseable shape, attached credential short flags cut to `<flag>*`, docstring
   - tests/test_permission_prompts.py: twin tests for G1–G6; the unparseable shape case moved to a twin test
   - agents.md, CLAUDE.md §23.I, changelog.d/5124-mask-credentials-in-prompt-reports.md
   - Done: new tests pass against the twin; the rest of the file passes except twin parity (waits on the sync); ruff clean; the issue's exploit command and a Basic-auth header leave no credential in the issue body, immediate block, or shape

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How is a posted command made safe? — Picked: A — a fail-closed shell-token parse that masks credential-bearing flag values, header values, URL userinfo, and credential-named assignments, and withholds the text (shape only) when the parse fails or a value cannot be masked exactly. Alternatives: B — post the shape only, always; C — add regex patterns only. Why: the issue's own recommendation, keeping the command where it is safe to (§1, §5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Fix only the consumer-thread post at line 791, or every posted command? — Picked: A — `record_example()`, which feeds line 791, the immediate report in coding-workflows, and every `ai:permission-prompt` issue body and comment. Alternatives: B — line 791 only. Why: the same text reaches the other sites unmasked (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] The shape carries raw text for unparseable commands and attached short flags; change it? — Picked: A — keep only the command word for unparseable commands and cut an attached credential short flag to `<flag>*`, accepting a signature change for those patterns. Alternatives: B — redact the unparseable first line with `redact()`. Why: the withheld fallback relies on the shape being safe. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Mask credential-named keys in non-Bash tool input too? — Picked: A — yes, recursively, plus credential headers inside a `headers` mapping. Alternatives: B — leave non-Bash input to `redact()`. Why: the same leak through another tool in the same function (§12.B). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Add a switch to post raw or shape-only commands? — Picked: A — no switch; the parse decides. Alternatives: B — a new env var for shape-only. Why: §5, §4; the fail-closed default already posts the shape when unsure. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Security pass: skip (security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}).
- Permission mode: auto (issue mode records it).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Phase 1 verification (2026-09-29, Python 3.12): tests/test_permission_prompts.py 157 passed (test_template_parity deselected, waits on the sync); overlay with the twin copied into .claude/: 424 passed, 1 skipped across the related suites; the 47 test files that read CLAUDE.md, agents.md, changelog.d, workflow-templates/.claude, or permission_prompts, one file at a time: 43 passed, 3 hit the 90 s per-file cap while passing (only test_orchestrate_poll_process.py reads agents.md content, and that test passes), 1 has no tests; ruff clean. The full suite does not finish inside this container's time limit.
- Plan deviation: `docker login` keeps `-p` only (`-u` is a username); a credential short flag counts only at the start of a cluster or after curl's boolean flags (`-XPUT` is not `-U`); `auth` excludes `author`.
- Coordination: #5125, #5126, #5127 (projects on the same base, PRs #5159, #5160, #5161) change other functions of `permission_prompts.py`; merge whichever lands first at the step 2 sync.
- Started by session_018rfawpSUKSdZNdgTEgS9MP, which replaced the pickup's session session_014Av6owbebsyBDa36GPDdiS (no repository checkout, #4938), dispatched by the master session session_01Qt5nTTqhWxcYA4NciTC6DL.
