<!-- changelog: security -->
- **Unblock judge no longer reads a failed run's logs solely because an item comment links to it.** It now checks up to three pipeline-cited run IDs against repository and item metadata before including a log in the model prompt; a matching issue title alone is insufficient, and unmatched or unreadable runs are omitted.

For maintainers: `UNBLOCK_JUDGE op=run_log` reports whether a run was attached or omitted and why. The judge still proceeds without logs when no run can be verified.
