# Deploy-Activation Log — batch: GH_PAT and OPENROUTER_API_KEY Actions secrets

- Reference: https://github.com/shubhodeep1/coding-workflows/issues/6211 (operator-steps issue), shared step "Ensure required Actions credentials are configured"
  (+ secondary refs, each "Activation of PR #N" in shubhodeep1/coding-workflows: #6201, #6204, #6433, #6136, #6567, #6273, #6438, #6462, #6509, #6606, #6610, #6679, #6704, #6709, #6777, #6550, #6555, #6569, #6649, #6938, #7058, #6655, #6674, #6728, #7147, #7152, #6877, #7201, #6980, #7213, #7036, #7270, #7062, #7294)
- Deploy target: shubhodeep1/coding-workflows and the 13 consumers in `.github/ai/consumer_repos.json`
- How it runs: secrets are read by every AI workflow run (pull_request, schedule, workflow_dispatch, repository_dispatch); consumer copies are kept present by `.github/workflows/propagate-consumer-secrets.yml` (weekly cron `23 5 * * 1`, only-missing mode)
- Status: LIVE
- Last updated: 2026-10-25
- Last note: Both secrets verified present and working in the source and every consumer. Operator skipped the credit top-up; balance $552.94 on 2026-10-25 (about 5–6 days at current spend).

## Scope

Only the GH_PAT and OPENROUTER_API_KEY secrets. Other secrets some entries
mention in the same checklist item (CHECK_TRIAGE_ISSUES_TOKEN for #6273,
#6709, #6728, #6980, #7062; TG_BOT_SECRET / TG_ADMIN_CHAT_ID for #6655,
#6980, #7201), repository settings (Allow auto-merge for #6938 / #7294,
required status check for #6438) and wrapper installation (#6777) belong to
the entries' own sessions.

## Union of required access

GH_PAT, in coding-workflows and every consumer:
- repository contents read/write, push branches, create and update PRs, enable auto-merge, update PR branches;
- workflow-file writes (`workflow` scope);
- issues read/write; Actions read (runs, logs) and write (workflow dispatch, reruns);
- commit-status write (`ai-review/head-gate`);
- source token: `repository_dispatch` to, and secret writes on, every consumer;
- must resolve an identity via `gh api user`.
A classic PAT with `repo` + `workflow` covers all of it.

OPENROUTER_API_KEY, in coding-workflows and every consumer that runs AI
workflows: a working inference key with credit.

## Runbook
1. [x] Source repo: GH_PAT and OPENROUTER_API_KEY set and working — done 2026-10-25: propagation run 39205226115 ran with non-empty values (no `skipped_empty` warning); E2E gate run 40113559006, validation-refresh 40155200919, security-audit 39110831927 and internal-review 40161403109 all succeeded; OpenRouter key `coding-workflows github` enabled, no limit, $679 spent in the last 7 days.
2. [x] Source GH_PAT has the union of permissions — done 2026-10-25: release job 120951846137 sent `repository_dispatch` to all 13 consumers with no "Failed to dispatch" warning; the propagation run listed secrets on all 13 consumers; E2E gate dispatches workflows; `ai-review/head-gate` statuses posted as shubhodeep1 on PR #8102/#8101 heads; sync-claude-live-copies run 39593441015 pushed and opened a PR.
3. [x] Consumers: both secrets present in all 13 — done 2026-10-25: propagation run 39205226115 (2026-10-19) reported `status=skipped_present` for GH_PAT and OPENROUTER_API_KEY in every consumer, `summary targets=13 set=0 skipped=52 failed=0`.
4. [x] Consumers: secrets working where workflows use them — done 2026-10-25: ai-update-workflows succeeded on the 2026-10-24 release dispatch and the 2026-10-25 cron in digital_pa, fun-token-multi-chain, binance-blessings, multi-user-ai-agent, bitsafe.io, hylifegroup.com, radateeree-resort.com and drhyg_ecommerce_automation; ai-review and ai-orchestrate-poll succeeded on 2026-10-25 in those 8 plus tele-funtoken-msg-scoring. Validate failures in fun-token-multi-chain (run 39631403428) and binance-blessings (run 38107224535) are harness errors with both secrets present, not credential failures.
5. [x] Top up OpenRouter credit (operator, in the OpenRouter dashboard) — skipped 2026-10-25 by operator choice; not a secret-configuration gate — balance $553.67 ($99,690 bought, $99,136.33 used) against about $679/week of spend.
6. [x] Verify LIVE — done 2026-10-25: steps 1–4 evidence stands; `GET /api/v1/credits` re-read shows $552.94 remaining. Risk: AI workflows fail once credit runs out unless it is topped up.

## Ticks in #6211 (2026-10-25)

Ticked (`- [ ]` → `- [x]`) in the single owner-authored entry comment for each key, nothing else changed:
pr-6204 "Supply runtime credentials"; pr-6433 "Ensure required Actions credentials are configured"; pr-6136 "Ensure required credentials exist"; pr-6567 "Provide required validation credentials"; pr-6273 "Required credentials must be present"; pr-6438 "Ensure required review credentials are configured"; pr-6462 "Provide review and audit credentials"; pr-6509 "Required automation credentials must be present"; pr-6606 "Confirm required automation secrets"; pr-6610 "Ensure required audit credentials are present"; pr-6679 "Verify required gate credentials"; pr-6704 "Confirm required Actions credentials"; pr-6709 "Ensure source credentials can be propagated"; pr-6550 "Ensure judge credentials are configured"; pr-6555 "Provision release and smoke-test credentials"; pr-6569 "Required credentials must be configured"; pr-6649 "Provision required credentials where unset"; pr-6938 "Provide review credentials and permit auto-merge"; pr-7058 "Confirm source release credentials" and "Confirm consumer sync credentials"; pr-6674 "Verify release and sync credentials"; pr-6728 "Ensure triage credentials are configured"; pr-7147 "Ensure release and validation credentials are configured"; pr-7152 "Confirm required workflow credentials"; pr-6877 "Ensure release-gate credentials are configured"; pr-7201 "Provision dispatch and alert credentials"; pr-7213 "Ensure release and updater credentials are configured"; pr-7036 "Ensure full-gate credentials are configured"; pr-7270 "Confirm the cross-repository PAT"; pr-7062 "Ensure required credentials are provisioned"; pr-7294 "Ensure release dispatch has cross-repository credentials".

Already ticked before this session: pr-6655 "Ensure outage credentials and alert destination are configured"; pr-6980 "Confirm triage credentials and warning delivery".

Extra evidence for bundled requirements: CHECK_TRIAGE_ISSUES_TOKEN and TG_BOT_SECRET present in all 13 consumers (run 39205226115); triage issues (`ai:check-triage`) created as shubhodeep1 in the source and 7 consumers; source poller run 40162977139 has TG_BOT_SECRET and TG_ADMIN_CHAT_ID set, ALERT_MSG_LEVEL=WARNING; coding-workflows has Allow auto-merge on.

Left unticked:
- pr-6201 "Ensure required credentials and audit setting are active" (issue body): SINGLE_ISSUE_SECURITY_PASS_ENABLED / SECURITY_AUDIT_ENABLED in consumers cannot be read (repo-variable API is refused by the proxy).
- pr-6777 "Ensure consumer sync credentials and settings are present": btc_sweeper, atlas-bridge.gd, mongo-explorer and fbc_shutdown have no ai-update-workflows.yml.
- pr-7294 "Ensure each consumer can deliver and merge its update PR": Allow auto-merge is off in those same 4 consumers.
- pr-6438 "Enforce the head-bound review status": a branch-protection step, not a credential.

## Notes
- tele-funtoken-msg-scoring: ai-update-workflows runs are `skipped` because the repo variable `AI_WORKFLOW_AUTO_UPDATE_ENABLED` is `false` (a deliberate opt-out, not a credential gap). Its AI Review, validate and poller runs succeed.
- btc_sweeper, atlas-bridge.gd, mongo-explorer and fbc_shutdown have no ai-update-workflows.yml (and mostly no AI review/poller wrappers), so nothing exercises their copies of the secrets; the secrets are present per step 3. Installing the updater there is #6777's wrapper step, outside this batch.
- Secret presence was read from workflow logs; the agent proxy refuses the Actions secrets API, and no secret value was read or printed.
