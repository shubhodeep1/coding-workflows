<!-- changelog: security -->
- **Prompt rendering no longer copies secrets from the environment into prompts.** A `{{GH_TOKEN}}` line in a PR diff or comment can no longer put the GitHub PAT into a reviewer or editor prompt.

`scripts/render_prompt.sh` used to fill every `{{NAME}}` token from the environment variable of the same name. Reviewer and editor prompt bodies contain the raw PR diff and comment text, so whoever wrote that text chose which variables were read, including `GH_TOKEN`. Now:

- When `RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED` or `RENDER_PROMPT_SKIP_SYNTAX_VALIDATION` is set (bodies with untrusted text), only `WORKFLOW_EDIT_RESTRICTION`, `SEMBLE_PREFETCH`, `SERENA_TOOL_HINTS`, `MODEL_FAMILY_OVERLAY` and `REFERENCE_*` files are filled in. Every other token stays as literal text.
- Trusted templates still read the environment, but never for secret-like names: `GH_TOKEN`, `GITHUB_TOKEN`, `GH_PAT`, the Actions runtime/OIDC variables, any name ending in `_TOKEN`, `_KEY`, `_SECRET`, `_PAT`, `_PASSWORD` or `_CREDENTIAL(S)`, and any name containing `SECRET` or `PASSWORD` (case-insensitive). A one-line warning on stderr gives the name, never the value.
- `scripts/render_prompt.py` also ignores `--var` values with secret-like names, so branches that still stage an older `render_prompt.sh` are covered too.
- The reviewer, model-family overlay and editor render subshells in `scripts/review_run_reviewers.sh` and `scripts/review_apply_fixes.sh` unset `GH_TOKEN` and `GITHUB_TOKEN` before rendering.

What this means for consumer repos: no action is needed. A custom prompt that relied on a secret-like placeholder name now renders it literally; rename the placeholder.

Refs #9399.
