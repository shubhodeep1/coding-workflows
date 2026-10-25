<!-- changelog: fixed -->
- **Implement runs in consumer repositories no longer commit Python bytecode from the pipeline's own helper scripts.** Before this, an issue could be blocked as `ai:scope-blocked` even though every file its plan changed was allowed.

The implement job copies its memory helpers (`ai_memory.py` and the modules it imports) into the consumer's `scripts/` folder, and a later step imports them from there. Python then wrote `scripts/__pycache__/*.pyc` files, the commit staged them, and the automation-path guard refused those `scripts/` paths and blocked the issue (mongo-explorer#42). `implement.yml` now sets `PYTHONDONTWRITEBYTECODE=1` for the whole workflow, and the generated `scripts/.gitignore` also lists `__pycache__/`, so both commit paths leave the folder out. The automation-path guard is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Bytecode files that blocked mongo-explorer#42 | 3 |
| Commit paths that now skip `scripts/__pycache__/` | 2 (preflight guard, `implement_commit_changes.sh`) |
| Guard rules changed | 0 |

What this means for operators: consumers without their own `__pycache__` ignore rule stop hitting this block once their wrappers sync to the release with this fix. mongo-explorer#42 still needs `ai:scope-blocked` removed and a new `/approved`.
