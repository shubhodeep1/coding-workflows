# Autonomous Issue → Plan → Implementation Pipeline

## Overview

This document defines the implementation plan for an autonomous AI development pipeline integrated with GitHub Issues and GitHub Actions.

The system enables Codex to:

1. Read a GitHub Issue
2. Ask clarification questions
3. Receive answers
4. Generate an implementation plan
5. Wait for approval
6. Implement the solution
7. Create a Pull Request
8. Trigger the existing PR review + autofix workflow

The system uses **comment commands instead of labels** to control workflow state.

Human interaction is intentionally limited to:

- answering clarification questions
- approving the implementation plan
- requesting re-clarification

---

# Commands

The following commands control the pipeline.

| Command | Meaning |
|-------|------|
| `/answer` | User provides answers to clarification questions; command must be first non-whitespace content and answers may be multiline below it (workflow may also auto-post `/answer [auto-answered-by-clarify]` when no clarification is required) |
| `/approved` | User approves the implementation plan |
| `/reclarify` | User requests clarification stage again |

Commands are issued as **issue comments**.

---

# Pipeline Phases

The pipeline consists of three primary phases.

```
Issue Created
     │
     ▼
Phase 1: Clarification
     │
     ▼
User or bot /answer
     │
     ▼
Phase 2: Planning
     │
     ▼
User /approved
     │
     ▼
Phase 3: Implementation
     │
     ▼
Pull Request Created
     │
     ▼
Existing PR Auto-Fix Workflow
```

---

# Repository Components

The following components must be created.

## Workflows

```
.github/workflows/ai-clarify.yml
.github/workflows/ai-plan.yml
.github/workflows/ai-implement.yml
```

## Documentation

```
codex_system_instructions.md
ai_pipeline.md   (this document)
```

Codex must load **codex_system_instructions.md** during every execution.

---

# Shared Runtime Behavior

Each workflow must follow these shared behaviors.

## 1. Ignore Bot Comments

Workflows must ignore comments created by bots.

Only run if:

```
github.event.comment.user.type == 'User'
```

---

## 2. Prevent Duplicate Executions

Before running major steps:

- check if a PR already exists for the issue
- check if the command has already been processed

Example guard:

```
gh pr list --search "issue:<issue_number>"
```

If a PR already exists, exit.

---

## 3. Repository Checkout

Each workflow must begin with:

```
actions/checkout@v4
fetch-depth: 0
```

---

## 4. Runtime Workspace

Each workflow should create a runtime workspace under `/tmp`.

Example:

```
/tmp/codex-issue-<run-id>
```

This workspace stores:

```
issue_context.txt
answers.txt
plan.txt
codex_output.txt
```

---

# Phase 1 — Clarification

## Purpose

Determine whether the issue contains sufficient information to implement the task.

If not, Codex asks clarification questions.

---

## Workflow

```
.github/workflows/ai-clarify.yml
```

---

## Triggers

```
issues:
  types: [opened]

issue_comment:
  types: [created]
```

Clarification should run when:

1. a new issue is created
2. the `/reclarify` command is issued

---

## Workflow Logic

Steps executed by the workflow:

1. Checkout repository
2. Fetch issue metadata
3. Fetch all issue comments
4. Build issue context
5. Run Codex
6. Post clarification questions
7. Send Telegram alert

---

## Context Construction

The workflow must build a context file.

Example:

```
ISSUE DESCRIPTION
<issue body>

ISSUE COMMENTS
<all comments in chronological order>
```

Saved as:

```
issue_context.txt
```

---

## Codex Execution

Codex must:

- read `codex_system_instructions.md`
- read `issue_context.txt`

Codex must determine:

- whether the issue is implementable
- what clarification questions are needed

---

## Output Handling

Two possible outcomes.

### Case 1 — Clarification Needed

Codex outputs clarification questions.

Workflow posts comment containing the questions.

Telegram alert is sent notifying maintainers.

---

### Case 2 — Issue Clear

Codex determines the issue is clear.

Workflow posts a clear-status note and auto-posts an `/answer` command comment so planning starts immediately without waiting for a human reply.

Example comments:

```
The task appears clear.

/answer [auto-answered-by-clarify]
```

No Telegram alert is sent for this clear/no-question auto-continue path because no human action is required.

---

# Phase 2 — Planning

## Purpose

Create a detailed implementation plan before coding begins.

The plan must be approved by a human before proceeding.

---

## Workflow

```
.github/workflows/ai-plan.yml
```

---

## Trigger

```
issue_comment:
  types: [created]
```

Run when the latest comment is an `/answer` command comment where `/answer` appears as the first non-whitespace content (multiline answers below `/answer` are supported). This includes trusted workflow-generated comments containing `[auto-answered-by-clarify]`.

---

## Workflow Logic

Steps:

1. Checkout repository
2. Fetch issue metadata
3. Fetch all issue comments
4. Extract clarification answers
5. Build planning context
6. Run Codex
7. Post implementation plan
8. Send Telegram alert when awaiting human action; skip it when auto-approval is posted

---

## Context Construction

Context must contain:

```
ISSUE DESCRIPTION
<issue body>

CLARIFICATION QUESTIONS
<questions previously asked>

USER ANSWERS
<latest /answer comment>

ALL COMMENTS
<full issue thread>
```

Saved as:

```
planning_context.txt
```

---

## Codex Planning Behavior

Codex must generate a structured plan including:

- files likely to change
- functions to implement
- data structures affected
- potential edge cases
- testing considerations

The output should represent a **clear implementation strategy**.

---

## Plan Approval Comment

The workflow posts the generated plan.

The comment must instruct the user to respond with:

```
/approved
```

or

```
/reclarify
```

Example:

```
Implementation Plan

<plan output>

To proceed with implementation reply:

/approved

To restart clarification reply:

/reclarify
```

---

# Phase 3 — Implementation

## Purpose

Implement the approved plan and create a Pull Request.

---

## Workflow

```
.github/workflows/ai-implement.yml
```

---

## Trigger

```
issue_comment:
  types: [created]
```

Run only when comment contains:

```
/approved
```

---

## Workflow Logic

Steps executed:

1. Checkout repository
2. Fetch issue metadata
3. Fetch issue comments
4. Extract implementation plan
5. Build implementation context
6. Run Codex
7. Create new branch
8. Commit changes
9. Push branch
10. Create Pull Request

---

## Context Construction

Implementation context must contain:

```
ISSUE DESCRIPTION
<issue body>

CLARIFICATION ANSWERS
<answers provided by user>

APPROVED IMPLEMENTATION PLAN
<plan generated by Codex>

ISSUE COMMENTS
<full comment thread>
```

Saved as:

```
implementation_context.txt
```

---

## Branch Creation

Branch naming convention:

```
ai/issue-<issue-number>
```

Example:

```
ai/issue-42
```

---

## Commit Message

Standard commit format:

```
AI implementation for issue #<issue-number>
```

---

## PR Creation

The workflow must create a PR targeting the default branch.

PR title:

```
AI implementation for issue #<issue-number>
```

PR body must reference the issue.

Example:

```
Automated implementation for issue <issue-link>
```

---

# Integration with Existing PR Workflow

Once the PR is created:

The existing workflow:

```
Codex PR Self-Healing Semantic Agent
```

will automatically run and:

- review the PR
- fix issues
- resolve merge conflicts
- finalize the code

No modifications to the existing PR autofix system are required.

---

# Telegram Notifications

Telegram alerts must be sent for:

| Event | Message |
|------|--------|
| Clarification questions posted | clarification required |
| Implementation plan posted | awaiting approval |
| Workflow failure | pipeline failure |

Telegram message to be sent on any failure in any workflow.

---

# Failure Handling

If a workflow fails:

1. Post a comment on the issue
2. Send Telegram failure alert
3. Exit safely

---

# Safety Mechanisms

The system must include safeguards:

## Prevent duplicate PRs

Before implementation:

```
gh pr list --search "issue:<issue-number>"
```

If PR exists → exit and send a telegram message.

---

## Prevent command loops

Ignore commands posted by bots.

---

## Ensure commands are processed once

Each workflow must track processed commands using:

- comment timestamps
- or command markers

---

## Codex settings for all phases

Codex to be run with model openai/gpt-5.3-codex, no timeout and search turned off (copy settings from the editor process in .github/workflows/ai-auto-review-and-edit.yml)

codex_system_instructions.md is to be included in the context for codex for every phase and Codex is to be prompted to read and follow this strictly before doing anything in each phase.

## Telegram messaging settings

For Telegram messaging in all workflows, copy the same settings and api calls as .github/workflows/ai-auto-review-and-edit.yml

# Future Extensions

Planned improvements (not required for initial implementation):

- automatic plan validation
- automated test generation
- multi-agent planning
- CI simulation before PR creation

---

# Summary

The system introduces an autonomous development workflow:

1. Issue clarification
2. Plan generation
3. Human approval
4. Automated implementation
5. PR review and autofix

Human involvement is limited to answering questions and approving plans, while Codex performs the remaining development tasks.
