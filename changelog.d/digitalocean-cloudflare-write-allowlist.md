<!-- changelog: added -->
- **Interactive Claude Code sessions can now execute approved DigitalOcean and Cloudflare API writes without a second permission prompt.** `.claude/settings.json` gains a `permissions.allow` list covering `curl` PUT / POST / PATCH calls to `api.digitalocean.com` and `api.cloudflare.com`.

Until now, a DO app-spec update or Cloudflare Worker write that the operator had already approved in the CLAUDE.md §22.B / §24.D Q/A flow was blocked a second time by the harness Bash permission layer, stalling the session until someone clicked through (or failing outright in unattended-adjacent web sessions). The allowlist removes only that harness prompt; the CLAUDE.md policy layer is unchanged, so Claude still asks in Q/A format before any mutation, and reads remain self-serve per §22.A / §24.B. Rules are command-prefix matches, so sessions compose approved mutations in the canonical form `curl -q -sS -X PUT https://api.digitalocean.com/... -H ... -d @spec.json` (`-q` first disables implicit curl config, followed by the method and URL). The existing Bash PreToolUse hook checks the complete command and restores the harness prompt when later curl options, redirects, command substitutions, or chained commands could override or extend the allowlisted operation. `DELETE` is deliberately not allowlisted: destroys keep the interactive prompt as a second gate.

| The numbers that matter | Value |
| --- | --- |
| Allow rules added | 6 (PUT / POST / PATCH × 2 hosts) |
| Hosts covered | `api.digitalocean.com`, `api.cloudflare.com` |
| Files changed | Settings and mirrored PreToolUse hook copies under `.claude/` and `workflow-templates/.claude/` |
| Methods still prompting | `DELETE` (and any non-canonical command form) |

What this means for operators: after you answer the §2 Q/A approval, Claude runs the DO / Cloudflare write itself, with no further prompt to babysit. Consumer repos pick the same behaviour up automatically on their next `.claude/` assets sync from the `stable` ref (daily 04:00 UTC cron or the `@stable` `repository_dispatch`).

### For contributors

The template copies under `workflow-templates/.claude/` are the ones the `Sync .claude/ assets from upstream` step of `update_workflows.yml` mirrors into consumers; the repo-root copies govern sessions in this repo only. Both settings and hook pairs remain identical. Unattended pipelines are unaffected — they read `unattended_system_instructions.md` and never load these session settings.
