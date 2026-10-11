<!-- changelog: added -->
- **CI now fails a pull request that adds documentation to `agents.md` directly.** New text belongs in an `agents.d/` fragment (CLAUDE.md §30), and the implement and review prompts now say so.

Project #6902's final PR #6905 stopped on an `agents.md` conflict: its integration branch and `main` had each appended lines to the same section, and the integration self-heal gave up. The fragment rule already existed, but the prompts the pipeline sends did not mention it and nothing checked it. The `agents.md direct-edit check (CLAUDE.md §30)` step in `ci.yml` now runs `scripts/check_agents_md_direct_edits.py` on every coding-workflows pull request except a project's final PR and a forward-merge fallback PR. It fails when an `agents.md` or `AGENTS.md` hunk outside `## Repo-tree (auto-generated)` only inserts lines, or when its hunks add more than `AGENTS_MD_DIRECT_NET_ADD_MAX` lines beyond the lines they replace. Corrections and removals still pass. The inline implement prompt, the review editor prompt and `unattended_system_instructions.md` §25 now tell the model to write a fragment.

| The numbers that matter | Value |
| --- | --- |
| Net lines a correction may add (`AGENTS_MD_DIRECT_NET_ADD_MAX`) | 5 |
| Log line | `AGENTS_MD_DIRECT_EDIT outcome=ok\|violation\|error` |

What this means for operators: agents.md conflicts between concurrent PRs and integration branches stop at the source, and a PR that slips through gets a CI error naming the hunk and the fragment to write instead.
