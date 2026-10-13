<!-- agents: section="Workflow architecture" -->
Heal intake scope-marker guard and resolver path categories (issue #7059):
before filing any heal issue (`workflow-defect`, `inconclusive`,
`base-self-inflicted` or `consumer-app-defect`, all labelled
`ai:workflow-heal` and refused by implement preflight without a verified
marker), `scripts/workflow_failure_heal_intake.sh` checks its own rendered
scope marker with `workflow_failure_heal.py heal-scope check-marker` (the
implement-side parser and shape rules, 8 KiB read cap, no symlinks). A
missing or invalid marker files nothing: it logs
`WORKFLOW_HEAL skip reason=scope_marker_unresolved ... status=<status>`, sends
a CRITICAL alert and fails the run, so the next report of the fingerprint
retries. Only `WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED=false` (script
environment) restores filing. The conflict resolver's nameless
`sandbox_path_unsupported` stop is now preceded by one
`::error::Conflict resolver: conflicted path(s) cannot enter the sandbox:
unsafe=<n> host_only=<m> categories=<c,...> depths=<1|2|3+|none>` line, which
becomes the failure's First error. Categories come from a closed set
(`symlink`, `symlink_in_path`, `special_file`, `unsafe_name`, `unreadable`,
`other`) and no path name is printed; the per-path report writes unsafe
entries as `unsafe<TAB><category><TAB><depth>`. Admission rules and failure
reasons are unchanged.
