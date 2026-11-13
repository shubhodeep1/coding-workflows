<!-- changelog: security -->
- **Prompt rendering no longer copies environment secrets into reviewer and editor prompts.** `scripts/render_prompt.sh` used to fill any `NAME` placeholder in the prompt body from an environment variable of the same name. A PR author could put a `GH_TOKEN` or `OPENROUTER_API_KEY` placeholder in a changed file or a comment, and the real secret was then sent to the model provider in the reviewer or editor prompt. Refs #3576.

What changed:
- When the caller marks the body as untrusted (`RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED` or `RENDER_PROMPT_SKIP_SYNTAX_VALIDATION`), `render_prompt.sh` no longer fills placeholders from the environment at all. Only the four scaffolding placeholders it passes explicitly (`WORKFLOW_EDIT_RESTRICTION`, `SEMBLE_PREFETCH`, `SERENA_TOOL_HINTS`, `MODEL_FAMILY_OVERLAY`) are rendered. Any other placeholder in the body stays as literal text.
- For trusted templates, environment forwarding still works, but a name containing `TOKEN`, `KEY`, `SECRET`, `PAT`, `PASSWORD`, `PASSWD` or `CREDENTIAL` (any case) is refused. The script prints `WARNING: render_prompt.sh: refusing to forward secret-like environment placeholder <NAME>` on stderr, with the name only, never the value.
- The reviewer body, model-family overlay and editor body renders in `scripts/review_run_reviewers.sh` and `scripts/review_apply_fixes.sh` now run with `GH_TOKEN`, `GH_PAT`, `GITHUB_TOKEN`, `OPENROUTER_API_KEY` and `TG_BOT_SECRET` unset, as a second layer of protection.

What this means for consumer repos: no action is needed. A trusted prompt template that relied on an environment variable whose name contains one of the words above must now pass that value explicitly with `--var`, or rename the placeholder.
