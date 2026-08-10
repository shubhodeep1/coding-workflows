<!-- changelog: changed -->
- **`/investigate-issue` now lands upstream fixes itself when the library is attached to the session.** A consumer-repo session that also has `shubhodeep1/coding-workflows` checked out no longer stops at "open an upstream session" — it auto-chains into `/validate-consumer-issue` and ships the fix.

Until now, an `[UPSTREAM]` root cause found from a consumer repo always ended read-only: the command emitted a proposed diff pinned to the consumer's upstream SHA and told the operator to open a separate `shubhodeep1/coding-workflows` session to apply it. That hand-off existed because a consumer session could not push to the library. When both repos are attached to the same session, that is no longer true, so `/investigate-issue` now detects a pushable upstream working tree, finishes the investigation exactly as before, then chains into `/validate-consumer-issue`, which applies, verifies, commits, pushes, and opens the upstream PR in the attached checkout. With no upstream attached, both commands behave exactly as they did — the read-only hand-off is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Command templates changed | `workflow-templates/.claude/commands/investigate-issue.md`, `workflow-templates/.claude/commands/validate-consumer-issue.md` |
| Checks a checkout must pass to count as attached | 4 — is the library, clean tree, reachable `origin`, push permitted |
| Default landing branch for the upstream fix | `stable` (`main` only when the consumer pins `@main`) |
| New Evidence Ledger fields | `UPSTREAM_ATTACHED`, `UPSTREAM_CHECKOUT`, `UPSTREAM_BASE` |
| Behaviour when upstream is not attached | unchanged |

What this means for operators: attach `shubhodeep1/coding-workflows` alongside the consumer repo before running `/investigate-issue`, and an upstream defect comes back as a PR against `stable` instead of a diff to carry to another session. A `[BOTH]` root cause produces two PRs — the consumer one first, then the upstream one — never one commit spanning two repos. Nothing is attached automatically: if the library is absent, dirty, or not pushable, the run keeps the old read-only ending.

### For contributors

Attachment is detected, never created — the commands will not clone the library to unlock the write path, and a dirty upstream tree counts as not attached so in-flight work is never branched over. Diagnosis stays pinned to `UPSTREAM_SHA` even when a checkout is attached; the checkout is a write target only, and it sits at whatever ref it was cloned to. `UPSTREAM_BASE` resolves to `stable` for `@stable`, version-tag, and raw-SHA pins, since a tag is not a mergeable PR base — a tag-pinned fix is validated at the pinned SHA and re-verified against `stable` before push, and carries the usual port-to-`main` caveat. `/validate-consumer-issue` keeps full ownership of the decision to land: being chained grants no exemption from its `MISCONFIG` / `NOT-REPRODUCIBLE` / `INCORRECT` verdicts or the §6 and §10 gates. A rejected push restores the attached tree to its original branch and falls back to the read-only output rather than leaving a half-applied fix behind. Upstream PR bodies reference the consumer issue with `Refs owner/repo#N`; cross-repo auto-close keywords are forbidden (§19).
