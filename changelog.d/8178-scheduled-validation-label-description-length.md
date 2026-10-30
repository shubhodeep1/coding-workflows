<!-- changelog: fixed -->
- **The `ai:scheduled-validation-failed` label can now be created by the label sync.** Its description was 115 characters, and GitHub rejects label descriptions over 100, so the label could never be created with its contract color and description.

  The description added in #8178 is now 92 characters: "Weekly scheduled validation of the default branch is failing; closes when a later run passes". Before this fix, `sync_ai_labels.yml` logged `LABEL_SYNC_ERROR: ... label=ai:scheduled-validation-failed action=create ... detail=description exceeds GitHub's 100-character limit (115)` on every run, including the dry run in CI. `ensure_label_exists` in `scripts/label_helpers.sh`, which the scheduled validation sink (`scripts/validate_scheduled_sink.sh`) calls before it opens its issue, failed the same way. The label name is unchanged. The text is updated in both `.github/ai/label_contract.v1.json` and the shell catalog in `scripts/label_helpers.sh`.

  | The numbers that matter | Value |
  | --- | --- |
  | GitHub label description limit | 100 characters |
  | Old description length | 115 characters |
  | New description length | 92 characters |

  What this means for consumer repos: the next `@stable` sync of `ai-sync-labels.yml` creates or updates the label with its contract color and description. Nothing needs to be run by hand.

  ### For contributors

  `tests/test_ai_labels.py::test_label_descriptions_fit_github_description_limit` now fails when any description in the contract or in `scripts/label_helpers.sh` is over 100 characters.
