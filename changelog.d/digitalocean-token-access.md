<!-- changelog: added -->
- **Interactive Claude Code sessions can now query DigitalOcean directly via `DIGITALOCEAN_ACCESS_TOKEN`, instead of asking the operator to fetch data by hand.** A new CLAUDE.md §22 splits DigitalOcean work into self-serve reads and ask-first mutations, and reaches every consumer repo through the existing root `CLAUDE.md` sync.

Sessions previously had no standing policy for DigitalOcean, so any question about a deployed app's env vars, logs, or deployment status bounced back to the operator. §22.A makes read-only calls (app specs, deployed env vars, build/deploy/runtime logs, deployment status, metrics) an explicit carve-out from the §2 ask-first rule: the session pulls the data itself, via `doctl` or the REST API with the `DIGITALOCEAN_ACCESS_TOKEN` env var. §22.B keeps provisioning and every other mutation (creating, resizing, destroying, redeploying, spec or env var changes) behind a mandatory §2 Q/A confirmation that names the resource type, size, region, and billing impact — not superseded by §12's proactive scope. §22.C adds a per-repo `## DigitalOcean resources` registry to the root agents file (`agents.md` here, `AGENTS.md` in consumer repos): sessions read app/db IDs from the table first, ask once when an ID is missing, and record it in the same PR so it is never asked for again. The pre-task context-loading rule now names both `agents.md` and `AGENTS.md` casings so the every-session read applies in every repo.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §22 |
| New env var | `DIGITALOCEAN_ACCESS_TOKEN` (session environment, not an Actions secret) |
| Actions workflows that read the token | 0 |
| New `agents.md` section | `## DigitalOcean resources` (ID registry) |

What this means for operators: set `DIGITALOCEAN_ACCESS_TOKEN` in the Claude Code session environment of any repo where sessions should verify DigitalOcean state themselves; nothing else to install. The §22 rules and the registry convention arrive in consumer repos on the next `@stable` sync via the root `CLAUDE.md` mirror in `update_workflows.yml`. Sessions never print the token, and a missing or expired token degrades to "report and continue" rather than a retry loop.

### For contributors

The unattended pipelines read `unattended_system_instructions.md` and never see `CLAUDE.md`, so §22 governs interactive sessions only — no codex-driven phase gains DigitalOcean access from this change. Registry entries are identifiers under §6: correcting or removing a recorded ID goes through the §2 ask flow.
