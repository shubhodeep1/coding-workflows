<!-- changelog: fixed -->
- **The wave-dispatch fingerprint gate no longer blocks a project when the merge resolver correctly combines two edits to the same line.** Project #7355 stopped before wave 3 because issue #7358 (PR #7365) and a back-merge of `main` (#7376) both edited the regex line in `_is_gh_permanent_failure`, and the `[ai-merge-resolve]` commit kept both edits on one line.

`scripts/verify_integration_fingerprints.py` used to treat any change to a captured line made by a resolver commit as "sub-issue intent silently reverted". It now accepts such a change only when the line still contains the sub-issue's whole line, with nothing removed, and everything the resolver added comes from the other merge side's edit of that same line, which is itself kept whole. The gate logs `FINGERPRINT_RESOLVER_UNION_MERGE_V1` when it does. A resolver that dropped, changed or invented text still blocks the wave as before.

What this means for operators: a project whose sub-issue and `main` touch the same line no longer waits for the integration judge or a person when the resolver merged both sides correctly.
