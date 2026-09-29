<!-- changelog: changed -->
- **An `/implement-issue-claude` session can now close its pipeline-filed `ai:permission-prompt` issue as a duplicate itself, instead of stopping to ask.** New shapes of an inline-interpreter write are also added as comments to the open issue of that class rather than opening new issues.

On 2026-09-28 the operator answered 15 duplicate questions by hand (#4677, #4726, #4749, #4751, #4759–#4762, #4767, #4779, #4780, #4808, #4820, #4824, #4843), and each one also cost an Opus session. CLAUDE.md §23.C now names a narrow carve-out, spelled out in §23.I "Closing pipeline-filed duplicates", and `/implement-issue-claude` gains step 5a. The session runs `permission_prompts.py duplicate-check`, which confirms three things: the issue was filed by the pipeline (signature marker, "Filed by" line, author = the session account, label applied at creation); the target is open or closed as completed; and the fix PR references the target and is in flight or merged. The session then posts one evidence comment and closes the issue with `state_reason: duplicate`. Human-filed or relabelled issues, PRs the session did not open, and branch deletion still ask. The filer tags inline-interpreter writes (the #4858 definition) with a class marker, and a later new shape of that class comments "Seen again" on the open class issue instead of filing.

| The numbers that matter | Value |
| --- | --- |
| Operator answers this replaces (2026-09-28) | 15 |
| `duplicate-check` REST reads | at most 5 (user, issue, one events page, target, fix PR) |
| Extra reads for class routing | 0 (reuses the one `ai:permission-prompt` list read) |
| Label-at-creation window | 120 s (the `security_pass_skip.py` rule) |
| Command classes | 1 (`inline-interpreter-write`) |

What this means for operators: a duplicate `ai:permission-prompt` issue closes itself, with an evidence comment naming the target, the fix PR, the matching reason or class, and the sessions involved. You only get an `ai:claude-blocked` question when the check fails or the evidence is not concrete.

### For contributors

The changes land through the `workflow-templates/.claude/` twins of `commands/implement-issue-claude.md` and `scripts/permission_prompts.py` (the interim twin-first rule, #4785). `.claude/**` follows at the twin sync. `duplicate-check` runs under the existing `permission_prompts.py *` allow rule, so `settings.json` is unchanged. `tests/test_permission_prompt_duplicates.py` covers the class detection, the routing, every eligibility condition, and the carve-out text.
