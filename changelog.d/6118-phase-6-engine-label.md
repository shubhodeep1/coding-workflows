<!-- changelog: added -->
- **`ai-orchestrate.yml` takes an optional `engine` input, and the project's engine label now follows its work to every issue and PR.** Dispatching with `engine=claude` puts the whole project on Claude Opus 5.5 at `high` effort once its roles are cut over; `engine=codex` pins it to codex.

Phase 6 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` propagates `ai:engine-claude` / `ai:codex` through project work; role cutovers are a separate Phase 5 task. The orchestrator turns its `engine` input into a label on the tracking issue and the wave-1 issues, and rejects any value other than empty, `claude` or `codex` before running the decomposer. The poller copies the tracking issue's engine label onto every issue and PR it creates (all 15 creation sites), and `implement.yml` copies an issue's label onto its PR. When both labels are present, `ai:codex` wins and is the one copied. `/implement-plan-claude` dispatches with `engine=claude`; against a wrapper without the input it dispatches without it and labels the tracking issue afterwards, so wave-1 issues created before that write do not inherit the label.

| The numbers that matter | Value |
| --- | --- |
| Poller issue and PR creation sites that copy the label | 15 |
| Accepted `engine` values | empty, `claude`, `codex` |
| New GitHub API calls | 0 |

What this means for operators: `gh workflow run ai-orchestrate.yml -f engine=claude …` starts a Claude project in one step, with no labelling afterwards. Leaving `engine` empty behaves exactly as before.

### For contributors

New log prefixes: `AI_ENGINE_PROJECT_LABEL` (orchestrate) and `AI_ENGINE_PR_LABEL` (implement). The poller helper `engine_label_create_args` reads `TRACKING_LABELS` (or a labels JSON argument) and prints `--label <name>` lines for `mapfile`; it prints nothing on an empty or unreadable list. `tests/test_engine_label_propagation.py` covers the mapping, every creation site, early input validation and the helper, and runs in its own `ci.yml` step. The consumer permission guard now recognizes vetted literal-ID read loops without prompting; unvetted loops remain undecided and writes still ask. Reusing an existing implementation PR applies the issue's engine label or fails the run.
