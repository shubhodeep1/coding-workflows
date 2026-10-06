<!-- changelog: security -->
- **Merge-train bypasses now require a verified queue history.** A queued comment only counts when the authenticated automation account wrote it, and a one-shot bypass requires a later queue-label removal by a collaborator with triage-or-higher access. Forged comments cannot skip the queue or be retired by automation.
