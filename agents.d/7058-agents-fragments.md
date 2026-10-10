<!-- agents: section="Workflow architecture" -->
agents.md fragments (CLAUDE.md §30): PRs document behaviour in
`agents.d/<issue-or-pr>-<slug>.md` instead of editing this file.
`scripts/assemble_agents.py` folds them into the named sections at release
time (release job, same commit as the changelog fold) and in consumers on the
`update_workflows.yml` sync; CI's `check` fails a fragment that would not fold,
and readers (`build_static_context.sh`, the materiality checks,
`tests/agents_doc.py`) see pending fragments through `render`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `AGENTS_ASSEMBLE_V1` (`scripts/assemble_agents.py`: `outcome=assembled|noop|ok|invalid file= folded= skipped= dry_run=`, `skipped=<file> reason=<r>` per fragment that cannot fold, `error=agents_unreadable`)
LOG_PREFIX.name=AGENTS_ASSEMBLE_V1
