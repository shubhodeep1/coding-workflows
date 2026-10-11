<!-- agents: section="Optional `.github/ai` operator surfaces" -->
- Implement workspace hooks (#7158). `scripts/run_workspace_hook.sh` runs every
  phase's hook in the validation hook container, never on the host: no network,
  read-only root, `env -i`, and a screened workspace copy without `.git` (whose
  config carries the `GH_PAT` origin URL in implement), `.ai`, the
  workflow-support checkouts or credential files. The trusted runner copies the
  hook it resolved into the snapshot (`.github/ai/workspace_hooks/<phase>/<hook>.sh`,
  regular file reached without symlinks, at most 256 KiB), so implement's
  `after_run` / `before_remove` keep running the pre-editor copy from
  `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`. Outside `validate`, only non-executable
  `<phase>/hook-output/<hook>/<name>.txt` files come back, into
  `${RUNNER_TEMP}/workspace-hooks/output/<phase>/<hook>/`, never into the
  workspace; any other change refuses the whole result (exit 125: fatal for
  `after_create` / `before_run`, a warning for `after_run` / `before_remove`).
  Phase names must start with a letter or digit.
