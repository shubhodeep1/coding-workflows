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
  `repository_dispatch` `coding-workflows-stable-released` plus daily cron 04:00 UTC (`workflow-templates/ai-update-workflows.yml:48-50`)
- Status: LIVE
- Last updated: 2026-10-26
- Last note: LIVE. All 13 consumers synced to v1.40.2 (`5596e7520ef4`); #6211 release/sync lines ticked (37), 14 held with reasons in Notes.

## Runbook
1. [x] Prereqs on the Mac: Homebrew, gh, `gh auth login` with read access to all 13 consumers — done 2026-10-25: operator confirmed `done`
2. [x] `STABLE_RELEASE_ENABLED=true` on the source repo — done 2026-10-25 (already satisfied): release job env of run 40113559006 shows `STABLE_RELEASE_ENABLED: true`
3. [x] Publish attested stable release — done 2026-10-25 (already satisfied): `v1.40.0` released 2026-10-24T18:29Z by `test-and-mark-stable.yml` run 40113559006; steps "Attest sync manifest", "Attest release manifest", "Upload release manifest asset" all succeeded; release assets `release-manifest.v1.json`, `sync-manifest.json`; `stable` -> `8164dcf9593d`
4. [x] Dispatch to consumers — done 2026-10-25 (already satisfied): same run, step "Notify consumer repos via repository_dispatch" succeeded and logged a dispatch to all 13 registered consumers with no warnings
5. [x] Confirm the 50 PRs are in `@stable` — done 2026-10-25: all 50 merged to `main`; GitHub compare `<merge_commit>...stable` returned `ahead`, `behind_by=0` for every one
6. [x] Verify each consumer's updater ran on or after the v1.40.0 dispatch (operator, consumers not attached to the session)
   - 2026-10-25 operator check, `schedule` runs on 2026-10-25, success: digital_pa, fun-token-multi-chain, binance-blessings,
     multi-user-ai-agent, bitsafe.io, hylifegroup.com, radateeree-resort.com, drhyg_ecommerce_automation (8/13)
   - skipped: tele-funtoken-msg-scoring (run 40161515398; job `if: vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false'`)
   - HTTP 404 on `ai-update-workflows.yml`: btc_sweeper, atlas-bridge.gd, mongo-explorer, fbc_shutdown
   - 2026-10-25 diagnosis: tele-funtoken-msg-scoring has `AI_WORKFLOW_AUTO_UPDATE_ENABLED` unset but every run since
     2026-10-24 (dispatch and schedule) skipped, at ~04:20 not 04:00, so its wrapper likely differs from the template.
     btc_sweeper and atlas-bridge.gd have 7 legacy `ai-*` wrappers and no updater; mongo-explorer and fbc_shutdown have
     no `ai-*` workflows at all (registered but never seeded). Bootstrap choices asked as Q2/Q3.
   - 2026-10-25: all 5 outliers have the `GH_PAT` secret. tele-funtoken's wrapper matches the template but pins
     `update_workflows.yml@2820e97e43bd` (v1.38.3), whose `update-wrappers` job has
     `if: inputs.allow_workflow_edits != false`; run 40161515398 skipped both inner jobs, so the repo very likely has
     `ALLOW_WORKFLOW_EDITS=false`. Current stable (`8164dcf9593d`) removed that gate, but the repo cannot reach it
     because the old pin skips the run. Fix choice asked as Q4.
   - 2026-10-25: `ALLOW_WORKFLOW_EDITS` is NOT set in tele-funtoken, so the `false` input most likely comes from the
     wrapper's `with:` block (e.g. a hardcoded `allow_workflow_edits: false`). Reading that block next.
   - 2026-10-25 confirmed: tele-funtoken's wrapper hardcodes `allow_workflow_edits: false` with the comment "keep overwrites
     disabled until a least-privilege release is pinned" (tele-funtoken commit e591b7b, PR #4824, 2026-10-24). At v1.38.3
     17 templates used `secrets: inherit`; at current stable `8164dcf9593d` none do, so that condition is now met.
     Q4 re-asked: replace the wrapper with the current template (lifting the hold) or keep it.
7. [x] tele-funtoken-msg-scoring: PR replacing `ai-update-workflows.yml` with the v1.40.0 template rendered at `8164dcf9593d` (lifts the hardcoded `allow_workflow_edits: false` hold; Q4: A). Expected file sha256 `02b8fd3779a12157dc366b4a46b5ac2ba64d93674edb94b6fce65a5a1c835000`
   - 2026-10-25: file built on operator's Mac on branch `chore/updater-v1.40.0`; sha256 matched; diff is only the pin (`2820e97` -> `8164dcf`) and `allow_workflow_edits`.
   - done 2026-10-25: commit 81168db497 pushed; PR shubhodeep1/tele-funtoken-msg-scoring#4885 opened (not merged yet; merge is step 10).
8. [x] Set `WORKFLOW_PROFILE`: `core` on btc_sweeper and atlas-bridge.gd (Q2: A), `standard` on mongo-explorer and fbc_shutdown (Q3: A) — superseded by Q5: A, final `full` on all 4 (see step 9)
   - done 2026-10-26: operator created the variable in all 4 repos; `gh variable get` read back core, core, standard, standard.
9. [x] PRs adding the same rendered `ai-update-workflows.yml` to btc_sweeper, atlas-bridge.gd, mongo-explorer, fbc_shutdown
   - done 2026-10-26: checksum matched in all 4; PRs opened: shubhodeep1/btc_sweeper#231, shubhodeep1/atlas-bridge.gd#120,
     shubhodeep1/mongo-explorer#46, shubhodeep1/fbc_shutdown#14 (not merged yet). Case-collision warnings (`CLAUDE.md`/`claude.md`)
     on the macOS clones; only the new file was staged, PR file lists to be checked before merge.
   - 2026-10-26 pre-merge check: btc_sweeper#231, atlas-bridge.gd#120, mongo-explorer#46, fbc_shutdown#14 each change only
     `.github/workflows/ai-update-workflows.yml` and are MERGEABLE (AI review `codex-agent` still pending).
     tele-funtoken#4885 already MERGED 2026-10-25T12:21:47Z (by shubhodeep1).
   - CORRECTION 2026-10-26: mongo-explorer and fbc_shutdown are NOT unseeded. Each has 18 of the 19 `full` profile
     wrappers (all but `ai-update-workflows.yml`) plus non-managed workflows (claude-auto-review, qodo-pr-review, tier1-*).
     The earlier filtered listing returned nothing for them. With `WORKFLOW_PROFILE=standard` the updater only iterates the
     standard manifest, so the 4 full-only wrappers already present (ai-check-failure-triage, ai-memory-maintenance,
     ai-security-audit, ai-workflow-failure-heal) would stay at their old pin (README "Profile downgrades are
     non-destructive"). Q3 re-asked as Q5; their PRs are on hold until answered.
   - CORRECTION 2026-10-26: btc_sweeper and atlas-bridge.gd also have the same 18 of 19 `full` wrappers (the earlier
     listing was truncated by `head -8`). `core` would freeze 10 existing wrappers, so Q2 is re-asked together with Q5.
   - 2026-10-26: tele-funtoken-msg-scoring is syncing again: `repository_dispatch` updater runs succeeded at
     2026-10-25T14:46:48Z (v1.40.1) and 2026-10-25T20:09:49Z (v1.40.2).
   - 2026-10-26: operator answered Q5: A (replaces Q2/Q3). `WORKFLOW_PROFILE=full` set on all 4 repos ("Updated" x4);
     read back `full` for btc_sweeper, atlas-bridge.gd, mongo-explorer; fbc_shutdown re-checked 2026-10-26: `full`.
10. [x] Merge the 5 PRs and run each repo's `ai-update-workflows.yml` once
   - 2026-10-26: all 5 PRs MERGED (tele-funtoken#4885 on 2026-10-25; btc_sweeper#231, atlas-bridge.gd#120, mongo-explorer#46,
     fbc_shutdown#14 merged by the consumers' AI review pipeline). Next (done, see the next bullet): one manual updater run in the 4 newly installed repos.
   - done 2026-10-26: no earlier updater runs in the 4 repos (none ran under core/standard); operator dispatched
     `ai-update-workflows.yml`: btc_sweeper run 40254082471, atlas-bridge.gd 40254086692, mongo-explorer 40254090855,
     fbc_shutdown 40254095533.
11. [x] Verify LIVE: all 13 consumers show a successful updater run on v1.40.0; then tick #6211 (Q1: A) — verified on v1.40.2 (`5596e7520ef4`), the stable release current at check time
   - 2026-10-26 check (current stable v1.40.2 = `5596e7520ef4`): 12/13 synced, each with a `completed/success` updater run,
     `pin=5596e7520ef4` and no open `auto/update-workflows-*` PR: tele-funtoken-msg-scoring, digital_pa, fun-token-multi-chain,
     btc_sweeper, binance-blessings, mongo-explorer, multi-user-ai-agent, fbc_shutdown, bitsafe.io, hylifegroup.com,
     radateeree-resort.com, drhyg_ecommerce_automation. Pending: atlas-bridge.gd run 40254086692 still `queued`, pin `8164dcf9593d`.
   - done 2026-10-26: atlas-bridge.gd run 40254086692 `completed/success`, pin `5596e7520ef4`. 13/13 consumers synced to v1.40.2.

## Notes
- The local clone is shallow, so `git merge-base --is-ancestor` reported false negatives; containment was checked with the GitHub compare API instead.
- `main` is 13 commits ahead of `stable`. The latest `test-and-mark-stable.yml` run (40147488993, 2026-10-25T00:38Z) passed validation but skipped `release`. This does not affect the 50 PRs, which are all already in v1.40.0.
- Consumer repos are not attached to this cloud session (HTTP 403), so step 6 runs on the operator's machine.
- 2026-10-25: operator answered Q1: A. Once verified, tick the release/sync line in the latest #6211 `project-6902` comment and the
  release/sync line of the 50 `pr-*` entries (no other lines). No ticks made yet: step 6 is not verified.
- 2026-10-25: operator answered Q2: A (btc_sweeper, atlas-bridge.gd -> `WORKFLOW_PROFILE=core` + updater), Q3: A
  (mongo-explorer, fbc_shutdown -> `WORKFLOW_PROFILE=standard` + updater), Q4: A (lift tele-funtoken hold). The rendered
  wrapper was produced with `sed` and checked byte-identical to `scripts/workflow_wrapper_refs.py` output at `8164dcf9593d`.
- 2026-10-26 #6211 ticks (Q1: A), each checked byte-for-byte after the edit:
  - Ticked the release/sync line in the latest `project-6902` comment ("Publish and propagate an attested stable release").
  - Ticked the release/sync line in 35 `pr-*` comments: pr-6562, 6204, 6433, 6136, 6462, 6610, 6677, 6777, 6550, 6791,
    6564, 6569, 6935, 6951, 6938, 6959, 7073, 6655, 7098, 6674, 7105, 6728, 7147, 7157, 7156, 7176, 6855, 7195, 7204,
    7213, 7036, 7062, 7044, 7065, 7294.
  - Ticked `pr-6283` ("Release and sync the consumer rollout") in the #6211 issue body; the responder wrapper is now on
    btc_sweeper and atlas-bridge.gd, which the 2026-10-17 verification had flagged. The body edit also dropped two
    trailing newlines at the end of the body (no visible change).
  - Held (not ticked), because the step depends on something outside this release/sync verification:
    pr-7050 (also sets CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED per consumer), pr-6877 (also AUTO_RELEASE_STABLE_ENABLED,
    unreadable through the session proxy), pr-6208 / pr-6605 / pr-6709 / pr-6682 ("after the code fixes"),
    pr-6273 ("after the corrected code"), pr-7043 / pr-7201 ("after implementation merges"), pr-6509 ("hook fix"),
    pr-6649 ("staging fix"), pr-7134 and pr-6643 ("live-copy parity"), pr-7256 ("after the matching hook change merges").
