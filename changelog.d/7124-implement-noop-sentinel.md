<!-- changelog: fixed -->
- **An implementation run whose plan needs no code change now closes as done instead of failing after five attempts.** The implement prompt names the exact `No changes needed.` line the workflow accepts, and says `BLOCKED:` is only for real obstacles.

Run 38082111776 for issue #6812 spent five editor attempts ($14.41) on a verification-only plan whose fix was already on `main`. Each attempt correctly made no edit but said so in prose ("This is reported as already done"). The success-no-op check in "Run Codex implementation" only accepts a line such as `No changes needed.`, and the inline prompt the workflow sends never named that line. Both the prompt's completeness contract and the retry nudge now give that one exception. The regex is unchanged.

What this means for operators: a verification-only or already-done issue closes through the existing no-op path on its first attempt, so it no longer burns the retry budget and files an implementation-failure alert.
