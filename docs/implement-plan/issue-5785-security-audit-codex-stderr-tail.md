# Implement-Plan Log — security_audit.sh: print a sanitized, size-capped tail of Codex stderr on a codex-execution failure

- Plan: docs/plans/issue-5785-security-audit-codex-stderr-tail-plan.md
- Source issue: shubhodeep1/coding-workflows#5785
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5785-security-audit-codex-stderr-tail   Final PR: (opening) draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: project branch opened by /implement-issue-claude (issue mode); phase 1 starting.

## Phases
1. [ ] Phase 1 — Codex stderr tail and provider class on codex-execution failures

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Where do the extra masks (`sk-`, long hex/base64, secret env values) live? — Picked: A — a tail-only `security_audit_mask_stderr_line` applied before the existing `security_audit_sanitize_log_value`. Alternatives: B — extend the shared sanitizer (would redact SHAs and paths in every other failure line); C — existing sanitizer only (misses generic `sk-` keys and the test env values). Why: meets issue item 1 without changing any other phase's output (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] `codex exec` echoes the prompt to stderr, and the existing contract test forbids prompt text and env values in failure output. How does the tail respect that? — Picked: A — drop tail lines that exactly match a rendered-prompt line, and mask literal values of secret-named env vars. Alternatives: B — relax the existing test assertions for codex-execution. Why: keeps the existing invariant and issue item 3, and stops prompt text from skewing the provider class. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How are provider failures matched? — Picked: A — reason phrases plus 401/402/429/5xx codes only in `http|status|code|error` context; latest matching line wins; within a line 402 > 401 > 429 > 5xx. Alternatives: B — bare numbers anywhere. Why: bare 3-digit numbers (token counts, ids) would misclassify, and the newest error is the cause. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Where do the tests go? — Picked: A — extend `tests/test_security_audit_workflow_contract.py`. Alternatives: B — new `tests/test_security_audit_codex_stderr_tail.py` registered in `ci.yml`. Why: it already has the fake-Codex harness, runs in CI, and holds the codex-failure test that must change anyway (§5). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] The issue asks for an `agents.md` note only if the security-audit section describes failure output, which it does not. Add one? — Picked: A — add a one-sentence note to the security-audit bullet. Alternatives: B — no `agents.md` change. Why: §7 requires documenting failure-mode changes. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] How are input bounding and the 4 KiB cap applied? — Picked: A — read at most the last 64 KiB (drop the partial first line when larger), drop blank lines, count the 4096-byte budget over sanitized line content keeping the newest lines, truncate a single over-budget newest line to its first 4096 bytes. Alternatives: B — cap raw bytes before sanitizing. Why: the issue's "sanitize first, then cap"; dropping the partial line stops a boundary-cut token from escaping the prefix masks. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude from #5785; base branch main; security pass: run (`security_pass_skip.py`: no skip label).
