<!-- changelog: fixed -->
- **The review-blocked judge now runs on a pull request whose conflict the resolver refused as host-only.**

A `force_rb_judge` dispatch used to re-run the conflict resolver, which refused the host-only conflicted paths again and failed the run before the review-blocked judge could decide. The pull request stayed stuck and gained another identical failure comment. When the newest failure marker that the pipeline account wrote for the current head names `conflict_resolver_sandbox_path_host_only`, the run now skips conflict detection and both resolver steps and goes straight to the judge. Markers from other authors or other heads, a newer failure with a different reason, or a failed identity or comment read keep the resolver steps, and the resolver's refusal and the review-blocked handoff are unchanged. Each decision logs `AUTOFIX_FORCE_RB_JUDGE_CONFLICT_SKIP`.

What this means for operators: a host-only conflict now reaches the review-blocked judge instead of looping. Set `REVIEW_FORCE_RB_JUDGE_HOST_ONLY_SKIP_ENABLED=false` to restore the old behaviour.
