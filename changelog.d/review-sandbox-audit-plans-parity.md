<!-- changelog: fixed -->
- **Review autofix no longer fails every PR on the `/audit-plans` template parity test.** The root `.claude/commands/audit-plans.md` and `.claude/hooks/pr_merge_status_guard.py` match their `workflow-templates/` copies again, and the review editor's sandbox now admits `.claude/commands/audit-plans.md`.

`main` CI had been red since 2026-10-04: `tests/test_audit_plans_command.py::test_template_parity` and five `tests/test_pr_merge_status_guard.py` tests failed because #6176 and #6133 updated only the template copies. Every PR inherited that failure, so `Internal: AI Review & Autofix` handed it to the editor. The sandbox had no `.claude/commands/` directory, so the editor created one, and the result transfer refused it with `reason=unsafe_directory`. That failed the whole editor step and threw away the editor's other, valid fixes (runs 37251542418 on #6204 and 37255381476 on #6208).

| The numbers that matter | Value |
| --- | --- |
| Root files re-synced from `workflow-templates/` | 2 |
| Failing `main` CI tests fixed | 6 |
| New sandbox-admitted path | `.claude/commands/audit-plans.md` |

What this means for operators: review autofix runs stop failing on this parity test, and the editor can repair future drift in `audit-plans.md` itself. Other `.claude/commands/` files stay outside the sandbox, and a new directory the sandbox does not admit still fails the transfer.
