<!-- changelog: security -->
- **Claude-fixer review now needs a strict verdict format before it counts a reviewer as clean past a failed reviewer slot.** A `NONE` next to an unlabelled finding, such as `- scripts/auth.sh:42 | severity=critical`, no longer counts as a clean vote.

When a reviewer slot fails, `scripts/review_autofix_step_claude_fixer_handoff.sh` counts the other reviewers as clean only when each one's own `review_<slug>.txt` says it found nothing (issue #5114). That check needed a `NONE` line and no *labelled* finding field, so a finding written without the `File:` / `Problem:` labels still passed. If the consensus ledger also read clean, five such votes could auto-merge the PR (security audit finding #5298). The output now has to match a strict verdict format. The verdict is one contiguous block of all nine checklist lenses, each followed by `NONE` (any order), or one bare `NONE`. Every other line is free text, and it fails the vote when it is a stray `NONE`, cites a code location (`auth.sh:42`, `#L42`, `line 42`), or carries a severity or confidence marker (`severity`, `blocker`, `major`, `critical`, `nit`, …). Narration, location-free summaries, and `HARDENING_SUGGESTIONS:` followed by `NONE` are still allowed.

| The numbers that matter | Value |
| --- | --- |
| Real reviewer outputs used to size the rule | 63, from 11 review runs on 2026-09-29 |
| Counted clean before / after | 34 / 22 |
| Outputs the old rule rejected that now count clean | 0 |
| New variables, log keys, GitHub API calls | none |

What this means for operators and consumer repos: a `claude/*` PR auto-merges past a failed reviewer slot only when the remaining reviewers wrote a clean verdict and nothing that reads like a finding. Reviewers whose summary cites a line number or a severity now count as missing clean votes, so such rounds go to the Claude session more often. Reviews with no failed slot are unchanged. Consumers get this with the next `@stable` release, with no wrapper change.

### For contributors

The new rejection reasons are `has text between its checklist verdicts`, `has a stray NONE verdict`, and `cites a code location or severity outside its verdicts`. The #5114 reasons are unchanged. The classifier stays plain awk, tested under both mawk and gawk.
