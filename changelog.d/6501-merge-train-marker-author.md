<!-- changelog: security -->
- **Forged queue comments can no longer bypass the merge train.** Resolves the `forged-queue-comment-bypasses-train` finding (issue #6501).

The train now recognizes queue markers only from the account authenticated for the run. A one-shot manual bypass proceeds only after its verified marker is consumed successfully; a failed update re-queues the review instead. Release also leaves a queued PR alone when the marker author cannot be verified.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | Up to 1 identity read per run, only when inspecting queue markers |

### For contributors

Removing `ai:merge-queued` and re-running review still provides a one-shot bypass when the train's own queued marker can be retired.
