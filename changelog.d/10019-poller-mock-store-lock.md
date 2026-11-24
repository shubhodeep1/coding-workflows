<!-- changelog: fixed -->
- **The poller test sandbox's fake `gh`, `git` and `python3` now lock and atomically write their shared mock store.** When orchestrate-poll test shards run in parallel, the external-finalize branch-deleted security-pass test can no longer lose a fetch or PR-read count or read a half-written store (Refs #9869).

Each fake, and the `tg_cleanup_msgs` / `tg_send_tracked` test helpers, takes an exclusive lock on `<store>.lock` before it loads `GH_MOCK_STORE` and replaces the file atomically when it saves. A fake started by another fake inherits `GH_MOCK_STORE_LOCK_HELD=1`, so it does not wait on its parent. The wait is capped at 30 seconds (tests can shorten it with `GH_MOCK_STORE_LOCK_WAIT_SECONDS`); after that the fake carries on unlocked, as before this change, and says so on stderr, and a test now checks that message. Only the test fixture changed; production scripts and workflows did not.

What this means for consumer repos: nothing; this changes tests only.
