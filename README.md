# coding-workflows

Centralized reusable GitHub Actions workflows for AI-powered issue-to-PR automation.

## Overview

This repository contains reusable `workflow_call` workflows that power the full AI development pipeline:

1. **Clarify** — Determines if an issue has enough information, asks clarification questions if needed
2. **Plan** — Generates a structured implementation plan from clarified issues
3. **Implement** — Executes the approved plan and creates a pull request
4. **Review & Autofix** — Multi-model PR review with automated fix application
5. **Issue PR Status** — Syncs issue labels when PRs are merged/closed
6. **Cancel on PR Close** — Cancels orphaned workflow runs when PRs close
7. **Memory Maintenance** — Monthly compaction and archival of AI memory records

## Quickstart

Get AI-powered issue-to-PR automation running in your repository in a few minutes.

### 1. Add secrets and variables

In your consumer repository, go to **Settings → Secrets and variables → Actions** and configure:

#### Secrets

| Secret | Required | Used By | Description |
|---|---|---|---|
| `GH_PAT` | **Yes** | All workflows | GitHub Personal Access Token with `repo` scope |
| `OPENROUTER_API_KEY` | **Yes** | clarify, plan, implement, review_autofix | [OpenRouter](https://openrouter.ai) API key for LLM access |
| `TG_BOT_SECRET` | No | clarify, plan, implement, review_autofix | Telegram bot token for notifications |

#### Variables

| Variable | Required | Default | Used By | Description |
|---|---|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | No | `openai/gpt-5.3-codex` | clarify, plan, implement, review_autofix | Model for code editing tasks |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | No | `true` | plan | Auto-trigger implementation when plan is clear |
| `ALLOW_WORKFLOW_EDITS` | No | `false` | review_autofix | Allow AI edits to `.github/workflows` files |
| `TG_ADMIN_CHAT_ID` | No | — | clarify, plan, implement, review_autofix | Telegram chat ID for notifications (pair with `TG_BOT_SECRET`) |
| `SERENA_VERSION` | No | `main` | clarify, plan, implement, review_autofix | Version/branch of the Serena MCP server |
| `SERENA_LANGUAGES` | No | `""` (empty) | clarify, plan, implement, review_autofix | Languages for Serena symbol analysis |
| `SERENA_DISABLED` | No | `false` | clarify, plan, implement, review_autofix | Disable the Serena MCP server |

### 2. Create wrapper workflows

Add thin wrapper workflows in your repo's `.github/workflows/` directory. Reference implementations live in [`.github/workflows/internal-*.yml`](.github/workflows/) in this repository.

At minimum, create these three core wrappers:

**`.github/workflows/ai-clarify.yml`** — Triages new issues automatically
```yaml
name: AI Clarify
on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  clarify:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-plan.yml`** — Generates an implementation plan when you comment `/answer`
```yaml
name: AI Plan
on:
  issue_comment:
    types: [created]
permissions:
  contents: read
  issues: write
jobs:
  plan:
    uses: shubhodeep1/coding-workflows/.github/workflows/plan.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-implement.yml`** — Executes the plan and opens a PR when you comment `/approved`
```yaml
name: AI Implement
on:
  issue_comment:
    types: [created]
permissions:
  contents: write
  issues: write
  pull-requests: write
jobs:
  implement:
    uses: shubhodeep1/coding-workflows/.github/workflows/implement.yml@stable
    secrets: inherit
```

#### Optional wrappers

**`.github/workflows/ai-review.yml`** — Multi-model PR review with automated fixes
```yaml
name: AI Review
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: write
  pull-requests: write
  issues: write
jobs:
  review:
    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable
    with:
      allow_workflow_edits: ${{ vars.ALLOW_WORKFLOW_EDITS == 'true' }}
    secrets: inherit
```

**`.github/workflows/ai-issue-pr-status.yml`** — Syncs issue labels when PRs are merged/closed
```yaml
name: AI Issue PR Status
on:
  pull_request:
    types: [closed]
permissions:
  issues: write
jobs:
  status:
    uses: shubhodeep1/coding-workflows/.github/workflows/issue_pr_status.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-cancel-on-pr-close.yml`** — Cancels orphaned workflow runs when PRs close
```yaml
name: AI Cancel on PR Close
on:
  pull_request:
    types: [closed]
permissions:
  actions: write
jobs:
  cancel:
    uses: shubhodeep1/coding-workflows/.github/workflows/cancel_on_pr_close.yml@stable
    secrets: inherit
```

**`.github/workflows/ai-memory-maintenance.yml`** — Monthly compaction and archival of AI memory
```yaml
name: AI Memory Maintenance
on:
  schedule:
    - cron: '0 3 1 * *'
permissions:
  contents: write
jobs:
  maintenance:
    uses: shubhodeep1/coding-workflows/.github/workflows/memory_maintenance.yml@stable
    secrets: inherit
```

> All 7 wrapper reference implementations can be found in [`.github/workflows/internal-*.yml`](.github/workflows/).

### 3. Open an issue

Create a new issue describing a feature or bug fix. The pipeline kicks off automatically:

1. **Clarify** evaluates whether the issue has enough detail. If not, it comments with clarification questions.
2. Once the issue is clear, comment `/answer` to trigger **Plan** generation.
3. Review the plan, then comment `/approved` to start **Implementation** — a PR is created for you.

## Usage

Consumer repositories use thin wrapper workflows that call these reusable workflows:

```yaml
# .github/workflows/ai-clarify.yml
name: AI Clarify

on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]

permissions:
  contents: read
  issues: write

jobs:
  clarify:
    uses: shubhodeep1/coding-workflows/.github/workflows/clarify.yml@stable
    secrets: inherit
```

See `workflow-templates/` in consumer repos for all wrapper examples.

## Reusable Workflows

| Workflow | Trigger (in consumer) | Description |
|---|---|---|
| `clarify.yml` | `issues.opened`, `issue_comment.created` | Issue clarity detection |
| `plan.yml` | `issue_comment.created` (`/answer`) | Implementation plan generation |
| `implement.yml` | `issue_comment.created` (`/approved`) | Plan execution + PR creation |
| `review_autofix.yml` | `pull_request.*` | Multi-model review + autofix |
| `issue_pr_status.yml` | `pull_request.closed` | Label/state sync |
| `cancel_on_pr_close.yml` | `pull_request.closed` | Active-run cancellation |
| `memory_maintenance.yml` | `schedule` (monthly) | Memory compaction/archival |

## Required Secrets

| Secret | Used By | Description |
|---|---|---|
| `GH_PAT` | All workflows | GitHub PAT with repo access |
| `OPENROUTER_API_KEY` | clarify, plan, implement, review_autofix | OpenRouter API key for LLM access |
| `TG_BOT_SECRET` | clarify, plan, implement, review_autofix | Telegram bot token (optional) |

## Required Variables

| Variable | Default | Description |
|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` | Model for code editing tasks |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `false` | Allow AI edits to workflow files |

## Repository Structure

```
coding-workflows/
  .github/
    workflows/          # Reusable workflow_call workflows
    actions/
      setup-runtime/    # Shared composite action for runtime setup
  scripts/              # Helper scripts (memory, context, git)
  prompts/              # LLM prompt templates
  ai-memory/            # Memory schemas, config, and examples
  netwask/              # Agent configuration
  docs/                 # Documentation
```

## Versioning

- **Immutable tags**: `v1.0.0`, `v1.0.1`, etc.
- **Stable channel**: `@stable` — moving tag, updated after canary validation
- **Canary channel**: `@canary` — pre-stable testing

Consumer repos pin to `@stable` for automatic updates or exact tags for reproducibility.

## Contributing

1. Make changes in a feature branch
2. Test via canary channel on pilot repos
3. Promote to stable after validation

See `docs/release-policy.md` for the full release process.
