<!-- changelog: fixed -->
- **Bind Claude project security and validation gates to their target branches.** Audit waits now verify the dispatched run and pinned branch head, require zero surviving findings, and confirm security follow-up merges. Issue-mode validation can authorize a non-default base from trusted source-issue metadata; blocked Claude fixes wake the parent project.
