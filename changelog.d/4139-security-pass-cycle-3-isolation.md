<!-- changelog: fixed -->
- **AI model processes no longer inherit GitHub credentials or writable Git metadata, and security waivers now follow Python trust-boundary causality.** Planner, review, resolver, poller-judge, and security-audit calls run behind the provider-only sandbox; model edits cross a hook-disabled exact-head Git writer before publication; review Python packages stay in a secretless container volume; and added routes, reverse callers, decorators, or deleted guards invalidate stale security waivers.

Concurrent branch movement now rejects a model-produced push and lets the next automated cycle re-evaluate it instead of rebasing or force-updating from a privileged step. Unsupported or ambiguous causal analysis remains blocking by design.
