<!-- changelog: security -->
- **Workflow failure heal no longer trusts the heal generation a report claims, so a forged `gen=999` marker can no longer stop the repair issue from being filed.** The intake now checks the claimed generation against the heal issues it lists itself.

An escalated heal issue carries `<!-- workflow-failure-heal:gen=… -->` / `root=…` markers. The reporter copies them into the dispatch payload, and the intake used to add one to that generation without further checks. A large generation therefore escalated straight to the lineage cap, and no repair issue was filed. The reporter already ignored markers on unverified issues. A collaborator-edited heal issue and a forged or replayed payload still got through, and both are now rejected.

`budget` in `scripts/workflow_failure_heal.py` now takes `--source-issue owner/repo#N`. The intake passes it for issue reports. The claimed generation is accepted only when all of the following hold:

- the source issue appears in the intake's own `ai:workflow-heal` listing;
- its author is an OWNER, MEMBER, COLLABORATOR or a Bot;
- its body starts with the canonical five-line marker header, and that header's `gen` / `root` equal the payload;
- the generation is backed by an earlier trusted heal issue of the same lineage. A generation-1 issue must be its own root.

When any check fails, the claim is ignored and lineage comes from the fingerprint, as it would with no claim. The open-issue and per-day caps still apply. The payload validator now drops `source_gen` / `source_root` from non-issue reports and when only one of the two is present.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls | 0 (the two existing heal-issue list calls gain `author_association` and the user type) |
| New decision fields | `source_lineage` (`none` / `verified` / `rejected`), `source_lineage_reason` |
| Rejection reasons | `source_issue_missing`, `source_issue_not_listed`, `untrusted_author`, `non_canonical_markers`, `payload_mismatch`, `lineage_gap` |
| New log line | `WORKFLOW_HEAL source_lineage outcome=rejected reason=<r> claimed_gen=<n> source=<repo#N> fp=<fp>` |

What this means for consumer repos: nothing to change. The check runs in this repository's intake. A heal issue whose lineage cannot be verified (for example, one filed before the canonical header existed, or whose earlier lineage issue was deleted) starts a new lineage instead of inheriting its generation.

### For contributors

The new helpers are `verify_source_lineage`, `_trusted_heal_author` and `_canonical_heal_markers`. `build_issue_payload` now uses the last two, with unchanged behaviour. `budget_decision(source_gen=…)` without a verifiable `source_issue` now ignores the generation. Tests: `test_budget_decision_verifies_inherited_lineage`, `test_validate_payload_keeps_lineage_only_for_issue_reports`, `test_budget_cli_passes_source_issue` and `test_intake_verifies_source_lineage_from_its_own_heal_list` in `tests/test_workflow_failure_heal.py`.
