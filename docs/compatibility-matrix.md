# Compatibility Matrix

## Workflow Version Compatibility

| Workflow | Min Consumer Version | Notes |
|---|---|---|
| `clarify.yml` | v1.0.0 | Initial release |
| `plan.yml` | v1.0.0 | Initial release |
| `implement.yml` | v1.0.0 | Initial release |
| `review_autofix.yml` | v1.0.0 | Initial release |
| `issue_pr_status.yml` | v1.0.0 | Initial release |
| `cancel_on_pr_close.yml` | v1.0.0 | Initial release |
| `memory_maintenance.yml` | v1.0.0 | Initial release |

## Required GitHub Features

- GitHub Actions with `workflow_call` support
- GitHub PAT with `repo` scope
- Repository labels (auto-created by workflows)

## Required External Services

| Service | Required | Purpose |
|---|---|---|
| OpenRouter API | Yes | LLM model access |
| Telegram Bot API | No | Optional notifications |

## Runner Requirements

- `ubuntu-latest` (Ubuntu 22.04+)
- Node.js 22 (installed by each workflow's setup steps)
- Python 3.12 (installed by each workflow's setup steps)
