<!-- changelog: fixed -->
- **Review dependency installs are now time-bounded.** The review sandbox installs PR dependencies best-effort, each under its own per-command budget. Several slow `npm`, `pip` or pytest-bootstrap installs in a row could still run into the dependency container's 900-second budget. That ended every install still pending and failed the review (#6793).

The optional installs now share one deadline, `REVIEW_DEPENDENCY_INSTALL_BUDGET_SECS` (default 600, allowed 30 to 780). An install that runs past it logs a `::warning::` naming the step, and the review continues with whatever dependencies were installed. Venv, container isolation and snapshot-refresh failures still fail the step. Registry-proxy failures still skip the installs with a warning, as before. The repository variable `REVIEW_BOUNDED_DEPENDENCY_INSTALL_ENABLED` defaults to `true`. `false` (or `0`, `no`, `off`) restores the old unbounded behaviour, and any other value does the same with a warning.

What this means for operators: slow or hanging dependency installs together cost at most the budget. The pytest bootstrap is skipped with a warning rather than cut off by the container timeout.
