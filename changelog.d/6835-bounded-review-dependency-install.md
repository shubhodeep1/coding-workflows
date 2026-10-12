<!-- changelog: fixed -->
- **Review dependency installs can now be time-bounded (opt-in).** The review sandbox installs PR dependencies best-effort, each under its own per-command budget, but several slow `npm`, `pip` or pytest-bootstrap installs in a row could still run into the dependency container's 900-second budget, which ends every install still pending, including the pytest bootstrap.

Set the repository variable `REVIEW_BOUNDED_DEPENDENCY_INSTALL_ENABLED=true` to give those optional installs one shared deadline, `REVIEW_DEPENDENCY_INSTALL_BUDGET_SECS` (default 600, allowed 30 to 780). An install that runs past it logs a `::warning::` naming the step, and the review continues with whatever dependencies were installed. Venv, container isolation and snapshot-refresh failures still fail the step; registry-proxy failures still skip the installs with a warning, as before.

What this means for operators: nothing changes until you set the variable. Once set, slow or hanging installs together cost at most the budget, and the pytest bootstrap is skipped with a warning rather than cut off by the container timeout.
