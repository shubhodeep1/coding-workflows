<!-- agents: section="Workflow architecture" -->
In Auto mode (hook payload `permission_mode` == `auto`) the `gh api` guard
gives no decision, instead of asking, for the doubts that are only about a
command's shell shape: an unparseable command, a hidden `gh api` call, ANSI-C
quoting / unquoted `#` comment / brace expansion, and an unquoted `gh api`
argument expansion. A hook "ask" forces a human prompt even in Auto mode, so
this hands those commands to the Auto-mode classifier. Calls the guard
classifies as writes (unreadable calls included) and a flag between `gh` and
`api` still ask; the malformed-jq deny and every allow are unchanged, and a
command with a shape doubt is never allowed. A command that an allow rule
could match is denied in Auto mode instead (`auto_shape_deny`, its reason
ending in `_AUTO_SHAPE_DENY_SUFFIX`: one plain `gh api` call per ID, no
`$VAR` file redirects, the Edit or Write tool for text), because Auto mode
resolves a matching allow rule before its classifier, so a no-decision would
approve it unprompted and an ask would stop on a prompt; a deny runs nothing: `_GH_API_ALLOW_RULE_RE` (`gh api repos/` or `gh api user`, also
after quote removal) and `_may_match_bash_allow_rule`, which reads every
`Bash(...)` allow rule in the project's `.claude/settings.json` /
`settings.local.json` (next to the hook and under `CLAUDE_PROJECT_DIR`),
`~/.claude/settings.json`, `CLAUDE_CONFIG_DIR` and the managed settings files
(`_MANAGED_SETTINGS_FILES`), and matches the command and each of its parts
(`Bash(test *)` around a hidden `$(gh api -X DELETE ...)`). A part that starts
with a wrapper in `_ALLOW_RULE_WRAPPER_WORDS` (`timeout`, `nice`, `env`,
`sudo`, `xargs`, ...) is also matched with up to 16 leading words dropped, so
`timeout 60 git diff "$(...)"` matches `Bash(git diff *)`. A settings file
that exists but cannot be read is denied the same way. A brace / ANSI-C /
comment hazard is denied in Auto mode (`hazard_auto_deny`, same rewrite
steps) when the command holds an `@` or an escape
that can produce one (`_AUTO_HAZARD_FILE_FIELD_RE`), because it could hide a
file-backed `-F key=@file` field (#4619). Any other or missing mode keeps
the prompts, and the headless pipelines run in `dontAsk` /
`bypassPermissions` (pinned by `tests/test_gh_api_write_guard.py`).

<!-- agents: section="Pre-push affected-test gate" -->
- **Auto mode.** `hook_decision()` (used by `cmd_hook`) denies instead of
  asking when the payload's `permission_mode` is `auto` and every finding is
  in `SESSION_FIXABLE_FINDINGS`: a missing stamp (`FINDING_MISSING_STAMP`), or
  a pushed ref that does not resolve to a commit yet, usually a branch the
  same command creates (`FINDING_UNRESOLVED_REF`). The reason tells the
  session to create the branch and commit in their own command, run `stamp`,
  fix failures this change caused, push again as its own plain command, and
  stop and tell the user if the stamp cannot be written. A stamp with status
  `fail` (`FINDING_STAMP_FAILURES`) never holds the push: alone it returns
  `DECISION_CONTEXT_ONLY`; next to another finding its text joins that
  finding's reason. A bulk or tag push, push options or a destination the
  hook cannot resolve, and a stamp with a missing or unknown status
  (`FINDING_NEEDS_HUMAN`), alone or next to another finding, still ask.
  `evaluate_hook()` keeps its `(ask reason, system message)` contract.

<!-- agents: section="Isolated Codex agents" -->
- **Merged-PR guard in Auto mode (#8180).** In `_evaluate_bash` of both
  `pr_merge_status_guard.py` copies, a push whose only confirmation reasons
  concern its shell shape (an unresolved push source or destination, such as
  a branch the same command creates, a push inside control flow, or an
  expansion in a refspec) exits 2 when `permission_mode` is `auto`
  (`AUTO_PERMISSION_MODE`), with steps to create the branch and commit first
  and then push as one plain `git push -u origin <branch>`. Nothing runs.
  Unknown-destination, deletion, default-branch, bulk and
  unverified-destination pushes still ask, and every other mode keeps the ask.

<!-- agents: section="Interactive slash-command model selection" -->
`.claude/settings.json` is listed in `.github/ai/claude_template_divergence.json`
(#8180): only the live copy appends `mcp__claude-code-remote__create_session`
to `permissions.allow`, for the operator's daily activation routine; consumers
never get it. The live-copy sync and the parity test skip the file, so every
other settings change goes into both copies by hand. The settings parity checks
use `settings_match_template()` in `tests/claude_template_drift.py`, which
accepts exactly that appended rule, and `tests/test_no_session_automation.py`
exempts exactly that one line.
