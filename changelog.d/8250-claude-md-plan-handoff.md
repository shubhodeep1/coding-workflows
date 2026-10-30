<!-- changelog: changed -->
- **Interactive Claude sessions now plan non-trivial tasks and hand them to the pipeline, and `CLAUDE.md` is about a third smaller.** New `CLAUDE.md` §31 limits in-session implementation to small fixes. Anything larger is clarified and planned in the session, shown to the user, and then opened as `ai:engine-claude` issues (one per repo) or, for multi-PR work, handed to the orchestrator with `/write-plan` and `/implement-plan-claude`.

`CLAUDE.md` is loaded into every interactive session in coding-workflows and in every consumer repo, so its size is paid on every turn. The full text of §21, §22, §23, §24, §25.D and §29 now lives in `.claude/docs/`, and `CLAUDE.md` keeps a shorter version of each with a pointer (the rest of §25 stays in full in `CLAUDE.md`). Every section number and subsection letter is unchanged (§6). The moved text is copied verbatim, plus one new sentence in §23.C saying the user's go on a §31 plan approves the hand-off, so no rule was dropped. The unattended pipelines never load `CLAUDE.md`, so they are not affected.

| The numbers that matter | Value |
| --- | --- |
| `CLAUDE.md` size | 97,531 → about 65,900 bytes |
| New reference files | 6, under `.claude/docs/` |
| "Small fix" limit (§31.A) | at most 2 files, not counting a test for the fix or the §20/§30 fragments |

What this means for consumer repos: the next `@stable` sync brings the new `CLAUDE.md` and the `.claude/docs/` files together through the existing `.claude/` asset step. Interactive sessions there will open issues for larger tasks instead of implementing them, after the user approves the plan.

### For contributors

`workflow-templates/.claude/docs/` and `.claude/docs/` must stay byte-identical (`tests/test_claude_template_live_parity.py`). `tests/test_pr_watch_guard.py` and `tests/test_pr_merge_status_guard.py` now check the guard details in the reference files.
