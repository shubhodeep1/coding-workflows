<!-- changelog: security -->
- **A real defect on the same line as a rejected false positive can no longer be demoted by the reviewer quoting the old finding's id. The flagging reviewer's `consensus_id` citation now binds only to one of its own structured findings at that file and line.**

Security audit follow-up #4975 (A04, high) found a gap in the #4687 rule that ties a pass-2 finding to the pass-1 finding other reviewers rejected. `scripts/review_claude_fixer_nonblocking.py` accepted the tie when the `consensus_id` appeared anywhere in the flagging reviewer's raw `review_<slug>.txt`. If the summariser copied the old id onto a new, real defect on the same line, the reviewer mentioning that id in passing was enough, and the rejections of the false positive moved the real defect into `NON-BLOCKING FINDINGS`, where a round with green checks could auto-merge. Now the id counts only as a whole `consensus_id:` line inside exactly one of the reviewer's `File:` finding records, and that record must name the same file with a line overlapping the entry. The id in prose, in a code block, in a `REJECTED_FINDING` line, or after the record's blank line binds nothing. A second finding by the same reviewer in that file within 3 lines, or with no readable line, keeps the entry blocking.

| The numbers that matter | Value |
| --- | --- |
| Citation that binds | one `consensus_id: p1-<12 hex>` line inside one `File:` record, same file, overlapping line |
| Record ends at | the first blank line, the next `File:` / `Requirement:` / `REJECTED_FINDING` line, a heading, or a code fence |
| Ambiguity window (keeps the finding blocking) | another finding by the flagging reviewer in the same file within 3 lines, or without a readable line |
| New `CLAUDE_FIXER_NONBLOCKING_KEPT` reasons | `flagger_citation_mismatch`, `ambiguous_flagger_nearby` |

What this means for operators: a real defect next to a rejected false positive reaches the Claude fixer instead of being demoted. Some rejected false positives also stay blocking when the reviewer writes the id outside its finding block, and cost one fixer round; that is the intended direction. The `CLAUDE_FIXER_NONBLOCKING_KEPT` line in the review run log names the reason.

### For contributors

`flagger_finding_records()` parses the reviewer's raw output with the same fence rules as the vote parser; a record starts at a plain, bulleted, numbered, or heading `File:` line. A finding's line comes from its `File:` value (`path:N[-M]`) or its first `Line or code reference:` / `Line:` / `Lines:` field, and only from an explicit reference: a leading number or range, `path:N`, `line N`, or `LN`. A number inside code text (`retries = 3`), a version (`3.14:40`), or a URL's `host:port` reads no line, and a `File:` value that starts with prose (`the install example`) names no file, so both keep a nearby entry blocking. Wrapping markup, quotes, angle or link brackets, and a trailing `.` / `;` are not part of the path; `N to M` / `N through M` is a range, and a list (`1250, 1262`, `lines 40 and 52`) spans every line it names. For the second-finding check, a path that ends with `/` plus the entry's path (an absolute runner path) is the entry's file. The pass-2 cross-pollination header in `scripts/review_run_reviewers.sh` now tells reviewers to put the `consensus_id:` line inside the finding's own block.
