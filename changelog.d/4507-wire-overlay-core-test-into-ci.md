<!-- changelog: changed -->
- **CI now runs `tests/test_workflow_overlay_core.py`.**

The "Validation bootstrap and family direct-run tests" step in `.github/workflows/ci.yml` now runs `tests/test_workflow_overlay_core.py` directly, right after its sibling `tests/test_workflow_overlay_orchestrator.py`. PR #4507 corrected this test to pin `validate.yml`'s trusted-checkout `stage_workflow_support.sh` invocation from #4463, but no workflow ran the file, so it had been failing on main since 56fcde4 without CI noticing. It covers WORKFLOW.md overlay loading, prompt-override rendering, and the overlay staging in `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, and `validate.yml`.

What this means for contributors: a pull request that breaks the overlay loader or the staging of `stage_workflow_support.sh` now fails CI instead of passing unnoticed. The release gates in `mark-stable.yml` and `test-and-mark-stable.yml` are unchanged, and consumer repos are not affected.
