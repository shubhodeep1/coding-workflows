<!-- changelog: security -->
- **Check-failure triage no longer lets PR-controlled logs redirect automated fixes.**

The triage workflow neutralises routing metadata and issue markers in both raw-log fallbacks and model diagnoses before posting an issue. Fallback evidence uses a fence longer than any backtick run in the captured logs, preserving readable failure context without allowing a log line to break out of the fence. If neutralisation fails, triage stops instead of posting the issue.

What this means for consumer maintainers: triage issues still follow the existing default-branch routing, while failure logs remain available as evidence.
