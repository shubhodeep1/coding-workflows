# Deploy-Activation Log — Claude token broker (replace-claude-sessions Phase 4)

- Reference: docs/plans/replace-claude-sessions-with-cli-engine-plan.md, Phase 4 (token broker)
  (+ landed on main via #6179 squash merge 1e213c2; Phase 4 PR #6175 still open;
  shubhodeep1/claude-workers#2 key-sync PR)
- Deploy target: shubhodeep1/coding-workflows (+ shubhodeep1/claude-workers, Cloudflare `FT_GAMES_CF` account)
- How it runs: on demand — every job with a Claude role calls `.github/actions/claude-pool-token`, which POSTs a GitHub OIDC token to `https://claude-pool-broker.shubhodeep.workers.dev/v1/pool`; the pool secret is refreshed by `claude-workers` `claude-pool-key-sync.yml` (cron hourly at :23, push to main, workflow_dispatch)
- Status: LIVE
- Last updated: 2026-10-06
- Last note: LIVE on the plan's own done criteria (operator Q5: A, 2026-10-06). Smoke run green with `available=true`, broker redeployed, hourly key sync green. The `@stable` carry of 1e213c2 / ba87459 is a follow-up that waits on the next promote cycle (see Notes); the hourly session check-in is cancelled.

## Runbook
1. [x] Prereqs: Homebrew, gh, Node, `gh auth login`, read claude-workers secret names   — done 2026-10-04: `gh api user` = shubhodeep1; claude-workers secrets = CLAUDE_POOL_TOKEN_FUNTOKEN1, CLAUDE_POOL_TOKEN_FUNTOKEN2, GH_PAT
2. [x] Cloudflare API token (Workers Scripts: Edit, ft.games account) → `CF_BROKER_DEPLOY_TOKEN` secret in shubhodeep1/claude-workers   — done 2026-10-04: token check on `claude-pool-broker/secrets` returned `"success": true`; secret set; account ID verified equal to the `FT_GAMES_CF` account
3. [x] `CLAUDE_POOL_TOKEN_<NAME>` secret per pool account in shubhodeep1/claude-workers (drop TEST1/TEST2 if not members)   — skipped 2026-10-04: already satisfied, FUNTOKEN1 and FUNTOKEN2 present (set ~1 day earlier), no TEST1/TEST2
4. [x] Merge shubhodeep1/claude-workers#2 (push to main runs the first key sync)   — done 2026-10-04: squash-merged; push run 37191352286 started
5. [x] Verify key sync: run green with N accounts; Worker secret `CLAUDE_POOL_TOKENS` present   — done 2026-10-04: run 37191352286 success, log `CLAUDE_POOL key_sync status=ok accounts=2 names=FUNTOKEN1,FUNTOKEN2`; Worker secrets = [CLAUDE_POOL_TOKENS (secret_text)]
6. [x] Dispatch `claude-engine-smoke.yml` on main; verify `CLAUDE_POOL available=true accounts=N` and probe lines in both legs   — run 37191845530 (2026-10-04): broker part passed in both legs (`available=true reason=selected accounts=2`; probes FUNTOKEN1 five_hour=0.02 seven_day=0.21, FUNTOKEN2 five_hour=0.06 seven_day=0.21, status=allowed). Run failed: write leg Context gate `startup_input_tokens=29369` (limit 25,000; read leg 16,804); P5 denials and review-editor relay gate did not run. Held open until a fully green run (Q2: B). Fixed by #6193 (explicit write-profile tool list, merged ba87459)   — done 2026-10-04: re-run 37203346242 success; both legs `available=true reason=selected accounts=2` (probes FUNTOKEN1 five_hour=0.01 seven_day=0.22, FUNTOKEN2 five_hour=0.02 seven_day=0.22); write `startup_input_tokens=18079`, read 16,804; P5 denials ok; relay clarify ok; relay editor ok.
6b. [x] Redeploy `claude-pool-broker` from main (#6175 review fixes merged at c04fd1b; live Worker still the 2026-10-04T03:30Z build). Validated: vitest 31/31, `wrangler deploy --dry-run` OK. Executed by the session on operator `go` (§24.C)   — done 2026-10-04: `wrangler deploy` from main f80f1bb, version 59a53cf2-1cfb-474b-84ba-8149b4219376; no token → 403 missing_token; lowercase `bearer abc` → 403 malformed_token (new regex); live code has `Bearer[ \t]+…/i`; secret `CLAUDE_POOL_TOKENS` kept; previous versions 40a13740 / 43b1be43 retained for rollback.
7. [x] Verify LIVE: an hourly scheduled key-sync run succeeds   — done 2026-10-06 (operator Q5: A): scheduled key syncs green every hour, e.g. 37197582662 (2026-10-04) and 37388903760 (2026-10-05T23:30Z), each `CLAUDE_POOL key_sync status=ok accounts=2 names=FUNTOKEN1,FUNTOKEN2`.
8. [ ] Follow-up: verify `@stable` carries the `id-token: write` wrappers after automatic daily promotion   — `stable` is still df70982 (2026-10-03) and does not contain 1e213c2 / ba87459 (see Notes).

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
- Step 1 output showed `CF_BROKER_DEPLOY_TOKEN` is not set yet, so Step 2 is needed.
- Step 2 attempt 1 (2026-10-04): the operator's shell is zsh, where `read -p` means coprocess, so both values were empty; `curl` returned `"success":false` and `CF_BROKER_DEPLOY_TOKEN` was saved as `:`. Harmless until the key sync is merged (Step 4); the corrective step overwrites it.
- Log pushes continue on `claude/determined-pascal-kvyrbg-2`, `-3`, … (operator answer Q1: A, 2026-10-04): the first branch's PR #6186 merged and its ruleset blocks non-fast-forward pushes and deletion.
- Step 2 attempts 2–3 (2026-10-04): attempt 2 used another Cloudflare account's ID (not saved, guard held). Attempt 3 had the right account, but the check's pattern missed Cloudflare's pretty-printed `"success": true` (with a space), so nothing was saved. Attempt 4 used `grep -oE '"success": *[a-z]+'` and saved the secret.
- Step 5: the stored `CLAUDE_POOL_TOKEN_FUNTOKEN1` value ends with a newline; the sync strips whitespace (spike S3), so the broker gets a clean token.
- Step 7 `@stable` follow-up (2026-10-06): `promote-main-to-stable.yml` promotes only after a full proving cycle (`scripts/promote_main_cycle.sh`: smoke gate, then a PROVING `/apply-analysis` project labelled `ai:comprehensive-test-pending`, then the orchestrator promotes). Runs on 2026-10-04 and 2026-10-05 skipped with `PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=6031`. #6031 (the previous cycle) is still open with `ai:validation-failed` + `ai:harness-broken` and never promoted; it lost `ai:comprehensive-test-pending` at 2026-10-05T01:12Z, so the 2026-10-06T00:38Z run 37395055737 got past that check: its `promote` job was skipped and its `cycle` job was still running at 01:05Z (outcome not recorded here). Consumers get the `id-token: write` wrappers when a proving cycle promotes. Until then consumers on `@stable` keep running the pre-Phase-4 code, which makes no broker call, so nothing breaks. #6031 is being investigated in a separate session.
- Key sync 37370689560 (2026-10-05T20:35Z) was cancelled because no hosted runner picked up the job (no step ran); the next hourly run 37376555003 was green. The Worker keeps the last written pool, so a missed hour is harmless.
