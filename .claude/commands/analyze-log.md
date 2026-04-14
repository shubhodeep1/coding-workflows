Download the full log from the following URL and analyze it for errors, failures, and issues:

$ARGUMENTS

## Steps

1. **Download the log** — Use `curl -sL` to download the raw log content from the provided URL. Save it to a temporary file in `/tmp/` so you can reference it throughout the analysis.

2. **Read the full log carefully** — Do not skim or summarize. Read the entire log from start to finish. Pay attention to:
   - Error messages, stack traces, exceptions
   - Failed assertions or test failures
   - Exit codes, signal kills, OOM errors
   - Timeout or connectivity failures
   - Deprecation warnings that may have escalated to errors
   - Mismatched dependency versions
   - Environment or configuration issues

3. **Identify every distinct issue** — List each unique issue found. For each issue, note:
   - The exact error message or log line
   - The root cause (not just the symptom)
   - Whether it is the primary failure or a cascading/secondary failure

4. **Correlate to source code** — For each issue, search the current repository to find the relevant source files. Use grep, file paths mentioned in stack traces, and your understanding of the codebase to locate the exact files and lines responsible.

5. **Propose fixes** — For each issue where a code change can help, explain what needs to change and why. Then apply the fix to the actual source files. Claude Code will ask for confirmation before writing.

## Rules

- Always download the complete log. Never truncate or skip sections.
- Prioritize the root cause — the first meaningful error in the log — over cascading failures.
- If the log contains multiple independent failures, address each separately.
- If a failure is environmental (e.g., a service was down, rate limit hit) and no code change can fix it, say so explicitly rather than proposing a workaround.
- Do not modify test expectations to make tests pass unless the test is genuinely wrong.
- Clean up the temp file when done.
