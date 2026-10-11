<!-- changelog: fixed -->
- **Review and planning prompts no longer include the README's env-var table, so a long table can no longer drop the whole README from prompts.** The `#### Variables` section is replaced by a one-line pointer to it.

The README text that review, clarify and plan prompts embed used to run from the top of `README.md` to `### 2. Create wrapper workflows`. In coding-workflows that span now holds a 280-row variable table of about 183 KB, and once a branch pushed it past the 200,000-byte prompt cap the reader rejected the README with `reason=prompt_size` and the prompts carried no README at all. That is what failed CI on the `orchestrator/project-6902` integration PR. Both trimmers, `scripts/review_untrusted_workspace.py readme-trimmed` and `scripts/build_static_context.sh readme`, now keep the `#### Variables` heading, add a pointer line, and skip the section's body up to the next heading. `README.md` itself is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Trimmed README in prompts, before | 196,814 bytes (main), 201,098 bytes (main merged with `orchestrator/project-6902`, rejected) |
| Trimmed README in prompts, after | about 15 KB |
| Prompt cap | 200,000 bytes, unchanged |

What this means for operators: prompts get smaller and the README no longer disappears from them as the variable table grows. Reviewers and planners that need a variable's default or description read it from `README.md` in the checkout. Consumer repos whose README has an exact `#### Variables` heading before `### 2. Create wrapper workflows` get the same treatment on the next `@stable` sync.
