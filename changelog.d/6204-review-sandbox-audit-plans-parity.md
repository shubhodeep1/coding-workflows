<!-- changelog: fixed -->
- **The review editor can now repair `.claude/commands/` template parity failures instead of failing the run.** Its sandbox admits each `.claude/commands/<name>.md` whose `workflow-templates/.claude/commands/<name>.md` twin exists in both the PR checkout and the verified workflow-support checkout.

Before this, the sandbox held no `.claude/commands/` files. When a PR's CI failed `tests/test_audit_plans_command.py::test_template_parity`, `Internal: AI Review & Autofix` handed the failure to the editor, the editor created the missing directory, and the result transfer refused it with `reason=unsafe_directory`. That failed the editor step and discarded the editor's other, valid edits (runs 37251542418 on #6204 and 37255381476 on #6208). The admitted set is frozen at snapshot time, so a PR-added twin absent from verified support or a twin added during transfer cannot admit a command on retry. Missing or malformed admission state stops transfer rather than falling back to the mutable host checkout.

| The numbers that matter | Value |
| --- | --- |
| Command files admitted in this repo | 13 of 13 under `.claude/commands/` |
| Command files admitted in a repo without `workflow-templates/` | 0 |
| Failing runs this addresses | 37251542418, 37255381476 |

What this means for operators: review autofix no longer fails on a `.claude/commands/` parity test; it edits the live copy in the PR like any other source file. Other `.claude/` paths stay outside the sandbox. An editor write to an excluded file in an admitted directory is still dropped, and a new directory outside the admitted ones still fails the transfer.
