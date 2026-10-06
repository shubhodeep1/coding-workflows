<!-- changelog: security -->
- **Review consolidation no longer runs OpenCode on the host.** Both the default path and Claude-unavailable fallback use a fresh credential-free, read-only sandbox. If isolation fails, consolidation is skipped and the reviewer bundle remains authoritative.
