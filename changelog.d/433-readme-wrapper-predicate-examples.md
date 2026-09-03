<!-- changelog: fixed -->
- **README core-wrapper examples now match the shipped dispatch predicates.**

The Quickstart now includes the canonical job-level `if:` predicates for `ai-clarify`, `ai-plan`, and `ai-implement` and describes their distinct issue-open, trusted-user, and marked-bot routes accurately. Consumers who hand-copy the examples no longer receive guidance that omits wrapper-side dispatch filtering or overstates the bot route for `/reclarify`. The Contributing section now links to the actual workflow-dispatch release flows instead of the removed `docs/release-policy.md` file.

What this means for operators: README-based wrapper installation and release guidance now agree with the checked-in templates and workflows.
