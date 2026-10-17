<!-- agents: section="Stable log prefixes (contractual)" -->
- `LABEL_SYNC_RETIRED_IN_USE` (`scripts/ai_labels.py`: `level=WARNING count= issues=`)
- `LABEL_SYNC_RETIRED_CHECK_FAILED` (`scripts/ai_labels.py`: `action=in_use_check`)
LOG_PREFIX.name=LABEL_SYNC_RETIRED_IN_USE
LOG_PREFIX.name=LABEL_SYNC_RETIRED_CHECK_FAILED

<!-- agents: section="Implement scope-lock label" -->
- Before `sync-labels` deletes a retired label it makes one read-only `GET repos/{repo}/issues?labels=<label>&state=open&per_page=100` and logs `LABEL_SYNC_RETIRED_IN_USE` (`level=WARNING`, `count=` capped at `100+`, `issues=` up to 10 numbers) when open issues or PRs still carry it. The delete still proceeds; a failed read logs `LABEL_SYNC_RETIRED_CHECK_FAILED` and the sync continues with the same exit status. In `--dry-run` the read runs only after the label `GET` found the label. Tests: `tests/test_ai_labels_retired.py`.
