<!-- changelog: changed -->
- **Review: reviewer prompts no longer inline the workflow repo's `agents.md`, and `qwen/qwen3.7-plus` leaves the reviewer panel (#8194).** Together these should cut reviewer-panel OpenRouter spend by more than half.

Every reviewer prompt on every repo started with coding-workflows' own `agents.md`, about 658 KB of static prefix, even for a 50-line game-code PR in a consumer repo. Reviewers now get the unattended system instructions plus one line telling them to read the checkout's own `agents.md` / `AGENTS.md` when a diff needs it. The editor, consolidator and judges keep the full prefix. With the static prefix gone, the reviewer embed budget rises to its 800 KB ceiling, so very large PRs embed more of their diff than before. `qwen/qwen3.7-plus` cost the most per finding no other reviewer raised, so the panel drops from six models to five (`minimax/minimax-m3`, `anthropic/claude-sonnet-5.5`, `z-ai/glm-5.3-flash`, `google/gemini-3.1-flash-lite`, `openai/gpt-6-luna`); its failback chain stays for operator overrides.

| The numbers that matter (2026-10-19 to 2026-10-25, coding-workflows + tele-funtoken-msg-scoring) | Value |
| --- | --- |
| OpenRouter spend, all models | $954.53 |
| Reviewer-panel share | about $800 |
| Median reviewer prompt, 50-line diff | about 220k tokens |
| Input share of each reviewer model's cost | 91–96% |
| `qwen/qwen3.7-plus` spend | $245.12 |
| High/critical findings only qwen raised and no other reviewer disputed | 3 |

What this means for operators: nothing to configure, unless a repo pins `qwen/qwen3.7-plus` in `REVIEW_TIER_LITE_REVIEWER_SLUG` or `REVIEW_TIER_STANDARD_REVIEWER_SLUGS`. An unknown slug there fails open to the full panel with a warning, so update or clear that pin. Full-tier reviews now run five reviewers and standard-tier reviews pick four of those five. Logs show `REVIEW_STATIC_CONTEXT_SPLIT shared_bytes=… reviewer_bytes=…` in the static-context step and `REVIEWER_STATIC_CONTEXT source=reviewer` in the reviewer step; `source=shared` means the reviewer copy was missing and reviewers fell back to the full prefix.
