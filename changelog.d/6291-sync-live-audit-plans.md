<!-- changelog: fixed -->
- **`/audit-plans` in coding-workflows sessions now matches the copy consumers receive.** `.claude/commands/audit-plans.md` had kept the pre-#6176 wording, which still described `/implement-plan-claude` as an in-session implementer.

The command now treats in-session `/implement-plan-claude` projects as legacy work that can still be in flight. It names `/implement-plan-claude` (Claude-engine orchestrator hand-off) and `/implement-plan-ai` (default-engine hand-off) as the follow-ups. The conflict gate, archival rules and report format are unchanged.

What this means for operators: `tests/test_audit_plans_command.py::test_template_parity` passes on `main` again.
