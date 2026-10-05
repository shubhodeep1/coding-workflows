<!-- changelog: security -->
- **The orchestrator no longer closes a project based solely on `ai:unblock-closed`.** It requires the unblock judge's latest trusted close verdict for that project, bound to the current blocked state; failed close requests can still be retried.
