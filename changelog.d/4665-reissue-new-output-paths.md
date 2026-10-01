<!-- changelog: fixed -->
- **A review-blocked spot-fix reissue can now list the new files its follow-up must create, so the implement run's scope guard no longer refuses a required changelog fragment or new test fixture.**

When the review-blocked judge closes a PR with `reissue_mode: spot-fix`, `scripts/review_rb_judge.sh` writes a `files_touched` allowlist into the replacement issue, and `implement.yml` refuses any commit outside it. That list could only hold files the judge cited or files the closed PR changed, and both had to exist at the closed PR head. A follow-up that had to add a file was therefore blocked. #4664, the reissue of #4605 / PR #4607, latched `ai:scope-blocked` on a new changelog fragment and four new `tests/fixtures/integration_ref_resolver/*.json` fixtures. The judge contract (`prompts/mode-judge-review-blocked.txt`) now has an optional `new_output_paths` array, and each declared path that passes validation is appended to the allowlist.

| The numbers that matter | Value |
| --- | --- |
| Paths #4664's scope guard refused | 5 (1 changelog fragment, 4 new fixtures) |
| Declared paths read per reissue | at most 10 |
| New GitHub API calls | 0 |

What this means for operators: a spot-fix reissue that has to add files no longer stops at `ai:scope-blocked`. The guard itself is unchanged. A declared path is dropped when it fails the path validator, is not printable ASCII or has a leading or trailing space, contains a glob character or trailing `/`, has a `.git` segment at any depth, already exists at the closed PR head, cannot be looked up there, or has no file extension in its last segment, so this cannot exempt an existing file or directory, or a new extensionless directory path. Extensionless new files (`Dockerfile`, `.gitignore`) are dropped too and still need the human-gated procedure, and a new directory whose name carries a dot (`conf.d`) is the one accepted residual: one brand-new subtree the judge named. The new `REISSUE_FILES_TOUCHED_NEW_OUTPUTS` log line shows how many paths the judge declared, added, and skipped, and each skip is logged with its reason. #4664 still has to be released through the existing human-gated procedure.

### For contributors

The new source runs after the `REISSUE_FILES_TOUCHED_UNION` step and only on the spot-fix path. A rejected entry is skipped and never forces `redo`, which matches the closed-PR union. A judge that omits the field produces a byte-identical issue body. Change `prompts/_templates/mode-judge-review-blocked.txt` together with the runtime prompt.
