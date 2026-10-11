<!-- agents: section="Stable log prefixes (contractual)" -->
- `STABLE_RELEASE_SWITCH` (`mark-stable.yml` and `test-and-mark-stable.yml` release job, step `Resolve stable release switch`: `enabled=true|false`; only `STABLE_RELEASE_ENABLED=true`, any letter case, publishes)
- `STABLE_RELEASE_CI_GATE` (same release jobs, step `Require successful CI lint check-run`: `outcome=pass|wait|fail reason=missing|incomplete|failed|unreadable|invalid_sha sha= runs=`; the newest github-actions `lint` check-run of a `ci.yml` run for the tested commit decides; `wait` adds `polls_left=`)
- `STABLE_RELEASE_DISABLED` (same release jobs, step `Release disabled summary`: `version= sha=`; the gate passed but `STABLE_RELEASE_ENABLED` is off, so nothing was pushed, tagged, released or dispatched)
LOG_PREFIX.name=STABLE_RELEASE_SWITCH
LOG_PREFIX.name=STABLE_RELEASE_CI_GATE
LOG_PREFIX.name=STABLE_RELEASE_DISABLED
