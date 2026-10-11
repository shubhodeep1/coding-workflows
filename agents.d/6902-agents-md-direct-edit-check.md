<!-- agents: section="Workflow architecture" -->
CI enforces the agents.md fragment contract (CLAUDE.md §30) on pull requests.
The `static-checks` step `agents.md direct-edit check (CLAUDE.md §30)` runs
`scripts/check_agents_md_direct_edits.py --base <merge^1> --head <merge>` and
fails when an `agents.md` / `AGENTS.md` hunk outside
`## Repo-tree (auto-generated)` only inserts lines, or when its hunks outside
that section add more than `AGENTS_MD_DIRECT_NET_ADD_MAX` lines (default `5`)
beyond the lines they replace. Corrections and removals pass. The step skips
a project's final PR from `orchestrator/project-*` (its sub-PRs were checked
when they merged into the integration branch) and an
`auto/forward-merge-stable-*` fallback PR (release-fold text), only for a
same-repository head targeting the default branch; the release fold itself
pushes without a PR. The
inline implement prompt (`mode-implement-inline.txt` in `implement.yml`), the
review editor prompt (`scripts/review_apply_fixes.sh`, `AGENTS.MD
DISCIPLINE`) and `unattended_system_instructions.md` §25 tell the model to
write an `agents.d/` fragment instead.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `AGENTS_MD_DIRECT_EDIT` (`scripts/check_agents_md_direct_edits.py`: `outcome=ok|violation|error`, `net_added=`, `count=`)
LOG_PREFIX.name=AGENTS_MD_DIRECT_EDIT
