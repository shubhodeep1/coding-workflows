<!-- changelog: security -->
- **The merged-PR guard now checks live PR state before a guarded push instead of trusting a cached open PR.**

`git push` and GitHub MCP file pushes query PR state on each hook invocation, sharing one result per branch within a Bash call. Local commits still use the 300-second cache. If the lookup fails, the existing git-history fallback blocks a provably stranded push or asks for confirmation; it does not use cached PR state to authorize the write.
