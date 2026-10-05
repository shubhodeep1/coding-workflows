<!-- changelog: security -->
- **Live `.claude/` sync now runs verified stable support.** The sync and Telegram alert no longer execute code from a pushed commit with broad credentials. Checkout credentials are not persisted, and the sync receives the PAT only in its pinned step. AI write guards protect the sync script and workflow from agent-authored changes. Refs #6419.
