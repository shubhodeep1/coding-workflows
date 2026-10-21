<!-- changelog: changed -->
- **Five test steps moved out of the CI job `tests-hooks-and-orchestrator`.** That job took 15m06s of its 20-minute timeout on `main` (run 39575950300, budget-watch issue #7798). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Isolated agent container tests (codex_isolated_exec.sh)` | `orchestrate-poll (2)` |
  | `Prebuilt sandbox image tests` | `orchestrate-poll (2)` |
  | `Review sandbox image build retry tests` | `orchestrate-poll (2)` |
  | `Set up Node 22 for the claude-pool-broker Worker tests` | `tests-release-and-log-analysis` |
  | `claude-pool-broker Worker tests (vitest)` | `tests-release-and-log-analysis` |

  The three steps in the `orchestrate-poll` matrix carry `if: strategy.job-index == 2`, so they run in group 2 only. The Node setup still runs right before the vitest step. The run's step timings could not be read, and the implement sandbox could not time every candidate (no network for `npm ci`), so these are the plan's fallback list, chosen by estimated cost. The two target jobs were estimated at about 12.5-13 minutes (`orchestrate-poll (2)` after #7697) and 12-13 minutes (`tests-release-and-log-analysis` after #7699). None of these durations has been measured; the next `budget-watch` run on `main` checks every job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `orchestrate-poll (2)` or `tests-release-and-log-analysis`. Operators have nothing to do.
