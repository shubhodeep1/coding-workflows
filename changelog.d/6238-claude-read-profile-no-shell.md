<!-- changelog: security -->
- **Claude read-only roles no longer have shell access.** The review-blocked judge's verdict pass now uses the read profile rather than the write profile.

The shared read profile allows only `Read`, `Grep` and `Glob`; command-prefix permissions for `git` and `gh` could still write runner files. `AI_ENGINE_READ_ONLY=true` now narrows any Claude role to that profile without affecting the judge's write-capable fix pass. The security-pass exhaustion judge can verify cited files without shell access.

What this means for operators: Claude verdict passes cannot run shell commands, while the separate fix pass retains its write tools.
