<!-- changelog: fixed -->
- **Review editor sandbox failures now identify a safe rejected directory.** The isolated editor is told which paths it cannot access, and transfer still rejects attempts to create excluded directories.

The audit-plans command now matches its consumer template. If the editor tries to create a forbidden directory, the failure log includes a bounded relative directory name or `redacted`, without accepting the unsafe result. This helps diagnose sandbox-only test failures without weakening the transfer gate.
