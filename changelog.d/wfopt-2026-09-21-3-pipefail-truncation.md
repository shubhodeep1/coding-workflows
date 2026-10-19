<!-- changelog: fixed -->
- **Long input no longer aborts a step where only a truncated excerpt was wanted.** Under `set -o pipefail`, `printf ... | head -c N` and `cat FILE | head -c N` fail when the writer gets SIGPIPE, which happens once the input is larger than the pipe buffer (about 64 KiB). A tracking issue body that large stopped `scripts/review_conflict_prepare.sh` before the conflict resolver ran, and a large plan comment stopped the plan workflow's memory-record steps.

The capped sites in `.github/workflows/plan.yml`, `scripts/review_conflict_prepare.sh`, `scripts/review_apply_fixes.sh` and `scripts/workflow_failure_heal_intake.sh` now read the file directly, use a here-string, or use process substitution, so oversized input is truncated instead of aborting. Output for input below the caps is byte-identical. Refs `analysis/workflow-optimization-2026-09-21-3.md` (SHELL-001).

What this means for consumer repos: nothing to do; the change arrives with the next `@stable` sync.
