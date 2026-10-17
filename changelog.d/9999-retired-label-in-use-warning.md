<!-- changelog: changed -->
- **Label sync now warns when a retired label is still on open issues or PRs.** `scripts/ai_labels.py sync-labels` logs `LABEL_SYNC_RETIRED_IN_USE` before it deletes such a label, so the issues it strands no longer disappear silently.

When the label contract retired `ai:claude`, the sync deleted the label from consumer repos while 26 open issues still carried it. For each label in `retired_labels`, the sync now makes one read-only call, `GET repos/{repo}/issues?labels=<label>&state=open&per_page=100`, before the delete. If any open issue or pull request still carries the label, it logs one `level=WARNING` line with `count` (`100+` when the page is full) and up to 10 issue numbers. The delete still proceeds, `--dry-run` also reads but never deletes, and a failed read logs `LABEL_SYNC_RETIRED_CHECK_FAILED` and continues, so the exit status and the `errors` list are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per retired label | 1 read (`per_page=100`) |
| Issue numbers listed per warning | up to 10 |
| Count cap | `100+` |

What this means for operators: search the sync logs for `LABEL_SYNC_RETIRED_IN_USE` to find issues that lost a retired label, and for `LABEL_SYNC_RETIRED_CHECK_FAILED` when the in-use check could not run.

### For contributors

On a real run the read must precede the `DELETE`, so it is also issued for a retired label that turns out to be absent. In `--dry-run` it runs only after the label `GET` found the label.
