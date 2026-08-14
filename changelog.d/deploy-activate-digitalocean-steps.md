<!-- changelog: changed -->
- **`/deploy-activate` now executes DigitalOcean steps itself when `DIGITALOCEAN_ACCESS_TOKEN` is present, instead of handing every DO command to the operator to paste.** Applies to both this repo's command and the consumer-repo template.

The command's "you guide, I execute" contract gains one carve-out, wired to CLAUDE.md §22. When the session environment carries `DIGITALOCEAN_ACCESS_TOKEN`, the session runs DigitalOcean API calls directly (`doctl` when installed, REST otherwise): reads such as app specs, deployed env vars, deployment status, and logs are self-serve at any point while building the runbook, and mutations such as spec/env-var updates or forced redeploys still go through the one-step-at-a-time loop — the step is emitted with the exact resource and change named, and only runs after the operator confirms it. Resource IDs are resolved from the `## DigitalOcean resources` table in the repo's root agents file (`agents.md` here, `AGENTS.md` in consumer repos): the session uses recorded IDs without re-asking, and when an ID is missing it asks once in Q/A format, verifies the ID resolves with a read call, and records it in the table in the same push as the activation log so no future session asks again. Without the token (or on 401/403), the command falls back to its original guide-and-paste mode for the DigitalOcean steps.

| The numbers that matter | Value |
| --- | --- |
| Files changed | `.claude/commands/deploy-activate.md`, `workflow-templates/.claude/commands/deploy-activate.md` |
| New command section | `## DigitalOcean Steps` |
| Env var gating the behavior | `DIGITALOCEAN_ACCESS_TOKEN` (session environment) |
| ID registry | `## DigitalOcean resources` in the root agents file (CLAUDE.md §22.C) |

What this means for operators: during a `/deploy-activate` run, DigitalOcean steps no longer bounce to your terminal when the session has the token — the session reads DO state itself and performs the confirmed mutation for you, then shows the API output. The consumer-repo template picks the change up on the next `@stable` sync; repos without the token see no behavior change.

### For contributors

Token hygiene and the ask-first mutation posture of CLAUDE.md §22 are unchanged — the command's step-confirmation loop is what satisfies §22.B's approval requirement. The unattended pipelines never read `.claude/commands/`, so no codex-driven phase gains DigitalOcean access from this change.
