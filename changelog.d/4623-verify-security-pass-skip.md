<!-- changelog: security -->
- **An `ai:security`, `ai:check-triage`, or `ai:workflow-heal` label no longer switches off the security pass by itself.** `/implement-issue-claude` now skips an issue-mode project's security audit only for issues that the issue automation created and labelled.

Before this change, anyone who could label an issue could make the project built from it skip `security-audit.yml` (finding `mutable-label-skips-security-pass`, #4623). Step 6 of `.claude/commands/implement-issue-claude.md` now runs `.claude/scripts/security_pass_skip.py --repo <owner>/<repo> --issue <N>` and writes `Security pass: skip` only when the script prints `"skip": true`. That requires four things:

- the author is `github-actions[bot]`, or the repository `OWNER` account that the audit, triage and heal workflows post as through `GH_PAT`;
- that account applied the label within 120 seconds of creation, and no other account ever applied it;
- the body carries the producer's marker line;
- for `ai:security`, `Refs #<tracker>` names the `ai:security-audit` tracker created by the same account.

Any other result, and any failed read, keeps `Security pass: run`.

| The numbers that matter | Value |
| --- | --- |
| REST reads per check | 1 with no skip label, at most 3 otherwise |
| Label-at-creation window | 120 seconds |
| Skip labels checked | `ai:security`, `ai:check-triage`, `ai:workflow-heal` |

What this means for operators: security follow-ups, check-failure triage issues and workflow-heal issues filed by automation still skip their own audit, so a fix cannot spawn follow-ups of follow-ups. An ordinary issue that someone labels by hand now runs the full security pass. In a repository owned by an organisation, the `GH_PAT` account is a `MEMBER`, not the `OWNER`, so the skip never verifies there and the audit always runs.

### For contributors

The script is mirrored to `workflow-templates/.claude/scripts/` and allowlisted in both `.claude/settings.json` files. `route_issue`'s `skip_security_pass` field in `scripts/claude_issue_route.py` is unchanged and remains advisory: it is logged and carried in the dispatch payload, but it decides nothing. The contract lives in `tests/test_security_pass_skip.py`, which runs in the `Claude issue implementer tests` step of `ci.yml`.
