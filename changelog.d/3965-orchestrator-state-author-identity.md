<!-- changelog: security -->
- **Orchestrator state comments are now authorized by the exact account behind `GH_TOKEN`.** The poller compares each V1 comment and every V2 chunk's positive numeric `.user.id` with one process-cached authenticated-user lookup, removing the prior `[bot]` suffix and repository-association spoofing paths.

If authenticated-user resolution fails, state input is rejected and the tracking issue safely pauses for that poll cycle instead of reconstructing state or performing state-driven repository mutations.
