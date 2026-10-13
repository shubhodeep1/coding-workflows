<!-- changelog: fixed -->
- **The check-triage resolution gate now posts the failing job's traceback for coloured Actions logs.** Issue #7302 (from the activation check of PR #7097).

`scripts/check_triage_resolution.py` read Actions job logs without `--allow-escape-sequences`. Actions logs carry ANSI colour codes, and `gh` refuses such a response without that option, so the read failed, was skipped, and the `ai:check-triage-traceback:v1` comment was never posted on an unverified `ai:check-triage` issue. Both job-log reads now pass the option. The log text is still filtered and redacted before it is posted, and the gate's verdict, markers and API calls are unchanged.
