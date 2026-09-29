<!-- changelog: security -->
- **Permission-prompt reports no longer post credentials from the blocked command.** `permission_prompts.py` now masks credential values before a command reaches an `ai:permission-prompt` issue or a consumer repository's PR or issue, and withholds the command when it cannot mask it safely.

A denied command such as `curl -u deploy:mycustompwd …` used to be posted as is, because the redactor only masked token-shaped strings. Every posted Bash command is now parsed first. The values of credential flags (`curl -u`, `--user`, `--password`, `--token`, `mysql -p`, …), credential headers (`Authorization`, `Cookie`, …), URL userinfo, credential query parameters, and credential-named `NAME=value` words become `***`. When the command cannot be parsed, or a value cannot be masked exactly, the report shows `<command withheld: …; shape: …>` instead. Other tools' input masks credential-named keys, and the pattern shape in the issue title no longer carries raw text for unparseable commands or attached short flags.

| The numbers that matter | Value |
| --- | --- |
| Finding | `immediate-report-credential-disclosure`, high, `.claude/scripts/permission_prompts.py:791` (issue #5124) |
| Shortest value masked in place | 4 characters (shorter ones withhold the command) |
| Sites covered | `file` issue bodies and comments, the `report-now` report in coding-workflows and on consumer PRs and issues, and `lookup`'s `command` |

What this means for operators: a report can now read `<command withheld: …>` with only the command's shape. That is deliberate: the session link in the report still leads to the full command. Patterns of unparseable commands, and of commands with an attached credential short flag such as `-udeploy:pwd`, get a new signature, so each opens a new `ai:permission-prompt` issue once.

### For contributors

The rules cover the flags, headers, URLs, and assignments above, plus the existing token and 40-character masking. A tool that takes a secret in a form none of them names (for example a bare positional password) is still posted, so add a rule to `_CREDENTIAL_SHORT_FLAGS`, `_CREDENTIAL_LONG_FLAG_RE`, or `REDACTION_PATTERNS` when one turns up. Tests: `tests/test_permission_prompts.py` (`test_example_masks_credentials` and the tests that follow it).
