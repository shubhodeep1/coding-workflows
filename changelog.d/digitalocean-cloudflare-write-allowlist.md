<!-- changelog: added -->
- **Interactive Claude Code sessions can now execute approved DigitalOcean and Cloudflare API writes without a second permission prompt.** `.claude/settings.json` gains a `permissions.allow` list covering `curl` PUT / POST / PATCH calls to `api.digitalocean.com` and `api.cloudflare.com`.

Until now, a DO app-spec update or Cloudflare Worker write that the operator had already approved in the CLAUDE.md §22.B / §24.D Q/A flow was blocked a second time by the harness Bash permission layer, stalling the session until someone clicked through (or failing outright in unattended-adjacent web sessions). The allowlist removes only that harness prompt; the CLAUDE.md policy layer is unchanged, so Claude still asks in Q/A format before any mutation, and reads remain self-serve per §22.A / §24.B. Rules are command-prefix matches, so sessions compose approved mutations in the canonical form `curl -sS -X PUT https://api.digitalocean.com/... -H ... -d @spec.json` (method and URL first). `DELETE` is deliberately not allowlisted: destroys keep the interactive prompt as a second gate.

| The numbers that matter | Value |
| --- | --- |
| Allow rules added | 6 (PUT / POST / PATCH × 2 hosts) |
| Hosts covered | `api.digitalocean.com`, `api.cloudflare.com` |
| Files changed | `.claude/settings.json`, `workflow-templates/.claude/settings.json` |
| Methods still prompting | `DELETE` (and any non-canonical command form) |

What this means for operators: after you answer the §2 Q/A approval, Claude runs the DO / Cloudflare write itself, with no further prompt to babysit. Consumer repos pick the same behaviour up automatically on their next `.claude/` assets sync from the `stable` ref (daily 04:00 UTC cron or the `@stable` `repository_dispatch`).

### For contributors

The template copy at `workflow-templates/.claude/settings.json` is the one the `Sync .claude/ assets from upstream` step of `update_workflows.yml` mirrors into consumers; the repo-root copy governs sessions in this repo only. Both carry the identical `permissions` block. Unattended pipelines are unaffected — they read `unattended_system_instructions.md` and never load these session settings.
