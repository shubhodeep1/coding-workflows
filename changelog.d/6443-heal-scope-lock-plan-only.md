<!-- changelog: security -->
- **Heal-evidence implementations now derive their scope lock only from concrete files in the approved plan.** Issue-body failure evidence can no longer supply an allowlist.

The implement workflow ignores `files_touched:` blocks in heal issue bodies, including copied failure logs. It rejects directory and glob entries from the plan, as well as paths that can change the scope guard. When no valid plan path remains, the existing heal-evidence scope lock blocks the run rather than accepting an unbounded change.

What this means for operators: heal issues with no valid concrete plan files remain blocked for review under `ai:scope-blocked`; ordinary issue scope handling is unchanged.
