<!-- changelog: changed -->
- **The merged-PR guard activation for #6831 is still pending an operator.** The fix needs both `pr_merge_status_guard.py` copies (and possibly `CLAUDE.md`), which the pipeline cannot write. `agents.md` now records the open step behind the `PR_MERGE_GUARD_ACTIVATION_UNSET_OPERATOR_STEP` placeholder. Nothing reads that name, the guard keeps its current behaviour, and no setting changes.

What this means for operators: land the #6831 change to both hook copies in a trusted session, keeping them byte-identical, then remove the `agents.md` note and the placeholder. Refs #6831. Refs #7052.
