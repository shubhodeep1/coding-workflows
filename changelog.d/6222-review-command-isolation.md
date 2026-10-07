<!-- changelog: security -->
- **Review editor transfers keep operator-facing Claude commands outside the isolated workspace.** The `audit-plans` command and its consumer template are synchronized so review does not need to repair their parity inside that workspace.

The `Internal: AI Review & Autofix` editor does not snapshot or transfer files under `.claude/commands/` back into the host checkout. Attempts to introduce that directory in an isolated result continue to fail with the path-free `unsafe_directory` reason rather than overwriting an operator command. The matching `audit-plans.md` copies remove the parity mismatch that triggered the repeated review failures on PR #6135.

What this means for operators: command parity can be checked without opening the isolated editor's trust boundary.
