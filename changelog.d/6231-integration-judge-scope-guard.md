<!-- changelog: fixed -->
- Integration-conflict judge resolutions can no longer push unrelated file changes.

The poller records the merged index before invoking the isolated judge and rejects changes outside the conflicted paths. Conflicted workflows and actions must use lines from the merge sides in their original relative order without extra duplicates; the committed tree must match the validated staged tree before a push. Rejected resolutions remain subject to the existing bounded escalation path.

What this means for operators: a judge can resolve genuine conflicts without gaining a route to publish unrelated workflow changes.
