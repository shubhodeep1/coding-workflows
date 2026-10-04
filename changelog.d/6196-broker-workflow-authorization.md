<!-- changelog: security -->
- **The Claude pool broker now restricts token access to approved reusable workflows and protected-branch commits.** It rejects unknown workflow files, unapproved refs, mismatched release tags or SHA pins, and unavailable authorization evidence instead of returning the pool.
