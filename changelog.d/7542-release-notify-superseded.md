<!-- changelog: changed -->
- **A release run cut short by a newer run now reports SUPERSEDED instead of FAILED.** `test-and-mark-stable.yml`'s `e2e-smoke-test` job shares a concurrency group with `cancel-in-progress: true`, so a newer release run cancels an older run's smoke test. The Telegram notification used to call that "Release vX FAILED", as it did for v1.35.2 in run 38986967451, which run 38993222716 had superseded.

The "Send Telegram notification" step now sends "Release vX SUPERSEDED: newer run N cancelled this run's e2e-smoke-test; nothing was released by this run" when three things hold: the smoke test was cancelled, no other gate, validate or release job failed, and a newer run of `test-and-mark-stable.yml` exists. It still lists every job's result under the header. The check costs one GitHub API call, made only in that case; if the call fails or finds no newer run, the header stays FAILED.

What this means for operators: a SUPERSEDED message needs no action; follow the newer run instead. FAILED still means a gate failed.
