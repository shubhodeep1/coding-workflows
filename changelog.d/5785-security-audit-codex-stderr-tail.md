<!-- changelog: fixed -->
- **Security audit failures now show a redacted Codex error tail and provider status.** When Codex exits nonzero, the audit log includes up to 40 sanitized lines (4 KiB) and a `provider=402|401|429|5xx|unknown` classification on the existing failure line. Prompt/config echoes and credential-shaped values stay out of the published tail; successful audits are unchanged.
