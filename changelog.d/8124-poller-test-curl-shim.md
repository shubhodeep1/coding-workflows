<!-- changelog: fixed -->
- **Poller tests no longer send real requests to `api.github.com`.** Two promote-cycle tests hung past the 180-second test limit when the network was slow (CI run 40182895760).

At project completion the poller cleans up its Telegram messages, and that cleanup reads the tracking issue's comments with `curl` and no time limit. The test harness did not replace `curl`, so every poller test that completed a project made a live HTTPS request. The harness now puts a `curl` stand-in first on the sandbox `PATH`: it records each call with `Authorization` values hidden and fails at once, as on a runner without network. The two promote-cycle tests now check that their cleanup request reached the stand-in.

What this means for operators: nothing changes in production. `curl_gh_api` in `scripts/gh_helpers.sh` still runs `curl` with no time limit, so a stalled connection can still hold a scheduled poller job until its timeout; that fix is tracked separately.
