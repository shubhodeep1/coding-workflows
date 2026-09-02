<!-- changelog: fixed -->
- **Stall recovery no longer re-approves issues latched with `ai:destructive-blocked` or `ai:scope-blocked`.** The orchestrator poller now pauses stall recovery for those issues the same way it already does for `ai:needs-human`.

When the destructive-commit guard or the scope guard in `implement.yml` rejects an implementation run, it latches `ai:destructive-blocked` (or `ai:scope-blocked`) on the issue and every later `implement.yml` run refuses to redispatch it (`AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=destructive_blocked outcome=skip`) until a human removes the label. The issue keeps its `ai:awaiting-approval` phase label, so `scripts/orchestrate_poll_process.sh` saw an ordinary approval stall and posted `/approved` once per poll cycle: `auto_approve` three times, then a Codex stall-judge run that chose `retrigger_implement`, each round a refused implement run and a Telegram warning. On `shubhodeep1/tele-funtoken-msg-scoring` issue #3906 that produced four recovery rounds in four hours with no possible progress, and the recovery budget would eventually have closed the issue and let the judge regenerate it under a new number, discarding the human-in-the-loop signal. `orchestrate_lib.detect_stalls` and the standalone stall loop now skip issues carrying a `STALL_RECOVERY_LATCH_LABELS` entry, and the `check-stalls` payload gains an additive `latched` list so the poll log shows `STALL_SKIP issue=<n> reason=human_gated_latch label=<label> phase=<phase> action=none` instead of staying silent.

| The numbers that matter | Value |
| --- | --- |
| Latch labels that pause stall recovery | `ai:destructive-blocked`, `ai:scope-blocked` |
| Recovery rounds burned on issue #3906 before this fix | 4 (`auto_approve` x3, `retrigger_implement` x1) |
| New stable log line | `STALL_SKIP issue=<n> reason=human_gated_latch label=<label> phase=<phase> action=none` |
| Regression tests | `tests/test_orchestrate_lib.py`, `tests/test_orchestrate_poll_process.py` |

What this means for operators: a latched issue now waits quietly for the human review the guard asked for. Remove the latch label (and set `ALLOW_BULK_DELETE=true` or `ALLOW_WORKFLOW_EDITS=true` when the deletions are legitimate), then reply `/approved`, and the pipeline resumes; the stall recovery counter is not consumed while the latch is present.

### For contributors

`detect_stalls()` keeps its return shape; the new `detect_stall_latched_issues()` helper feeds the additive `latched` field of the `check-stalls` CLI payload, and `stall_recovery_latch_label()` is the single predicate both call. The standalone loop in `run_standalone_stall_recovery` resolves the phase and the latch label through `determine_phase()` and `stall_recovery_latch_label()` in one Python call, so `STALL_RECOVERY_LATCH_LABELS` is the single place to add a latch label; if that call produces no output the candidate is skipped for the cycle with a `[standalone-stall]` warning instead of aborting the poller.
