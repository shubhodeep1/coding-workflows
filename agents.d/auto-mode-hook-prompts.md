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
could match keeps every ask in Auto mode, because Auto mode resolves a
matching allow rule before its classifier, so a no-decision would approve it
unprompted: `_GH_API_ALLOW_RULE_RE` (`gh api repos/` or `gh api user`, also
after quote removal) and `_may_match_bash_allow_rule`, which reads every
`Bash(...)` allow rule in the project's `.claude/settings.json` /
`settings.local.json` (next to the hook and under `CLAUDE_PROJECT_DIR`),
`~/.claude/settings.json`, `CLAUDE_CONFIG_DIR` and the managed settings files
(`_MANAGED_SETTINGS_FILES`), and matches the command and each of its parts
(`Bash(test *)` around a hidden `$(gh api -X DELETE ...)`). A part that starts
with a wrapper in `_ALLOW_RULE_WRAPPER_WORDS` (`timeout`, `nice`, `env`,
`sudo`, `xargs`, ...) is also matched with up to 16 leading words dropped, so
`timeout 60 git diff "$(...)"` matches `Bash(git diff *)`. A settings file
that exists but cannot be read keeps every ask. A brace / ANSI-C / comment
hazard keeps its ask in Auto mode when the command holds an `@` or an escape
that can produce one (`_AUTO_HAZARD_FILE_FIELD_RE`), because it could hide a
file-backed `-F key=@file` field (#4619). Any other or missing mode keeps
the prompts, and the headless pipelines run in `dontAsk` /
`bypassPermissions` (pinned by `tests/test_gh_api_write_guard.py`).

<!-- agents: section="Pre-push affected-test gate" -->
- **Auto mode.** `hook_decision()` (used by `cmd_hook`) denies instead of
  asking when the payload's `permission_mode` is `auto` and every finding is
  a missing stamp (`FINDING_MISSING_STAMP`). The reason tells the session to
  commit in its own command, run `stamp`, fix failures this change caused,
  push again as its own command, and stop and tell the user if the stamp
  cannot be written. A failing stamp, a bulk or tag push, an unresolved push
  or ref (`FINDING_NEEDS_HUMAN`), alone or next to a missing stamp, still
  asks. `evaluate_hook()` keeps its `(ask reason, system message)` contract.
