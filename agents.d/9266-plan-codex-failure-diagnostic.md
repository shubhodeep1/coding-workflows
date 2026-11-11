<!-- agents: section="Stable log prefixes (contractual)" -->
- `RUN_PLAN_CODEX_FAILED` (`.github/workflows/plan.yml` "Run Codex planning", as `::error::RUN_PLAN_CODEX_FAILED rc=<exit code> exception=<line>|none`; printed only when `scripts/run_plan_codex.sh` exits non-zero. `exception` is the last runner stderr line matching `^[[:space:]]*[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Exit|Interrupt|Warning)\b`, with control characters removed, `%` encoded as `%25` and cut to 300 characters. The step's exit code, stdout and stderr are unchanged; when `mktemp` fails the runner runs as before with no line. Issue #9266)

LOG_PREFIX.name=RUN_PLAN_CODEX_FAILED
