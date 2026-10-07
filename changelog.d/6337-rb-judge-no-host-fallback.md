<!-- changelog: fixed -->
- The orchestrator review-blocked judge no longer runs PR-derived prompts on a credentialed host agent when sandbox preparation fails. It defers and escalates repeated isolation failures on the same PR head, and uses a fresh isolated OpenCode sandbox when Claude is unavailable.
