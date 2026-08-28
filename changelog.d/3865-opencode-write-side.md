<!-- changelog: changed -->
- **Review/autofix now runs its complete model pipeline on OpenCode.** The editor, review-blocked judge and judge-fix path, consolidator, and merge-conflict resolver use isolated reviewer/writer configurations while preserving retries, fallback models, watchdogs, ledgers, write guards, and fingerprint gates.

The reusable review workflow no longer installs Codex or creates and mutates a shared Codex configuration. Existing `CODEX_*` compatibility identifiers and watchdog helper names remain unchanged, and a requested thread-reuse path now logs its documented fallback to a fresh full prompt. OpenCode failures never fall back to Codex; they emit the stable `opencode_agent_failure` alert, while the advisory consolidator retains its existing fail-open empty-artifact behavior.

Reverting this P3 change restores the Codex installation and write-side invocation paths without reverting the P2 OpenCode reviewer and summariser cutover. Do not propagate this change to `@stable` until the documented three-run review parity hold completes.
