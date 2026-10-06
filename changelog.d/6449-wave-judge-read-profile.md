<!-- changelog: security -->
- **Wave-judge model calls no longer inherit GitHub write credentials.** Claude runs in the isolated read profile; the Codex fallback strips GitHub and Telegram tokens and uses a read-only sandbox. Refs #6449; related to #3576.
