<!-- changelog: changed -->
- **Workflow-heal implementations now run on Claude, and the review editor's Claude pool runs show up in the job log.** Heal issues no longer fall back to `openai/gpt-6-sol` by design.

`scripts/heal_isolated_implement.sh` used to log `AI_ENGINE_FALLBACK role=IMPLEMENT reason=isolated_claude_unavailable` and run codex for every `ai:workflow-heal` issue, even though `IMPLEMENT` resolves to Claude by default. When it resolves to Claude, the heal editor and its syntax-repair passes now run through `ai_engine.sh` `claude_run` on the script's own snapshot, in the same credential-free write container normal implementation uses. The snapshot then takes the unchanged syntax validation and scope-checked transfer. Claude unavailable (exit 75) runs the isolated codex editor as before; any other Claude failure fails the run without a transfer. `HEAL_ISOLATED_EDITOR ... engine=` names the engine that ran.

The review editor's stderr reaches the job log only when an attempt fails, so a successful Claude editor run left no trace of which pool account it used. `scripts/review_apply_fixes.sh` now replays the attempt's `CLAUDE_POOL` and `AI_ENGINE_*` lines with a `stage=editor` prefix, like the consolidator's `stage=consolidator stderr=` lines.

| The numbers that matter | Value |
| --- | --- |
| Heal runs that fell back to codex by design, Oct 7-9 | 8 |
| New log line | `stage=editor CLAUDE_POOL run role=REVIEW_EDITOR ...` |
| Heal wall-time limit (`HEAL_ISOLATED_EDITOR_WALL_SECS`) | now applies to the Claude run too |

What this means for operators: heal fixes add Claude pool load instead of OpenRouter spend, and the editor's pool usage is now countable from run logs alongside clarify, plan and implement.
