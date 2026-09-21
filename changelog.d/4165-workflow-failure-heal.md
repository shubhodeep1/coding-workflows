<!-- changelog: added -->
- **Human-needed escalations and failed releases now heal themselves.** When the pipeline labels an issue or pull request `ai:needs-human` (or a terminal latch such as `ai:check-triage-escalated`, `ai:destructive-blocked`, `ai:scope-blocked`, `ai:harness-broken`, `ai:resolver-escalated`, `ai:security-pass-failed`), or when a release / promotion workflow run fails in coding-workflows, a fix issue is opened for the same clarify → plan → implement → review pipeline instead of only an admin Telegram message.

Consumers get a new `ai-workflow-failure-heal.yml` wrapper (profile `full`, on by default) that reports the escalation to coding-workflows with the failed runs linked and the release pin recorded. The `Workflow Failure Heal Intake` workflow in coding-workflows fetches the failed job logs, diagnoses against the source at that release SHA, and routes by classification: shared-workflow defects become an `ai:workflow-heal` issue in coding-workflows targeting `stable`, so the fix ships as a hotfix through `auto-release-stable.yml` and reaches every consumer on the next sync; defects in the consumer's own code become an issue in that consumer; configuration problems and transient failures only alert and leave a comment on the escalated issue. Failures are fingerprinted so the same bug in many consumers is one issue with occurrence comments, recurrences are capped by a lineage generation, and open-issue and per-day budgets bound the volume.

| The numbers that matter | Value |
| --- | --- |
| Escalation labels that trigger a report | 7 |
| Release workflows reported on failure | 5 (`Test & Mark Stable Release`, `Mark Stable Release`, `Promote main to stable`, `Auto release stable`, `Forward-merge stable to main`) |
| Lineage cap per fingerprint (`WORKFLOW_HEAL_MAX_LINEAGE_DEPTH`) | 3 |
| Open-issue / per-day budgets (`WORKFLOW_HEAL_MAX_OPEN_ISSUES`, `WORKFLOW_HEAL_MAX_ISSUES_PER_DAY`) | 10 / 20 |
| Kill switch | `WORKFLOW_HEAL_ENABLED=false` |
| New labels | `ai:workflow-heal`, `ai:workflow-heal-escalated` |

What this means for operators: the Telegram feed still shows every escalation, but each one now also carries a link to the heal issue (or a diagnosis comment explaining why no code fix applies), and the fix arrives through the normal reviewed pipeline without anyone opening an issue by hand. Nothing new has to be configured: the consumer `GH_PAT` already reaches coding-workflows, and the wrapper arrives on the next `@stable` sync.

### For contributors

Shared logic lives in `scripts/workflow_failure_heal.py` (payload validation, error-signature fingerprinting, dedup / lineage / budget decisions, issue composition) and is exercised by `tests/test_workflow_failure_heal.py`, which also runs `scripts/workflow_failure_heal_report.sh` and `scripts/workflow_failure_heal_intake.sh` end to end against a mock `gh` and mock `codex`. Stable log prefixes are `WORKFLOW_HEAL_REPORT` (reporter) and `WORKFLOW_HEAL` (intake). Smoke-test fixtures and promote / auto-release runs that failed only because the smoke gate failed are skipped so the gate run's own report is the single record.
