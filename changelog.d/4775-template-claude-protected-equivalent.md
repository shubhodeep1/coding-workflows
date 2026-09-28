<!-- changelog: security -->
- **`workflow-templates/.claude/**` is now protected-equivalent for unattended authorization, and CI fails on a template-only change to it.** This closes security finding #4775 (`template-protected-path-bypass`, high).

The `@stable` sync copies `workflow-templates/.claude/` over every consumer's `.claude/`, where unattended sessions follow its commands, hooks, and settings. Claude Code protects only the repository root's `.claude/`, and the #4678 wording in CLAUDE.md §23.I said the template tree "is not protected". An unattended agent steered by issue text could therefore change consumer commands with no approval. CLAUDE.md §23.I and §28.C now say the template twins take the same `Protected-path approval:` stop as the root `.claude/**`. A permission-prompt issue about them is still fixed rather than closed as by design, because Claude Code itself does not prompt there.

| The numbers that matter | Value |
| --- | --- |
| Template files checked | every file under `workflow-templates/.claude/` (commands, hooks, scripts, `settings.json`) |
| Allowed divergences | 5 consumer-variant commands, each pinned by SHA-256 |
| CI step | "Unattended helper and permission prompt report tests (CLAUDE.md §23.I)" in `.github/workflows/ci.yml` |

What this means for contributors and operators: a PR that changes a `workflow-templates/.claude/` file without the same change to its root `.claude/` twin fails CI. So does a PR that edits one of the five consumer-variant commands (`analyze-log.md`, `deploy-activate.md`, `investigate-issue.md`, `validate-consumer-issue.md`, `verify-activation.md`) without updating its pin in `tests/test_claude_template_parity.py`, and one that adds a template-only file or a symlink. An unattended `/implement-plan-claude` phase that needs such a change stops for an operator's approval first. Consumer repos receive the CLAUDE.md wording on their next `@stable` sync; the test runs only in this repo.

### For contributors

`tests/test_claude_template_parity.py` replaces nothing: the per-file `test_template_parity` checks stay. It walks the template tree without following symlinks and skips only `__pycache__/` and `*.pyc`. To change a consumer variant, update its `TEMPLATE_DIVERGENCE` digest (`sha256sum workflow-templates/.claude/commands/<name>.md`) in the same PR. This refines the #4678 entry: that entry's "only protected path" statement describes what Claude Code itself protects.
