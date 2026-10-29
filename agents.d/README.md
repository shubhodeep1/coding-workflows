# agents.d — agents.md fragments

One file per PR: `agents.d/<issue-or-pr>-<slug>.md`. Never add to `agents.md`
directly (CLAUDE.md §30). Each block starts with a marker naming an existing
`## ` heading of `agents.md`:

```md
<!-- agents: section="Workflow architecture" -->
Text appended to the end of that section.
```

`scripts/assemble_agents.py` folds the fragments into `agents.md` at release
time and deletes them. CI runs `python3 scripts/assemble_agents.py check
--repo-root .`. This README is never folded.
