<!-- changelog: fixed -->
- **A check-triage issue whose failure is already fixed on the default branch is now closed with evidence instead of looping on clarification.** Issue #7095 (re-issued from #6945).

#6945 was filed for a failed `CI` run on PR #6938. By the time it reached clarify, #6938 had merged and #6941 had merged the test correction the triage named. Clarify still asked for `/answer` again and again, and the issue ended up blocked.

The clarify route step now runs `scripts/check_triage_resolution.py gate` for a trusted `ai:check-triage` issue before any question is posted. The issue is closed only when GitHub confirms all of the following:
- the linked PR merged from this repository;
- the failing check passes on the default branch tip, or for a failed CI run, the newest completed default-branch push run succeeded and every failed job name (matrix suffix included) succeeded in it;
- that commit contains the PR's merge commit (compare API `ahead` or `identical`).

The gate then posts one evidence comment, adds `ai:closed` and closes the issue as completed. It never writes to the PR and never posts `/answer`. Any other conclusion (`neutral`, `skipped`, pending, failed) or any read error keeps the normal fix path, and the gate posts the failing job's traceback once (redacted, with routing keys and markers neutralized). A failed check is never treated as passed, and security-pass behaviour is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls for issues without `ai:check-triage` | 0 |
| Reads for a check-triage issue | about 7, plus up to 3 job logs when unverified |
| Markers trusted only from the pipeline account | 2 (`ai:check-triage-resolved:v1`, `ai:check-triage-traceback:v1`) |

What this means for operators:
- Nothing to configure; `CHECK_TRIAGE_RESOLVED_GATE_ENABLED=false` turns the gate off.
- The gate runs when clarify starts (a new issue or `/reclarify`). A triage issue whose PR merges later is closed on its next `/reclarify`.
- If verification succeeds but a write fails, the clarify run fails (no `/answer` is posted), and a rerun finishes the close.
