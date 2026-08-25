# Autonomous Issue → Plan → Implementation Pipeline

## Overview

An autonomous AI development pipeline integrated with GitHub Issues and Actions.

Pipeline: Issue → Clarify → Plan → Approve → Implement → PR → Review/Autofix

Human interaction is limited to: answering clarification questions, approving plans, requesting re-clarification.

---

## Commands

Issued as **issue comments**:

| Command | Effect |
|---------|--------|
| `/answer` | User provides answers to clarification questions (must be first non-whitespace; multiline below supported; workflow may auto-post `/answer [auto-answered-by-clarify]` when clear) |
| `/approved` | User approves the implementation plan |
| `/reclarify` | User requests re-clarification |

---

## Pipeline Phases

```
Issue Created → Clarify → /answer → Plan → /approved → Implement → PR → Auto-Review
```

---

## Shared Runtime Behavior

1. **Ignore bot comments** — only run if `github.event.comment.user.type == 'User'`
2. **Prevent duplicates** — check for existing PR (`gh pr list --search "issue:<number>"`) before major steps; track processed commands via timestamps/markers
3. **Checkout** — `actions/checkout@v4` (clarify uses `fetch-depth: 1` since history is not needed; plan/implement use `fetch-depth: 0`)
4. **Workspace** — `/tmp/codex-issue-<run-id>` storing context files (`issue_context.txt`, `answers.txt`, `plan.txt`, `codex_output.txt`)
5. **Codex settings** — model `openai/gpt-5.6-sol`, no timeout, search off (match `review_autofix.yml` settings). `unattended_system_instructions.md` included in context for every phase.
6. **Telegram** — copy settings/API calls from `review_autofix.yml`. Alert on: clarification posted, plan awaiting approval, any failure. Skip alerts when auto-approval proceeds without human action.

---

## Phase 1 — Clarification

**Workflow:** `.github/workflows/ai-clarify.yml`
**Triggers:** `issues: [opened]`, `issue_comment: [created]` (for `/reclarify`)

Codex reads system instructions + issue context, then determines if the issue is implementable.

**If clarification needed:** post questions as issue comment, send Telegram alert.
**If clear:** post clear-status note + auto-post `/answer [auto-answered-by-clarify]` to trigger planning. No Telegram alert (no human action required).

---

## Phase 2 — Planning

**Workflow:** `.github/workflows/ai-plan.yml`
**Trigger:** `issue_comment: [created]` where comment starts with `/answer`

Context includes: issue body, clarification Q&A, full comment thread.

Codex generates a structured plan: files to change, functions to implement, data structures affected, edge cases, testing considerations.

Plan posted as comment with instructions to reply `/approved` or `/reclarify`. Telegram alert sent when awaiting human action; skipped on auto-approval.

---

## Phase 3 — Implementation

**Workflow:** `.github/workflows/ai-implement.yml`
**Trigger:** `issue_comment: [created]` containing `/approved`

Context includes: issue body, answers, approved plan, full comment thread.

Steps: implement plan → create branch `ai/issue-<number>` → commit → push → create PR referencing the issue.

The existing PR auto-review/autofix workflow handles review, fixes, and conflict resolution automatically.

---

## Failure Handling

On any workflow failure: post comment on issue, send Telegram alert, exit safely.

---

## Summary

1. Issue clarification
2. Plan generation
3. Human approval
4. Automated implementation
5. PR review and autofix

Human involvement: answering questions and approving plans. Codex handles the rest.
