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

**Thinking levels** — control the model's reasoning effort per phase. Valid values: `xhigh`, `high`, `medium`, `low`. Defaults are tuned per phase: `medium` for clarify (gap analysis doesn't need deep reasoning), `xhigh` for plan (architectural decisions benefit from maximum reasoning), `high` for implement (follows an existing plan), and `xhigh` for review (last line of defense for catching bugs).

| Variable | Default | Used By | Description |
|---|---|---|---|
| `THINKING_LEVEL_CLARIFY` | `medium` | clarify | Reasoning effort for the clarification phase |
| `THINKING_LEVEL_PLAN` | `xhigh` | plan | Reasoning effort for the planning phase |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | implement | Reasoning effort for the implementation phase |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | review_autofix | Reasoning effort for the reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `high` | review_autofix | Reasoning effort for the editor model (applying fixes) |

**Tool call budgets** — soft limits on the number of MCP + shell tool calls per phase. The LLM treats these as guidelines; it may exceed them for large refactors that span many files.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | clarify | Tool call budget for the clarification phase |
| `TOOL_CALL_BUDGET_PLAN` | `40` | plan | Tool call budget for the planning phase |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | implement | Tool call budget for the implementation phase |

**Token warning thresholds** — when a phase exceeds this many tokens, a warning appears in the GitHub Actions run summary. Raise these for large repos where deeper exploration is expected.

| Variable | Default | Used By | Description |
|---|---|---|---|
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | clarify | Token usage warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | plan | Token usage warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | implement | Token usage warning threshold for implementation |

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

> All internal wrapper reference implementations can be found in [`.github/workflows/internal-*.yml`](.github/workflows/).

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

See [`workflow-templates/`](workflow-templates/) in this repository for ready-to-copy caller wrappers.

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
| `orchestrate.yml` | `workflow_dispatch` | Project decomposition + multi-issue orchestration |
| `orchestrate_poll.yml` | `schedule` (every ~10 min) | Orchestrator progress poller + judge + auto-recovery |

## Required Secrets

| Secret | Used By | Description |
|---|---|---|
| `GH_PAT` | All workflows | GitHub PAT with repo access |
| `OPENROUTER_API_KEY` | clarify, plan, implement, review_autofix, orchestrate | OpenRouter API key for LLM access |
| `TG_BOT_SECRET` | clarify, plan, implement, review_autofix, orchestrate | Telegram bot token (optional) |

## Required Variables

| Variable | Default | Description |
|---|---|---|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` | Model for code editing tasks |
| `TG_ADMIN_CHAT_ID` | — | Telegram chat ID for notifications |
| `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `true` | Auto-approve clear plans |
| `ALLOW_WORKFLOW_EDITS` | `false` | Allow AI edits to workflow files |
| `AI_MEMORY_BRANCH` | `ai-memory` | Branch used for persistent AI memory |
| `AI_MEMORY_ROOT` | `ai-memory` | Memory root path used by workflows |
| `AI_MEMORY_RETRIEVAL_PROFILES` | `ai-memory/config/retrieval_profiles.v1.json` | Retrieval role config |
| `AI_MEMORY_ENABLED` | `true` | Enable/disable memory operations |
| `THINKING_LEVEL_CLARIFY` | `medium` | Reasoning effort for clarification (`xhigh`, `high`, `medium`, `low`) |
| `THINKING_LEVEL_PLAN` | `xhigh` | Reasoning effort for planning |
| `THINKING_LEVEL_IMPLEMENT` | `xhigh` | Reasoning effort for implementation |
| `THINKING_LEVEL_REVIEWER` | `xhigh` | Reasoning effort for reviewer models (bug detection) |
| `THINKING_LEVEL_EDITOR` | `high` | Reasoning effort for editor model (applying fixes) |
| `TOOL_CALL_BUDGET_CLARIFY` | `15` | Tool call budget for clarification |
| `TOOL_CALL_BUDGET_PLAN` | `40` | Tool call budget for planning |
| `TOOL_CALL_BUDGET_IMPLEMENT` | `50` | Tool call budget for implementation |
| `TOKEN_WARN_THRESHOLD_CLARIFY` | `80000` | Token warning threshold for clarification |
| `TOKEN_WARN_THRESHOLD_PLAN` | `200000` | Token warning threshold for planning |
| `TOKEN_WARN_THRESHOLD_IMPLEMENT` | `200000` | Token warning threshold for implementation |
| `WORKFLOW_ORCHESTRATE_MODEL` | (falls back to `WORKFLOW_EDITOR_MODEL`) | Model override for orchestrator/judge |
| `THINKING_LEVEL_ORCHESTRATE` | `xhigh` | Reasoning effort for project decomposition |
| `THINKING_LEVEL_JUDGE` | `xhigh` | Reasoning effort for judge evaluation |
| `ORCHESTRATE_POLL_INTERVAL` | `10` | Minutes between orchestrator poll checks |
| `TOOL_CALL_BUDGET_ORCHESTRATE` | `40` | Tool call budget for decomposer |
| `TOOL_CALL_BUDGET_JUDGE` | `60` | Tool call budget for judge (needs deep repo inspection) |
| `TOKEN_WARN_THRESHOLD_ORCHESTRATE` | `200000` | Token warning threshold for orchestration |

## Project Orchestrator

The orchestrator enables complex, multi-issue projects from a single prompt. It decomposes a project description into a dependency-aware DAG of GitHub issues, dispatches them through the existing AI pipeline in waves, and uses a judge to validate results between waves.

### Architecture

```
workflow_dispatch (project description)
    → Decomposer (LLM): breaks project into issues + dependency DAG
    → Creates tracking issue + child issues
    → Wave 1 issues enter pipeline (clarify → plan → implement → review → merge)
    → Poller (scheduled): monitors progress, dispatches next waves
    → Judge (LLM, xhigh thinking, full repo checkout): evaluates after each wave
        → complete: close tracking issue
        → in_progress: create fix-up issues, advance to next wave
        → failed: auto-recovery (revert + re-plan, retry once), then stop
```

### Setup

**1.** Copy the two wrapper workflows from [`workflow-templates/`](workflow-templates/) into your consumer repo's `.github/workflows/` directory:

- [`ai-orchestrate.yml`](workflow-templates/ai-orchestrate.yml) — triggers decomposition via `workflow_dispatch`
- [`ai-orchestrate-poll.yml`](workflow-templates/ai-orchestrate-poll.yml) — scheduled poller (every 10 min)

Or create them manually — see the inline examples in the [Quickstart](#quickstart) section above.

**2.** Ensure your repo has the required secrets (`GH_PAT`, `OPENROUTER_API_KEY`) and optionally configure the orchestrator variables listed in [Required Variables](#required-variables).

**3.** Go to **Actions → AI Orchestrate → Run workflow**, paste your project description, and click **Run workflow**.

### How it works

1. **Decomposition:** The LLM reads your repo, breaks the project into scoped issues with a dependency graph, and creates a tracking issue (labeled `ai:orchestrator-tracking`).
2. **Wave dispatch:** Wave 1 issues (no dependencies) are created immediately and enter the existing clarify → plan → implement → review pipeline automatically.
3. **Auto-merge:** The poller automatically merges PRs via squash merge when they reach `ai:ready-to-merge`. This requires either (a) no branch protection rules, or (b) branch protection with "Require status checks" that have already passed. See [Enabling auto-merge](#enabling-auto-merge) below.
4. **Polling:** Every 10 minutes, the poller checks if the current wave's issues have reached `ai:merged`. When all are merged, it runs the judge.
5. **Judge:** Full repo checkout + tool access (Serena, shell, file reads) with `xhigh` thinking. Compares merged code against the project spec. Decides: complete, in_progress (next wave or fix-ups), or failed.
6. **Next wave:** When the judge approves, the poller creates the next wave's issues (deferred creation — they don't exist until their dependencies are met). This triggers `clarify.yml` via `issues.opened`.
7. **Auto-recovery:** On failure, the judge can revert problematic PRs and create fix-up issues. Recovery is attempted once; if it still fails, the project stops and the operator is notified via Telegram.
8. **Completion:** When the judge says "complete", the tracking issue is closed.

### Enabling auto-merge

For fully hands-off operation, the poller needs to be able to merge PRs. There are three options depending on your repo's branch protection setup:

**Option A: No branch protection (simplest)**
The poller merges PRs directly with `gh pr merge --squash`. No extra config needed.

**Option B: Branch protection with auto-merge enabled**
1. Go to **Settings → General → Pull Requests** and check **Allow auto-merge**.
2. The poller uses `gh pr merge --squash --auto`, which queues the PR for merge once all required status checks pass.
3. Your `GH_PAT` must have permission to enable auto-merge.

**Option C: Branch protection with required reviews**
If your branch protection requires human reviews, the orchestrator cannot fully auto-merge. The poller will attempt to merge but log a warning. You'll need to manually approve PRs or add the bot account as a bypass actor in branch protection settings.

### Labels

The orchestrator uses `ai:orchestrator-tracking` for tracking issues. Child issues use the standard `ai:*` phase labels. The `ai:orchestrator-tracking` label is defined in the [label contract](/.github/ai/label_contract.v1.json).

## Repository Structure

```
coding-workflows/
  .github/
    workflows/          # Reusable workflow_call workflows
    actions/
      setup-runtime/    # Shared composite action for runtime setup
    ai/                 # AI config: label contract, orchestrate schema
  scripts/              # Helper scripts (memory, context, git, orchestrator)
  prompts/              # LLM prompt templates (clarify, plan, orchestrate, judge)
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
