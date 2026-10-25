<!-- agents: section="Workflow architecture" -->
In Auto mode (hook payload `permission_mode` == `auto`) the `gh api` guard
gives no decision, instead of asking, for the doubts that are only about a
command's shell shape: an unparseable command, a hidden `gh api` call, ANSI-C
quoting / unquoted `#` comment / brace expansion, and an unquoted `gh api`
argument expansion. A hook "ask" forces a human prompt even in Auto mode, so
this hands those commands to the Auto-mode classifier. Calls the guard
classifies as writes (unreadable calls included) and a flag between `gh` and
`api` still ask; the malformed-jq deny and every allow are unchanged, and a
command with a shape doubt is never allowed. Any other or missing mode keeps
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
