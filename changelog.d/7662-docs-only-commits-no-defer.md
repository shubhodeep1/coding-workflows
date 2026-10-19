<!-- changelog: changed -->
- **A documentation-only commit on `main` no longer defers a promotion, whoever wrote it.** Before this, only `github-actions[bot]` commits were exempt, so a plan doc merged by a person during a promote cycle threw away a day's proving and verifying runs.

At a verifying run's ready-to-merge point, the poller (`comprehensive_untested_commits_json` in `scripts/orchestrate_poll_process.sh`) checks every commit between the smoke-tested commit and the promotion candidate. It now exempts a commit with one parent whose listed files are all non-code paths, from any author or committer. It still treats these as untested, so the promotion defers (`COMPREHENSIVE_PROMOTION_DEFERRED`):

- a merge commit (any number of parents other than one), because its file list is a combined diff that can hide the code it brought in;
- a commit that lists no files, or 300 or more;
- a commit that touches any code path, including the old name of a renamed file.

Some markdown files steer the pipeline because it reads them into model context. These now count as code in both the poller and the daily cycle start (`scripts/promote_main_cycle.sh`): `README.md`, `unattended_system_instructions.md`, `ai_pipeline.md`, `agents.md`, `AGENTS.md`, `codex.md`, `probably_unnecessary_but_read_if_stuck.md` and every `agents.d/` fragment. `docs/`, `changelog.d/`, `CHANGELOG.md` and other `*.md` files stay non-code. The two path lists are identical, and a test keeps them that way.

What this means for operators: a commit that touches only one of the newly listed files now starts a daily cycle, and one landing during a cycle defers the promotion. That includes a `github-actions[bot]` commit that changes `agents.md` outside a verified `auto/forward-merge-stable-*` merge. `COMPREHENSIVE_CYCLE_BOT_LOGINS` is still read but no longer changes the decision. The poller makes one commit read per single-parent commit in the range, once per verifying run; a failed read goes through the existing bounded hold.
