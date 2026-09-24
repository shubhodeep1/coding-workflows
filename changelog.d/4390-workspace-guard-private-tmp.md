<!-- changelog: fixed -->
- **Workspace guards now run with `PrivateTmp=yes` without losing their state.** Review, implementation, syntax repair, and poller guards store their manifests, reports, and quarantines in run-scoped directories under `RUNNER_TEMP`. The implement and poller guards also use trusted executable copies outside `/tmp`.

The sandbox rejects workspace-guard runtime and file paths hidden by private `/tmp` before launch, including symlink redirects, while retaining fail-closed snapshot and reconcile behavior. Existing workflow cleanup removes only its own guard directory.

The review iteration summary now runs from staged support so the review workflow stays below GitHub's workflow file-size ceiling.
