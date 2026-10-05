<!-- changelog: fixed -->
- **Check-failure triage now keeps PR-controlled prompts and checkout credentials out of the diagnosis.** It renders instructions from trusted workflow support, runs Codex read-only, and redacts credentials from the issue body before posting. Older consumer workflows retain a compatibility fallback when they have not yet exported the trusted support directory.
