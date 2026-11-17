# Deploy-Activation Log — Telegram alert delivery (shared operator step, batch)

- Reference: https://github.com/shubhodeep1/coding-workflows/issues/6211, shared step "Configure Telegram alert delivery"   (+ entries pr-6462, pr-6951, pr-6655, pr-7156, pr-7176, pr-7201, pr-6980 — all PRs in shubhodeep1/coding-workflows, all merged 2026-10-07..2026-10-12)
- Deploy target: shubhodeep1/coding-workflows (this repo only; consumer repos out of scope per operator answer Q1: A)
- How it runs: cron from default branch (orchestrate poller) + pull_request (review, check-failure triage); alerts sent by scripts/tg_helpers.sh, which skips silently when TG_BOT_SECRET or TG_ADMIN_CHAT_ID is empty
- Status: LIVE
- Last updated: 2026-10-25
- Last note: All 3 steps done; Telegram delivery verified in poller run 40162977139. LIVE.

## Runbook
1. [x] Confirm in repo Settings → Secrets and variables → Actions: TG_BOT_SECRET secret present, TG_ADMIN_CHAT_ID variable present, ALERT_MSG_LEVEL absent or WARNING   — done 2026-10-25: operator confirmed on the settings page
2. [x] Tick the Telegram checkbox in each of the 7 entry comments on #6211   — done 2026-10-25: ticked by this session at operator request ("you tick"); each saved body re-read and matched the expected one-line change
3. [x] Verify LIVE (re-read #6211 entries and a fresh poller run log)   — done 2026-10-25: scheduled poller run 40162977139 (04:44 UTC, success) shows TG_BOT_SECRET=***, TG_ADMIN_CHAT_ID set, ALERT_MSG_LEVEL=WARNING, no "Telegram send failed" warnings

## Notes
- 2026-10-25 pre-check (read-only, from run logs):
  - orchestrate_poll run 40160712340 (schedule, 2026-10-25 04:07 UTC): TG_BOT_SECRET=*** (set), TG_ADMIN_CHAT_ID set, ALERT_MSG_LEVEL=WARNING.
  - internal-check-failure-triage run 40108741912: same three values.
  - internal-review.yml calls review_autofix.yml with `secrets: inherit`, so review gets the same repo secret.
  - Direct reads of repo secrets/variables are refused by the web-session proxy (HTTP 403), so the settings page check is Step 1.
- The co-listed credentials in the Telegram boxes of pr-6655, pr-7156, pr-7201 and pr-6980 are also in place here: poller run 40160712340 shows GH_PAT=*** and OPENROUTER_API_KEY=***; check_failure_triage.yml:54-60 declares GH_PAT, CHECK_TRIAGE_ISSUES_TOKEN and OPENROUTER_API_KEY required, and triage run 40108741912 succeeded. pr-6655's box also names consumer repos, which are out of scope (Q1: A).
- Q2: A — ALERT_MSG_LEVEL stays WARNING (pr-7156 intent). pr-7176 asked for DEBUG to deliver gated-only Claude pool notices (scripts/claude_pool_health_alert.sh:141-145); operator declined because the variable is repo-wide and DEBUG would page on every DEBUG alert. Rejected-token / failed-probe notices still send at WARNING.
- Only the Telegram checkbox of each entry is in scope; the entries' other steps are handled in their own sessions.
- Ticks on #6211 (2026-10-25), Telegram step only:
  - pr-6462 (comment 6031951051): Configure the CRITICAL alert destination
  - pr-6951 (comment 6084132710): Confirm Telegram delivery is enabled
  - pr-6655 (comment 6100538172): Ensure outage credentials and alert destination are configured — done for coding-workflows only; consumer repos not covered by this batch
  - pr-7156 (comment 6110720388): Confirm notification and poller configuration
  - pr-7176 (comment 6111055899): Enable delivery of gated-account notices — closed as declined: ALERT_MSG_LEVEL stays WARNING (Q2: A), so gated-only pool notices are not delivered by choice
  - pr-7201 (comment 6117130598): Provision dispatch and alert credentials
  - pr-6980 (comment 6123553308): Confirm triage credentials and warning delivery
  - The pr-7176 / pr-6655 notes live here, not in the comments, because the tick instruction asked for every other byte of each comment to stay unchanged.
  - pr-6462's comment was edited again at 04:58:17Z by another session (ticked "Provide review and audit credentials", added a footer); the Telegram tick survived and that edit was left alone.
