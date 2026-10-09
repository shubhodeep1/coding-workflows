<!-- changelog: fixed -->
- **Review dependency installs can now be time-bounded (opt-in).** The review sandbox installs PR dependencies best-effort, but a single stalled `npm`, `pip` or pytest-bootstrap install used up the whole 900-second isolation timeout, and the review step then failed as an isolation failure.

Set the repository variable `REVIEW_BOUNDED_DEPENDENCY_INSTALL_ENABLED=true` to give those optional installs one shared deadline, `REVIEW_DEPENDENCY_INSTALL_BUDGET_SECS` (default 600, allowed 30 to 780). An install that runs past it logs a `::warning::` naming the step, and the review continues with whatever dependencies were installed. Venv, registry-proxy, container isolation and snapshot-refresh failures still fail the step.

What this means for operators: nothing changes until you set the variable. Once set, a hanging registry or build backend costs at most the budget instead of failing the review.
