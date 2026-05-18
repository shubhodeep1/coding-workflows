# Scripts Pending Removal

Centralized registry of single-use scripts, long-running scripts, and
long-running supervisors that have a known future-removal condition.

Mandated by **§18.F** (`CLAUDE.md`) and **§20.F**
(`unattended_system_instructions.md`).

This is **one** living registry — do not create per-script removal
docs. Entries are added when a script is introduced and **removed**
from this file when the script itself is removed from the codebase.
Git history is the audit trail; there is no "removed" archive section.

## Entry Schema

Each entry MUST include all of the following fields. Use the per-script
block format below — tables collapse multi-line preflight checks.

- **Script path** — the script, supervisor entry point, or workflow
  file the entry is about (repo-relative path).
- **Introduced in** — PR number and date the script landed.
- **Type** — one of `single-use`, `long-running`, `supervisor`.
- **Removal trigger** — the concrete condition that makes removal
  safe. Examples:
  - `after backfill users_email_lowercase completes for all docs`
  - `when feature flag new_pricing_v2 is GA (100% rollout) for 30 days`
  - `when supervisor_v2 replaces supervisor_v1 in production`
  - `permanent — review annually` (use only when no sunset applies; do
    not omit the field)
- **Removal preflight checks** — explicit list of checks that MUST
  pass before the script is removed, to verify the script has already
  done its job. Each check names the exact command, query, or signal
  to inspect and the expected result / threshold. These checks protect
  against removing a script that hasn't finished its work.
- **Owner** — GitHub handle of the person / agent who owns the
  removal decision.

## Template

Copy this block when adding a new entry:

```markdown
### `path/to/script-or-workflow`

- **Introduced in:** #NNNN (YYYY-MM-DD)
- **Type:** single-use | long-running | supervisor
- **Removal trigger:** <concrete condition>
- **Removal preflight checks:**
  - `<exact command / query>` returns `<expected result>`.
  - `<signal to inspect>` shows `<expected threshold>`.
- **Owner:** @handle
```

## Entries

_No entries yet._
