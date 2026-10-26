<!-- changelog: fixed -->
- **Review runs consolidate findings again.** The consolidator, parser, issue ledger and floor-rule stages of `review_autofix.yml` had been skipped on every run. They now run, the consolidator runs on Sonnet 5.5 on the Claude engine, and the review editor's Claude usage reaches the job log.

None of `scripts/review_floor_rules.sh`, `scripts/review_consolidate.sh`, `scripts/review_parse_consolidator.sh` or `scripts/review_issue_ledger.sh` was in `REQUIRED_BOOTSTRAP_SCRIPTS`, so `scripts/review_apply_fixes.sh` logged `review_consolidate.sh not found; skipping consolidator stage` and skipped all four stages, including the "≥2-reviewer floor rule is non-overridable" guarantee documented in `agents.md`. The four scripts, and the editor's `ledger_emit_substate.sh` (run-substate telemetry) and `codex_thread_reuse.sh`, which it finds the same way, are now staged with the rest of the support bundle, and staging fails if one is missing. The editor's `stage=editor` log replay now includes the usage line `claude_engine.py extract` prints, so editor calls report `num_turns`, tokens and `total_cost_usd` like the panel reviewer does.

| The numbers that matter | Value |
| --- | --- |
| Runs that skipped consolidation, 2026-10-19 to 10-26 | 827 of 827 that reached the editor (coding-workflows and tele-funtoken-msg-scoring) |
| Successful editor calls with a usage line, same week | 11 of 845 |
| Consolidator model on the Claude engine | `claude-sonnet-5-5` (was `claude-opus-5-5`); `ai:engine-claude` still forces Opus |
| New smoke input | `cache_layout_probe` on `claude-engine-smoke.yml`, default `false` |

What this means for operators and consumer repos: after the next `@stable` sync, each review run that reaches the editor makes one more Claude call (the consolidator, on Sonnet), and the floor-rule, parser and ledger stages run before the editor again. Editor cost now shows in `stage=editor {"duration_ms": ...}` log lines. `CLAUDE_FIXER_ENABLED=false` still moves the consolidator back to OpenCode.

### For contributors

`claude-engine-smoke.yml` gains an opt-in prompt-cache layout probe (read leg only, about $3.50 of Sonnet usage). It compares `cache_read` and `cache_write` for the review static context placed at the top of the user message versus in the system prompt (`SUPPORT_INSTRUCTIONS_FILE`), and logs `CACHE_LAYOUT_PROBE` lines. It measures only; no reviewer prompt layout changes in this PR.
