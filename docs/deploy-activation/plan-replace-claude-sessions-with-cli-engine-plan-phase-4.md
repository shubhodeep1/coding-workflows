# Deploy-Activation Log — Claude token broker (replace-claude-sessions Phase 4)

- Reference: docs/plans/replace-claude-sessions-with-cli-engine-plan.md, Phase 4 (token broker)
  (+ landed on main via #6179 squash merge 1e213c2; Phase 4 PR #6175 still open;
  shubhodeep1/claude-workers#2 key-sync PR)
- Deploy target: shubhodeep1/coding-workflows (+ shubhodeep1/claude-workers, Cloudflare `FT_GAMES_CF` account)
- How it runs: on demand — every job with a Claude role calls `.github/actions/claude-pool-token`, which POSTs a GitHub OIDC token to `https://claude-pool-broker.shubhodeep.workers.dev/v1/pool`; the pool secret is refreshed by `claude-workers` `claude-pool-key-sync.yml` (cron hourly at :23, push to main, workflow_dispatch)
- Status: IN_PROGRESS
- Last updated: 2026-10-04
- Last note: Runbook built after Phase 4 reached main; Step 1 emitted.

## Runbook
1. [ ] Prereqs: Homebrew, gh, Node, `gh auth login`, read claude-workers secret names
2. [ ] Cloudflare API token (Workers Scripts: Edit, ft.games account) → `CF_BROKER_DEPLOY_TOKEN` secret in shubhodeep1/claude-workers
3. [ ] `CLAUDE_POOL_TOKEN_<NAME>` secret per pool account in shubhodeep1/claude-workers (drop TEST1/TEST2 if not members)
4. [ ] Merge shubhodeep1/claude-workers#2 (push to main runs the first key sync)
5. [ ] Verify key sync: run green with N accounts; Worker secret `CLAUDE_POOL_TOKENS` present
6. [ ] Dispatch `claude-engine-smoke.yml` on main; verify `CLAUDE_POOL available=true accounts=N` and probe lines in both legs
7. [ ] Verify LIVE: an hourly scheduled key-sync run succeeds; `@stable` carries the `id-token: write` wrappers (automatic daily promotion)

## Notes
- Completeness (2026-10-04): `tools/claude-pool-broker/`, `.github/actions/claude-pool-token/`,
  `scripts/claude_pool_token.sh`, `.github/ai/claude_engine.json` (`broker_url` set), the
  `ci.yml` vitest step, `id-token: write` in `claude-engine-smoke.yml` and in 10 of 16
  `workflow-templates/ai-*.yml` wrappers (the 6 without it call no Claude role), and the
  agents.md "Cloudflare resources" row are all on main at 1e213c2.
- Deployed Worker (modified 2026-10-04T03:30Z) matches main's `src/index.ts`. The open
  PR #6175 head carries later review fixes (case-insensitive owner / `job_workflow_ref`,
  `Bearer` regex, self-repo bypass of the registry). When #6175 merges, the Worker must be
  redeployed from main.
- Worker secrets were empty at runbook build (expected until the key sync runs).
- `claude-engine-smoke.yml` passes even with `available=false` (it asserts the D1 fallback),
  so a green run alone is not proof; Step 6 checks the `CLAUDE_POOL available=true` log line.
- Slug carries `-phase-4` so later phases of the same plan get their own log.
- No `/implement-plan-claude` progress log exists for this plan, so there are no auto-decisions to review.
