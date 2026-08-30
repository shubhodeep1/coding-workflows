<!-- changelog: changed -->
- **CI now runs deterministic shared shell-block policy guards immediately after checkout.** Guard failures stop the job before Python setup, dependency installation, lint, and tests consume runner time.

The `Shared shell-block anti-regression checks` step retains its existing Codex configuration, memory bootstrap, Telegram helper, and watchdog-helper policies. Rejections now emit the secret-safe `CI_GUARD_FAILURE` diagnostic with the guard, check, file, line, expected policy, and policy-specific scanned-file set. The lint job keeps its existing four-way orchestrator-poll sharding and 45-minute timeout.

| The numbers that matter | Value |
| --- | --- |
| Guard position | Immediately after checkout |
| Diagnostic fields | 6 |
| Lint job timeout | 45 minutes |

What this means for contributors: deterministic policy drift now fails early with enough context to identify the affected helper and file without exposing the matched source line.
