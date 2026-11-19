<!-- changelog: fixed -->
- **A `.gitleaksignore` merge conflict no longer stalls a project.** The conflict resolver now merges `.gitleaksignore` itself, the autofix editor suggests the fingerprint lines it may not write, and the unblock judge stops re-planning check-triage issues whose fix is already on the planning ref.

One gitleaks false positive stalled an orchestrator project three ways (shubhodeep1/binance-blessings #585, #615, #618, #629). The resolver sandbox may not edit root dotfiles, so a `.gitleaksignore` conflict failed closed with `sandbox_path_host_only` even when both sides had added the same fingerprint. Now `scripts/review_conflict_prepare.sh` keeps the base branch's `.gitleaksignore` byte for byte and appends the fingerprint lines the PR side added, on PR conflicts and on `orchestrator/project-*` integration sync. Deletions, malformed lines, symlinks and other unsafe shapes still fail closed. When a review finding needs `.gitleaksignore`, the editor summary comment now carries the validated gitleaks fingerprint lines as a ready-to-paste block for a human to decide on. A standalone `ai:check-triage` issue blocked with "nothing to implement" gets one note and waits for its check to pass instead of a resume that only blocks planning again.

| The numbers that matter | Value |
| --- | --- |
| New switches (default `true`) | 2 (`CONFLICT_GITLEAKSIGNORE_UNION_ENABLED`, `UNBLOCK_TRIAGE_NOTHING_TO_IMPLEMENT_GATE_ENABLED`) |
| New log keys | `CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION`, `REVIEW_EDITOR_GITLEAKS_SUGGESTION`, `UNBLOCK_JUDGE op=triage_superseded` |
| Extra GitHub API calls | at most 1 note comment per check-triage issue, re-written once per held judge run (at most once per `--marker-hours` window, since the note's last line is a throttle marker); once the note outlives `UNBLOCK_JUDGE_WAIT_MAX_DAYS`, 1 `close` marker comment plus the needs-human park's label and digest calls |

What this means for consumer repos: after the next `@stable` sync, a `.gitleaksignore` conflict resolves without a model run. Set the repository variable `UNBLOCK_TRIAGE_NOTHING_TO_IMPLEMENT_GATE_ENABLED=false` to restore the previous unblock judging; `CONFLICT_GITLEAKSIGNORE_UNION_ENABLED=false` in the review job environment turns the union off.
