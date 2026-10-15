<!-- changelog: fixed -->
- **Sandbox images include gawk.** The review sandbox (`scripts/review_sandbox/Dockerfile`) and the isolated agent container that `scripts/codex_isolated_exec.sh` builds now install `gawk`, which the review issue ledger and the consolidator parser call. Tests run inside either sandbox no longer fail on a missing binary. Refs #7355.
