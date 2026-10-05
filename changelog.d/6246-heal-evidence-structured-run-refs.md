<!-- changelog: security -->
- **Workflow-heal evidence now reads only intake-authored structured run references.** It ignores run links in quoted logs and comments without leading markers, verifies every run's repository, head and outcome before fetching evidence, and limits successful or unfinished runs to review jobs.
