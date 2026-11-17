# Deploy-Activation Logs

Per-project activation-status logs written by the `/deploy-activate` command
(`.claude/commands/deploy-activate.md`).

Each project being deployed to LIVE gets one file here:
`docs/deploy-activation/<ref-slug>.md`. The slug is derived deterministically
from the project reference so the same project always maps to the same file:

- issue → `issue-<N>.md`
- PR → `pr-<N>.md`
- plan doc → `plan-<basename-without-extension>.md`
- bare feature name → `feature-<kebab-case>.md`

The command **reads the matching log first** before emitting any step and
**resumes from the first step not marked `[x]`**, so a fresh session — a new
machine, a re-cloned container, or just a later day — picks up exactly where
the previous session stopped instead of restarting the runbook. After each
confirmed step the command marks it done, refreshes the timestamp, and commits
& pushes the log.

See the **Activation Log** section of `.claude/commands/deploy-activate.md` for
the full file format and the read/update/persist contract.
