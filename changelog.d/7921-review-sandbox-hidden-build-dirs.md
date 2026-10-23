<!-- changelog: fixed -->
- **Poller judges and other review-sandbox agents are now told which committed build-output directories they cannot see.** The review sandbox leaves every `dist`, `build` and `coverage` directory out of its copy of the repository, so a judge that could not see a committed `dist/` took it for missing.

On 2026-10-22 the wave judge for tele-funtoken-msg-scoring project #3928 filed fix issue #4770 to "restore" `cloudflare/funtoken-competition-worker/dist/`, which was committed and correct on the integration branch. The implementer then blocked on `npm ci`, and the project sat on `ai:blocked`. Now `scripts/review_untrusted_workspace.py snapshot` records the committed directories it hid, and `scripts/review_untrusted_sandbox.sh` adds a "Directories left out of this sandbox copy" note to every prompt: each directory with its committed file count, and an instruction not to treat their absence as missing from the branch. The directories still stay out of the copy, so no agent can edit or write back anything new.

| The numbers that matter | Value |
| --- | --- |
| Directory names that are hidden and now reported | `dist`, `build`, `coverage` |
| Directories listed per prompt | at most 40 |
| New log field | `REVIEW_ISOLATION_SNAPSHOT hidden_build_dirs=<n>` (a count, never a path) |

What this means for operators: poller judges, the review editor and the other roles that run in the review sandbox (including the Claude-pool reviewer slot) no longer file or plan work to rebuild committed build output just because they cannot see it. No setting changes.

### For contributors

Only tracked files count, and only plain-ASCII paths are listed; a path under a dot directory (other than `.github/actions` and `.github/workflows`, which the sandbox admits, so a committed action bundle is listed) or `node_modules`, or with a secret-like name, is left out of the note. The list is written next to the snapshot manifest as `<manifest>.hidden_build_dirs.json`. The note is advisory: a missing or unreadable list adds nothing to the prompt and never fails the run.
