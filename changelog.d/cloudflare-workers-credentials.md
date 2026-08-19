<!-- changelog: added -->
- **Interactive Claude Code sessions can now manage Cloudflare Workers for funtoken.io, ft.games, and 5m.fun via two new session env vars, `FUNTOKEN_IO_CF` and `FT_GAMES_CF`.** A new CLAUDE.md §24 maps each credential to its sites, splits Cloudflare work into self-serve reads, self-serve Worker deploys the task calls for, and ask-first destructive writes, and reaches every consumer repo through the existing root `CLAUDE.md` sync.

Sessions previously had no standing policy for Cloudflare, so any Worker change bounced back to the operator. Each var holds one Cloudflare account's credentials as a single `<account_id>:<api_token>` string: `FUNTOKEN_IO_CF` covers the funtoken.io website, `FT_GAMES_CF` covers ft.games and 5m.fun, and the two are not interchangeable — §24.A pins the site-to-credential mapping, the first-colon split, and the transport (`wrangler` via `CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN`, or the REST API). §24.B makes read-only calls (Worker scripts, bindings, routes, deployments, DNS on covered zones, `wrangler tail` logs) an explicit carve-out from the §2 ask-first rule. §24.C makes creating and editing Workers self-serve when the task asks for it — that is what the credentials exist for — with validate-before-deploy and preserve-rollback constraints. §24.D keeps deletions, DNS/zone setting changes, KV/R2/D1 data wipes, and secret rotation behind a mandatory §2 Q/A confirmation, not superseded by §12. §24.F adds a per-repo `## Cloudflare resources` registry to the root agents file, mirroring the §22.C DigitalOcean registry.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §24 |
| New env vars | `FUNTOKEN_IO_CF` (funtoken.io), `FT_GAMES_CF` (ft.games, 5m.fun) — session environment, not Actions secrets |
| Credential format | `<account_id>:<api_token>`, split on the first `:` |
| Actions workflows that read them | 0 |
| New `agents.md` section | `## Cloudflare resources` (identifier registry) |

What this means for operators: set `FUNTOKEN_IO_CF` and/or `FT_GAMES_CF` in the Claude Code session environment of any repo whose sessions should manage Workers for those sites; nothing else to install. The §24 rules and the registry convention arrive in consumer repos on the next `@stable` sync via the root `CLAUDE.md` mirror in `update_workflows.yml`. Sessions never print the credentials, and a missing or rejected token degrades to "report once and continue" rather than a retry loop.

### For contributors

The unattended pipelines read `unattended_system_instructions.md` and never see `CLAUDE.md`, so §24 governs interactive sessions only — no codex-driven phase gains Cloudflare access from this change. §24.G forbids committing any workflow, script, or hook that reads either var from the session environment; Actions-side Cloudflare work would need its own repo secret and its own review. Registry entries are identifiers under §6.
