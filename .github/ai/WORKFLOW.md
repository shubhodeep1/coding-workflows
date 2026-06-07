---
schema_version: workflow_overlay.v1
---

# Workflow overlay

Optional per-repository workflow overlay, loaded by
`scripts/load_workflow_overlay.py` and validated against
`ai-memory/schemas/workflow_overlay.v1.json`.

This overlay is intentionally a **no-op**: the front matter declares only
`schema_version` and no `prompt_overrides`. Its presence sets
`WORKFLOW_OVERLAY_ENABLED=true` so the overlay loader path is exercised on every
workflow, but no rendered prompt is changed.

To tune per-mode prompts, add `prompt_overrides[]` entries to the front matter,
each with a `mode` and exactly one of `append_path` or `replace_path` (paths
resolved from the repository root). For example:

```
---
schema_version: workflow_overlay.v1
prompt_overrides:
  - mode: implement
    append_path: .github/ai/prompt_overrides/implement-extra.txt
---
```

Markdown body content below the front matter is ignored by the loader.
