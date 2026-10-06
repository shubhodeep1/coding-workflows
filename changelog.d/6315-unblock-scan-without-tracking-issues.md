<!-- changelog: security -->
- **Blocked standalone issues and pull requests reach the unblock judge even without an open tracking project.** The scheduled orchestrator poll now scans blocked items on idle project ticks as well as after active projects are processed.

Previously, the unblock scan was skipped when no orchestrator tracking issue was open, leaving standalone blocked work waiting indefinitely. The idle-tick scan uses the existing cooldown, trusted-marker checks, and one-dispatch-per-tick limit. No new scheduler or credentials are required.
