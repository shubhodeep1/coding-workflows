<!-- changelog: fixed -->
- **Consumer workflow updates now send the existing Telegram notification when only changelog assets or assembled fragments changed.**

The `Update Workflows` workflow now uses the same managed-change conditions for commits and notifications. Changelog asset updates list their changed paths, while fragment assembly reports the fragment count and detected changelog layout. The existing `SILENT` default, helper fallback, unchanged notification categories, and true no-op behavior are preserved.

What this means for operators: changelog-only automated commits are no longer silent when workflow-update notifications are enabled.
