# Deploy-Activation Log — Protected main for source-repo support gates (shared operator step, batch)

- Reference: https://github.com/shubhodeep1/coding-workflows/issues/6211, shared step "Confirm source main-branch protection"   (+ entries pr-6204, pr-6509, pr-6550, pr-6938 — all PRs in shubhodeep1/coding-workflows)
- Deploy target: shubhodeep1/coding-workflows
- How it runs: runtime gate. unblock_judge.yml:64, security-audit.yml:153, review_autofix.yml:466 and issue_pr_status.yml:1106 read `repos/shubhodeep1/coding-workflows/branches/main` and refuse source-repo support unless `.protected == true`
- Status: LIVE
- Last updated: 2026-10-26
- Last note: All steps done. main is protected (ruleset 14197800) and requires `lint` + `ai-review/head-gate` (ruleset 25851281). All 4 entries ticked. LIVE.
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/6211 — keys pr-6204, pr-6509, pr-6550, pr-6938

## Runbook
1. [x] Confirm main reports protected=true   — done 2026-10-26: `gh api repos/shubhodeep1/coding-workflows/branches/main` → `protected: true`; already configured, verified (no action needed)   covers pr-6204 «Confirm source main-branch protection», pr-6509 «Source audit requires protected main», pr-6550 «Ensure the trusted main branch is protected»; the protected=true part of pr-6938 «Protect main for trusted review support»
2. [x] Create ruleset "Required checks for default branch" (required_status_checks: `lint` integration 15368, `ai-review/head-gate`)   — done 2026-10-26: operator ran `gh api -X POST .../rulesets` on Mac → ruleset 25851281, enforcement active   covers pr-6938 «Protect main for trusted review support»
3. [x] Verify LIVE   — done 2026-10-26: `rules/branches/main` lists 14197800 (deletion, non_fast_forward, copilot_code_review) and 25851281 (required_status_checks lint + ai-review/head-gate); `branches/main` protected=true; pr-6938 ticked

## Notes
- 2026-10-26 read-only evidence:
  - `branches/main` → `protected: true`. Classic branch protection: none (`branches/main/protection` → 404 "Branch not protected").
  - Protection comes from repository ruleset 14197800 "Copilot review for default branch": enforcement `active`, target `branch`, include `~DEFAULT_BRANCH`, `~ALL`; rules `deletion`, `non_fast_forward`, `copilot_code_review`. No `required_status_checks` rule. A second ruleset, 25516414 "promote-candidate pipeline-only", is also active.
  - All four workflow gates on origin/main test only `.protected == true` on branches/main, so the gate is satisfied.
- Ticks on #6211 (2026-10-26), this batch's step only. Each was made from the live body, saved with mcp__github__update_issue_comment, then re-read and matched byte-for-byte. No newer pipeline entry comment exists for these keys:
  - pr-6204 (comment 6016877098): Confirm source main-branch protection
  - pr-6509 (comment 6035928540): Source audit requires protected main
  - pr-6550 (comment 6066861711): Ensure the trusted main branch is protected
- pr-6938 (comment 6086488579) «Protect main for trusted review support» was held back at first. Its text also asks for the CI lint check and the ai-review/head-gate status to be required, and ruleset 14197800 had no required status checks. Operator answered Q1: A, to do it from the CLI.
- Assumed (operator did not override): Q2: A, a new ruleset scoped to `~DEFAULT_BRANCH` rather than editing 14197800, which also covers `~ALL`. Q3: A, no bypass actors, because the pipeline merges with the owner's GH_PAT and an admin bypass would let those merges skip the checks. Q4: A, strict up-to-date policy off, because the pipeline already handles base freshness.
- Pre-check: both checks had already reported on recent merged PR heads (#8175, #8173): `lint` is a check run from GitHub Actions app 15368, and `ai-review/head-gate` is a commit status. README.md:3518 documents requiring `ai-review/head-gate`.
- Side effect: a PR whose review never posts `ai-review/head-gate` (for example a PR the review workflow does not run on) cannot merge until that status succeeds.
- Tick on #6211 (2026-10-26): pr-6938 (comment 6086488579) «Protect main for trusted review support». Made from the live body (unchanged since planning) and re-read after save; it matched. No newer pipeline entry comment exists for pr-6938.
- Only this shared step is in scope. The entries' other steps are handled in their own sessions.
