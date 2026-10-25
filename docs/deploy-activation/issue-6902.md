# Deploy-Activation Log — Safeguarded consumer updater: attested stable release + consumer sync

- Reference: #6902 (orchestrator tracking issue; operator steps under key `project-6902` in #6211, entry of 2026-10-12)
  - Secondary refs, release/sync step only: the "release to @stable and let consumer repos sync" step of #6211 entries
    pr-6283, pr-6562, pr-6204, pr-6433, pr-6136, pr-6208, pr-6273, pr-6462, pr-6509, pr-6610, pr-6605, pr-6677,
    pr-6709, pr-6777, pr-6550, pr-6791, pr-6643, pr-6564, pr-6569, pr-6649, pr-6935, pr-6951, pr-6938, pr-6959,
    pr-7043, pr-7073, pr-6655, pr-7098, pr-6674, pr-7105, pr-7134, pr-6728, pr-7147, pr-7157, pr-7156, pr-7176,
    pr-6855, pr-6877, pr-6682, pr-7195, pr-7201, pr-7204, pr-7256, pr-7213, pr-7036, pr-7062, pr-7050, pr-7044,
    pr-7065, pr-7294. Their credential and feature-flag steps are out of scope (other sessions).
  - Out of scope here: project-6902 steps 2-5 (consumer `GH_PAT`, `UPDATER_VERIFY_RELEASE_MANIFEST`,
    `UPDATER_PR_DELIVERY_ENABLED`, auto-merge settings).
- Deploy target: shubhodeep1/coding-workflows -> 13 consumers in `.github/ai/consumer_repos.json`
- How it runs: release via `workflow_dispatch` of `test-and-mark-stable.yml` (manual); consumers update on
  `repository_dispatch` `coding-workflows-stable-released` plus daily cron 04:00 UTC (`workflow-templates/ai-update-workflows.yml:49-51`)
- Status: IN_PROGRESS
- Last updated: 2026-10-25
- Last note: Step 6 partial (8/13). Reading tele-funtoken-msg-scoring's wrapper and consumer GH_PAT presence; Q2/Q3 open on bootstrapping 4 repos.

## Runbook
1. [x] Prereqs on the Mac: Homebrew, gh, `gh auth login` with read access to all 13 consumers — done 2026-10-25: operator confirmed `done`
2. [x] `STABLE_RELEASE_ENABLED=true` on the source repo — done 2026-10-25 (already satisfied): release job env of run 40113559006 shows `STABLE_RELEASE_ENABLED: true`
3. [x] Publish attested stable release — done 2026-10-25 (already satisfied): `v1.40.0` released 2026-10-24T18:29Z by `test-and-mark-stable.yml` run 40113559006; steps "Attest sync manifest", "Attest release manifest", "Upload release manifest asset" all succeeded; release assets `release-manifest.v1.json`, `sync-manifest.json`; `stable` -> `8164dcf9593d`
4. [x] Dispatch to consumers — done 2026-10-25 (already satisfied): same run, step "Notify consumer repos via repository_dispatch" succeeded and logged a dispatch to all 13 registered consumers with no warnings
5. [x] Confirm the 50 PRs are in `@stable` — done 2026-10-25: all 50 merged to `main`; GitHub compare `<merge_commit>...stable` returned `ahead`, `behind_by=0` for every one
6. [ ] Verify each consumer's updater ran on or after the v1.40.0 dispatch (operator, consumers not attached to the session)
   - 2026-10-25 operator check, `schedule` runs on 2026-10-25, success: digital_pa, fun-token-multi-chain, binance-blessings,
     multi-user-ai-agent, bitsafe.io, hylifegroup.com, radateeree-resort.com, drhyg_ecommerce_automation (8/13)
   - skipped: tele-funtoken-msg-scoring (run 40161515398; job `if: vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false'`)
   - HTTP 404 on `ai-update-workflows.yml`: btc_sweeper, atlas-bridge.gd, mongo-explorer, fbc_shutdown
   - 2026-10-25 diagnosis: tele-funtoken-msg-scoring has `AI_WORKFLOW_AUTO_UPDATE_ENABLED` unset but every run since
     2026-10-24 (dispatch and schedule) skipped, at ~04:20 not 04:00, so its wrapper likely differs from the template.
     btc_sweeper and atlas-bridge.gd have 7 legacy `ai-*` wrappers and no updater; mongo-explorer and fbc_shutdown have
     no `ai-*` workflows at all (registered but never seeded). Bootstrap choices asked as Q2/Q3.
7. [ ] Bootstrap `ai-update-workflows.yml` on any consumer that never had it (only if step 6 finds one)
8. [ ] Verify LIVE

## Notes
- The local clone is shallow, so `git merge-base --is-ancestor` reported false negatives; containment was checked with the GitHub compare API instead.
- `main` is 13 commits ahead of `stable`. The latest `test-and-mark-stable.yml` run (40147488993, 2026-10-25T00:38Z) passed validation but skipped `release`. This does not affect the 50 PRs, which are all already in v1.40.0.
- Consumer repos are not attached to this cloud session (HTTP 403), so step 6 runs on the operator's machine.
- 2026-10-25: operator answered Q1: A. Once verified, tick the release/sync line in the latest #6211 `project-6902` comment and the
  release/sync line of the 50 `pr-*` entries (no other lines). No ticks made yet: step 6 is not verified.
