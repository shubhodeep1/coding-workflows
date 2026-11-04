<!-- changelog: changed -->
- **The security-pass create-race test failure on `28f87cb` is treated as a likely flake.** It was not reproduced, so no poller change shipped. If it happens again, the workflow failure heal intake files it with the test's new failure diagnostics attached. Refs #8352.
