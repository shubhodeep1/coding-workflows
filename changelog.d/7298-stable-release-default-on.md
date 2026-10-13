<!-- changelog: changed -->
- **The stable-release switch's default-on setting is now documented as intentional, so activation verification no longer reports it as a gap.**

`STABLE_RELEASE_ENABLED` still publishes when unset or empty, and release behaviour does not change. The fail-closed `CI / lint` check-run gate is the release safety control and runs whatever the switch says; the switch is an emergency off switch.

What this means for operators: set `STABLE_RELEASE_ENABLED=false` and `AUTO_RELEASE_STABLE_ENABLED=false` to pause releases.
