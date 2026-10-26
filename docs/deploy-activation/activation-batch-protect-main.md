# Deploy-Activation Log — Protected main for source-repo support gates (shared operator step, batch)

- Reference: https://github.com/shubhodeep1/coding-workflows/issues/6211, shared step "Confirm source main-branch protection"   (+ entries pr-6204, pr-6509, pr-6550, pr-6938 — all PRs in shubhodeep1/coding-workflows)
- Deploy target: shubhodeep1/coding-workflows
- How it runs: runtime gate. unblock_judge.yml:64, security-audit.yml:153, review_autofix.yml:466 and issue_pr_status.yml:1106 read `repos/shubhodeep1/coding-workflows/branches/main` and refuse source-repo support unless `.protected == true`
- Status: IN_PROGRESS
- Last updated: 2026-10-26
- Last note: main already reports protected=true (ruleset 14197800). 3 of 4 entries ticked. pr-6938's step also asks for required status checks, which are not configured; waiting on operator Q1.
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/6211 — keys pr-6204, pr-6509, pr-6550, pr-6938

## Runbook
1. [x] Confirm main reports protected=true   — done 2026-10-26: `gh api repos/shubhodeep1/coding-workflows/branches/main` → `protected: true`; already configured, verified (no action needed)   covers pr-6204 «Confirm source main-branch protection», pr-6509 «Source audit requires protected main», pr-6550 «Ensure the trusted main branch is protected»; the protected=true part of pr-6938 «Protect main for trusted review support»
2. [ ] pr-6938 extra: required status checks (CI `lint`, `ai-review/head-gate`) on main   — pending operator decision Q1   covers pr-6938 «Protect main for trusted review support»
3. [ ] Verify LIVE

## Notes
- 2026-10-26 read-only evidence:
  - `branches/main` → `protected: true`. Classic branch protection: none (`branches/main/protection` → 404 "Branch not protected").
  - Protection comes from repository ruleset 14197800 "Copilot review for default branch": enforcement `active`, target `branch`, include `~DEFAULT_BRANCH`, `~ALL`; rules `deletion`, `non_fast_forward`, `copilot_code_review`. No `required_status_checks` rule. A second ruleset, 25516414 "promote-candidate pipeline-only", is also active.
  - All four workflow gates on origin/main test only `.protected == true` on branches/main, so the gate is satisfied.
- Ticks on #6211 (2026-10-26), this batch's step only. Each was made from the live body, saved with mcp__github__update_issue_comment, then re-read and matched byte-for-byte. No newer pipeline entry comment exists for these keys:
  - pr-6204 (comment 6016877098): Confirm source main-branch protection
  - pr-6509 (comment 6035928540): Source audit requires protected main
  - pr-6550 (comment 6066861711): Ensure the trusted main branch is protected
- Not ticked: pr-6938 (comment 6086488579) «Protect main for trusted review support». Its text also says "Require the CI lint check and the head-bound ai-review/head-gate status". The ruleset has no required status checks, so this step is only partly done.
- Only this shared step is in scope. The entries' other steps are handled in their own sessions.
