# Implement-Plan Log — Treat workflow-templates/.claude as protected-equivalent for unattended authorization

- Plan: docs/plans/issue-4775-template-claude-protected-equivalent-plan.md
- Source issue: shubhodeep1/coding-workflows#4775
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs
- Project branch: claude/implement-plan-issue-4775-template-claude-protected-equivalent   Final PR: #4783 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4819
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker, safety net, and hand-back ids are in the stage report and the next `— resume.` block
- Last updated: 2026-09-28
- Last note: review round 1 on PR #4819: fixed the `.pyc`-named symlink bypass (and committed bytecode, which the sync copies) in tests/test_claude_template_parity.py; rejected the pinned-digest finding (intended design).

## Phases
1. [ ] Phase 1 — protected-equivalent template tree: CLAUDE.md §23.I/§28.C and the template parity contract test (CLAUDE.md, tests/test_claude_template_parity.py [new], tests/test_permission_prompts.py, .github/workflows/ci.yml, agents.md, changelog.d/4775-template-claude-protected-equivalent.md [new]); protected paths: none   — PR #4819 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should CLAUDE.md treat `workflow-templates/.claude/**`? — Picked: A — keep "not a Claude Code protected path" for prompt triage (a prompt issue about it is fixed) and declare it protected-equivalent for authorization in §23.I and §28.C, since the sync copies it into consumer `.claude/`. Alternatives: B — revert #4678's clause to the unqualified `.claude/**`; C — leave the text as is. Why: A closes the authorization gap the audit found without undoing #4678's triage fix; C leaves a high-severity finding open. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which deterministic control backs the rule? — Picked: A — a new CI contract test over the whole template tree: every template file needs a root twin and is byte-identical to it unless it is in an explicit divergence list whose template SHA-256 is pinned; no symlinks; wired into the existing §23.I `ci.yml` step. Alternatives: B — the same list by name only, with no digest; C — an auto-merge gate in `review_autofix.yml` that requires an owner approval for PRs touching either `.claude/` tree; D — wording only. Why: A fails closed on every template-only command, hook, or settings change and needs no protected-path edit. B lets a divergent command change silently. C changes the documented auto-merge contract for every project and consumer (§12.D), beyond §5 for this finding. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Also change `/implement-plan-claude` step 3 (and its template twin) to name `workflow-templates/.claude/**`? — Picked: A — no. CLAUDE.md §28.C, which step 3 cites and every session reads, names the twins, and the parity test is the deterministic backstop. Alternatives: B — yes, as a root `.claude/**` edit that stops this unattended chain for a watched session (§28.C); C — file a separate issue. Why: A is the smallest change (§5) and needs no protected-path stop. The #4678 conformance run found that step 3's loose reading already stops such a phase. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-28] How is the change recorded in the changelog? — Picked: A — a new `security` fragment `changelog.d/4775-template-claude-protected-equivalent.md` that qualifies the #4678 entry; the #4678 fragment stays as is. Alternatives: B — also rewrite the #4678 fragment. Why: the #4678 statement stays true for Claude Code's own protection, one fragment per PR (§20.B), and B edits another project's entry. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] A tree-parity or allow-list walker must check symlinks before any name-based skip, and may skip `.gitignore`d runtime output (`__pycache__/`, `*.pyc`) only while git does not track it, because the `.claude/` sync copies every regular file (`find -type f`). (files: tests/test_claude_template_parity.py, .github/workflows/update_workflows.yml)

## Notes
- Invoking session: session_013fp3SZXJQE3XBJt3SiWYkw (started by the Claude issue dispatcher routine trig_018FzWWEXvLsvqk8uNUGDReS, permission mode auto).
- Security pass: `security_pass_skip.py` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Issue progress comment id: 5867385117.
- Base branch check (2026-09-28): the only PR whose head is the base branch is #4684 (open, draft, into `main`), so the base has not moved.
