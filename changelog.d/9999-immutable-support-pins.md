<!-- changelog: security -->
- **Consumer AI workflows now run support code only from the release commit their wrapper pins.** Pinned runs no longer fall back to `stable` or `main` for scripts, helper actions or the integration-ref resolver.

Consumer wrappers call the reusable workflows at a fixed release SHA, but ten of those workflows still checked out their support scripts from `stable`, fell back to `main` when that failed, and staged extra files from a `main` snapshot. All of that ran with `GH_PAT`, OpenRouter and Telegram secrets in the job. A run pinned to `<file>@<sha>` now checks out support code from that SHA only, verifies the checkout `HEAD` before staging anything, and fails the job instead of falling back. The integration-ref resolver follows the same pin, `install-codex` is referenced by commit instead of `@stable`, and the `ai-review.yml` wrapper's provider-outage support checkout is now rendered with the release SHA like its two sibling checkouts. Wrappers that still call `@stable` keep today's behaviour. This closes the support-ref findings in tele-funtoken-msg-scoring#4566.

| The numbers that matter | Value |
| --- | --- |
| Reusable workflows changed | 14 (10 support checkouts, 5 resolvers, `install-codex` pins) |
| Mutable fallbacks removed for pinned runs | `main` fallback, `main` snapshot, `main` resolver clone |
| `install-codex` pin | `91b30f6fba400686c5dded34423f88f1ea10e163` |

What this means for consumer repos: after the next sync, a compromised or broken `main` or `stable` can no longer change what a pinned AI run executes. A pinned run whose support checkout fails now stops with a clear error instead of silently running newer code.

### For contributors

`validate.yml` still calls `setup-runtime@stable` (its own `install-codex` reference is now pinned); a follow-up moves that pin to the commit carrying this change. `tests/test_workflow_support_ref_follows_pin.py` holds the contract.
