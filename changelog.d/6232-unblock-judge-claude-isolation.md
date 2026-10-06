<!-- changelog: security -->
- **The unblock judge no longer runs Claude Code on the credential-bearing runner when judging blocked work.** It runs Claude inside the read-only, network-isolated container, with its OAuth token held by a host-side relay. If Claude is unavailable, it falls back to the isolated Codex path; other isolation failures leave the item blocked without a verdict.

The judge reads issue comments, PR diffs, and run logs when deciding how to unblock an item. Previously those inputs could reach host-side Claude Code with access to the runner filesystem and the Claude credential. The existing clarify isolation runner now accepts the `UNBLOCK_JUDGE` role for Claude and enforces the judge's timeout inside the container. No new workflow trigger or credential is needed.
