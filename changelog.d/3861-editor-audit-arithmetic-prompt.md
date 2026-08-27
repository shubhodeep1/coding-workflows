<!-- changelog: fixed -->
- **Convergence runs of `review_autofix.yml` no longer trip a false-positive editor no-op block.** The editor prompt now states the audit arithmetic invariant the no-op validator enforces.

When every reviewer finding was already fixed on HEAD, the editor could record "confirmed a prior fix is already present" as `issues already applied 1` against `total issues listed 0` in its summary's "Review file issue audit" section. `scripts/validate_editor_audit.sh` correctly flagged the imbalance, set `EDITOR_NOOP_SUSPICIOUS=true`, skipped the "Enable auto-merge on PR" step, and paged the operator, even though the run was a genuine, healthy convergence. This happened on tele-funtoken-msg-scoring PR 3809 (run 33088357425). The editor prompt in `scripts/review_apply_fixes.sh` now spells out that `total issues listed == issues applied + issues already applied + issues ignored` must hold on every audit bullet, and that a review file listing zero new issues gets all four counts emitted as 0, with prior-fix confirmations narrated under "Already satisfied (suggested but already present):" instead.

| The numbers that matter | Value |
| --- | --- |
| Prompt file changed | `scripts/review_apply_fixes.sh` |
| Validator (unchanged, strict on purpose) | `scripts/validate_editor_audit.sh` |
| Incident run | tele-funtoken-msg-scoring Actions run 33088357425 |
| New tests | 2 (`test_editor_prompt_states_audit_arithmetic_invariant`, `test_convergence_shape_total_zero_already_applied_one_is_mismatch`) |

What this means for operators: a review run whose editor honestly concludes "no changes needed" now reaches auto-merge instead of ending in an "Editor no-op suspicious" Telegram warning that asks for a manual re-run. The validator itself is unchanged, so a summary whose counts genuinely do not add up still blocks auto-merge.

### For contributors

The fix is prompt-side only. The validator's arithmetic stays strict because it is what keeps `total issues listed` trustworthy as an auto-merge gate; the new validator test pins the observed false-positive shape (`total 0, already applied 1`) as a mismatch on purpose, and the new cascade-contract test keeps the prompt sentence and the validator in lockstep.
