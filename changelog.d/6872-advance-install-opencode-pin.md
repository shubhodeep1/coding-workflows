<!-- changelog: fixed -->
- **The review workflow and the orchestrator poller now get the OpenCode install retry.** `review_autofix.yml` and `orchestrate_poll.yml` pinned `.github/actions/install-opencode` to commit `28f5134`, which predates the retry added for #6818 (PR #6867). Both now pin merge commit `0e8d83a7bc8c05218b77026899c8d53f45ab921f`, which contains it.

The #6818 entry (`changelog.d/6818-install-opencode-retry.md`) said these two workflows would get the retry only once their pins moved; activation verification of PR #6867 found the pins unchanged and filed #6872. Neither call site passes `install_max_attempts`, so both use the default.

| The numbers that matter | Value |
| --- | --- |
| Pinned references moved | 2 (review, poller) |
| npm install attempts | 3 |
| Pauses between attempts | 10 s, 20 s |

What this means for operators: a single npm registry blip while installing OpenCode no longer fails a review run or skips the poller's OpenCode judge. Consumer repositories receive this with the next stable release and wrapper sync.
