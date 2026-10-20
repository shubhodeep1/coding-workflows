<!-- changelog: fixed -->
- **AI memory pushes that fail on missing or rejected credentials now stop after one attempt.** Before this, every such push used all 16 retries with backoff, fetch and rebase. In run 35665803378, nine memory writes each spent 16 attempts on `could not read Username`, about 10-16 minutes of runner time.

`persist_memory_operation` in `scripts/ai_memory_lib.py` now checks the stderr of each failed push. It stops at once on these credential and permission errors:

- `could not read Username` or `could not read Password`;
- `Authentication failed` or `terminal prompts disabled`;
- `Invalid username or password` or `Permission to <repo> denied`;
- `The requested URL returned error: 403` or `returned error: 401`.

A 403 failure whose text, outside any URL, names a rate limit or abuse detection (`triggered an abuse detection mechanism`) still retries. The raised error reads `Failed to push memory branch: error_class=auth attempts=<n> ...` and includes git's stderr. The `GH_TOKEN`, `GITHUB_TOKEN` and `GH_PAT` values are removed from that text, and every URL in it, including the origin URL, becomes `<redacted-url>`.

Ref-lock (`cannot lock ref`), non-fast-forward and other failures keep the existing jittered retry loop and `AI_MEMORY_PUSH_RETRIES` budget. Callers still treat the error as before, so memory writes stay fail-open.
