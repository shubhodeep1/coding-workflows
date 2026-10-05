<!-- changelog: fixed -->
- **Merged-PR guard confirmations now survive multiple warnings in one hook call.** The guard combines its warnings and confirmation reasons into one JSON response, so a push with an unresolved repository still asks for permission instead of producing output the hook cannot parse.

The live and consumer-template hooks now emit at most one JSON object per invocation. A blocked push retains its stderr block and warning without an ask decision; a non-canonical API write retains its confirmation request. No scheduler or operator action is required.
