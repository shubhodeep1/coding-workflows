<!-- agents: section="Reference" -->
`CLAUDE.md` keeps a condensed version of §21, §22, §23, §24, §25.D and §29; the
full section text lives in `.claude/docs/sNN-*.md` (for example
`.claude/docs/s23-github-access.md`). The §25 file holds only §25.D;
`CLAUDE.md` keeps §25.A–C and §25.E in full. The files are maintained in
`workflow-templates/.claude/docs/` and its byte-identical live copy
`.claude/docs/` (`tests/test_claude_template_live_parity.py`), and reach
consumer repos through the existing `.claude/` asset step of
`update_workflows.yml`, in the same sync as `CLAUDE.md`. When a rule in one of
those sections changes, edit both the `CLAUDE.md` summary and the reference
file. `CLAUDE.md` §31 makes interactive sessions plan non-small tasks and hand
them to the pipeline as `ai:engine-claude` issues (or `/write-plan` plus
`/implement-plan-claude` for multi-PR work) instead of implementing them.
