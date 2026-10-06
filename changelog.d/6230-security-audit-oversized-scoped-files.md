<!-- changelog: fixed -->
- Security audits no longer record a clean result when an explicitly scoped tracked file is omitted from the isolated agent's view.

Oversized scoped files are supplied as bounded read-only chunks, including prior-finding and fix-cycle files in full scans. A filtered scoped file or a file exceeding the export caps stops the audit before the model runs. Full scans continue to report other oversized files as a coverage note.

| Limit | Default |
| --- | --- |
| Read-only snapshot file threshold | 2 MiB |
| Scoped oversized per-file export cap | 16 MiB |
| Scoped oversized total export cap | 64 MiB |

What this means for operators: an audit that cannot inspect an explicitly scoped file fails instead of publishing a clean finding set; unscoped oversized files remain visible in the coverage note.
