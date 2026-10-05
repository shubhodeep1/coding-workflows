<!-- changelog: fixed -->
- **A failed isolated review fix cannot push a partial host edit.** Sandbox result transfer validates destination parents, stages payloads and rolls back host writes if applying them fails. The review-blocked judge refuses to commit, push or mark a PR merged when its Claude fix or transfer fails.
