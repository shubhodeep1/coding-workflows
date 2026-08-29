<!-- changelog: fixed -->
- **The AI Plan template-path guard no longer rejects plans that edit or create consumer-owned `scripts/` files.** Runtime-fetched helpers remain protected as template-owned.

The `Guard plans targeting template-owned paths` step in `.github/workflows/plan.yml` blanket-rejected every plan whose Files section mentioned `scripts/`, while the commit step it protects (`scripts/implement_commit_changes.sh`) excludes only runtime-fetched helpers. The mismatch put valid consumer plans into a planning retry loop, seen on shubhodeep1/drhyg_ecommerce_automation issue 25, where a fix to the consumer-tracked validation entry script was rejected five times in a row by stall-recovery re-triggers. The guard now rejects a `scripts/` path only when it is named in the bootstrap-generated manifest or is an existing untracked runtime artifact, allows both tracked consumer files and planned additions, preserves backtick-quoted script paths containing spaces while scanning the Files section, keeps the blanket rejection for `.github/prompts/` and `.github/scripts/`, and falls back to blanket-rejecting `scripts/` when the manifest was not generated.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/plan.yml` |
| Commit-step contract mirrored | `scripts/implement_commit_changes.sh` |
| Stalled consumer issue observed | shubhodeep1/drhyg_ecommerce_automation#25 |

What this means for consumer repos: plans that edit or create consumer-owned files under `scripts/` (validation entry scripts, security checks) proceed to implementation instead of failing the AI Plan run, and genuinely template-owned targets are still routed back with a rejection comment.
