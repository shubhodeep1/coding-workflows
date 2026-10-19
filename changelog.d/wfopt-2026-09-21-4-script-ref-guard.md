<!-- changelog: fixed -->
- **The workflow script-reference check now fails when `scripts/render_prompt.py` is missing.**

`scripts/check_workflow_script_refs.py` skipped `render_prompt.py` through `OPTIONAL_REFS`, because of an old note saying the file did not exist on `main`. It does: it is the backend of `render_prompt.sh` and `assemble_prompt.sh`, and review support staging requires it. Deleting or renaming it therefore passed CI and only broke staging at run time. `OPTIONAL_REFS` is now empty, so the `Script-workflow cross-reference` CI step, the release gates and the post-resolver guard report the missing file. `tests/test_check_workflow_script_refs.py` covers the workflow scan, `scripts/stage_workflow_support.sh` and `--files` mode.

What this means for operators: nothing to set.
