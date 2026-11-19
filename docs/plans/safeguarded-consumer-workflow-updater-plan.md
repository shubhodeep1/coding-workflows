# Safeguarded consumer workflow updater

## Summary

Make the consumer sync (`.github/workflows/update_workflows.yml`) safe enough for consumers on a security hold to turn it back on. Each stable release signs the list of files it ships. The updater checks that signature and each file's hash before running or copying anything, writes only to paths on the list, and delivers changes as a pull request that it merges after the checks pass, instead of pushing to the default branch. binance-blessings has kept its updater off since security finding #317, and this is what lets it switch back on.

## Context

- **The finding.** `shubhodeep1/binance-blessings#317` (high, A08:2021) found that the consumer updater commits workflows straight to the default branch from a moving upstream ref. Its recommendation was to verify a signed, immutable manifest and a path/hash allowlist, open an update PR, and auto-merge only after provenance and policy checks pass.
- **binance's current state.** It switched the updater off behind `vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED == 'true'` (unset, so off). Its `.github/workflows/ai-update-workflows.yml` lists three conditions before the variable may be set:
  1. a safeguarded upstream release exists;
  2. the `uses:` pin is moved to it;
  3. provenance and checks pass.
- **The unblock chain.** Issues #328 and #338 (and the fix-up chain #339–#367 behind them) were the unblock judge trying to do step 2. It could not: no safeguarded release exists, and the judge closed #328 at its round cap.
- **The upstream gap.** On both `main` (`32afacb62`) and `stable` (v1.31.0, `b728d0ac`):
  - `update_workflows.yml` ends its "Commit and push updates" step with `git push` (line 720 on `main`);
  - `workflow-templates/ai-update-workflows.yml` has no job gate and only `contents: write`. The first enabled run in binance would therefore replace binance's gated file with an ungated one.
- **Unverified code runs with write access.** The fetch step checks out `refs/tags/stable` (a moving tag) and then runs these upstream helpers with the consumer's `GH_PAT` and `contents: write`, unverified:
  - `scripts/workflow_wrapper_refs.py`
  - `scripts/apply_audit_gate_assets.py`
  - `scripts/assemble_changelog.py`
  - `scripts/assemble_agents.py`

  The Telegram step also downloads `scripts/tg_helpers.sh` from `ref=stable` through the contents API and sources it.
- **Release tags.** The release jobs create immutable version tags (`vX.Y.Z`) and move `stable` and the major tag:
  - `test-and-mark-stable.yml` job `release` (line 6298); the tag pushes are around 6583–6595 and `gh release create` is around 6636;
  - `mark-stable.yml` job `release` (line 636); `gh release create` is around 983.

  GitHub releases are not marked immutable (`immutable=false` on v1.29.15–v1.31.0).
- **Signing is free.** `shubhodeep1/coding-workflows` is public, so GitHub artifact attestations (Sigstore-backed) cost nothing, and anyone with the public repo name can verify them.
- **Review cost.** A sync PR would go through the consumer's `ai-review.yml` → `review_autofix.yml` like any PR. Because `.github/workflows/` is a protected path, every one would get the full reviewer panel.

Decisions taken in clarification (Q46–Q53, all A) are cited inline as "(Qnn)".

## Goals

- A stable release cannot move `stable` unless a `sync-manifest.json` exists and is attested by that release workflow. The release asset is uploaded right after `gh release create`, behind the same fail-closed guard (Q46, Q51).
- Before the updater executes any upstream script or copies any upstream file, it confirms all of the following, and otherwise changes nothing and fails with a named reason (Q51):
  - an attestation for the manifest exists, signed by `shubhodeep1/coding-workflows`'s own release workflows on `refs/heads/stable`;
  - the manifest's `commit` equals the fetched stable SHA;
  - every file in the sparse checkout matches the manifest's sha256, with nothing missing and nothing extra.
- The updater stages only consumer paths on the signed `allowed_consumer_paths` list. Any other staged path fails the run before commit (Q51).
- By default, updates land as a PR from branch `ai-sync/stable`, merged by GitHub auto-merge or directly by the updater (Q47). `update_via_pull_request: false` restores the direct push (Q48).
- `workflow-templates/ai-update-workflows.yml` carries the job gate `vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false'` and `pull-requests: write`. Every later sync keeps the gate in place (Q49).
- The consumer's LLM review and autofix skip `ai-sync/` PRs whose changed paths are all sync-managed. The review pipeline never merges those PRs (Q50).
- binance-blessings is re-pinned to the first release with both phases, its variable is set to `true`, and #317 is closed (Q53).

## Non-goals

- Turning on GitHub's repository-level "immutable releases" setting, or adding branch protection to consumers. Both are admin settings (§23.C) and remain separate operator decisions.
- Signing anything other than what the consumer sync copies or executes. Sandbox images, release notes and similar are out of scope.
- Changing what the sync copies (templates, `.claude/`, `CLAUDE.md`, audit-gate assets, retired files, changelog and agents assembly). Only verification and delivery change.
- Re-pinning or flipping variables in the other 12 consumers. They keep syncing with no action (Q49).
- Restoring drhyg's local cloud-permission policy (dropped in `drhyg_ecommerce_automation#71`).

## Constraints

- **§6 naming immutability.**
  - Existing step names and ids stay unchanged: "Commit and push updates", `steps.update`, `steps.claude_sync`, and the rest.
  - The input `allow_workflow_edits` and the variables `ALLOW_WORKFLOW_EDITS`, `WORKFLOW_PROFILE` and `ALERT_MSG_LEVEL` keep their meaning.
  - `AI_WORKFLOW_AUTO_UPDATE_ENABLED` is an existing consumer variable name (binance). The template adopts it rather than inventing a new one. Its template default differs from binance's hand-written file (unset means on, not off), and the plan documents that explicitly (Q49).
  - New names: input `update_via_pull_request`, release asset `sync-manifest.json`, branch `ai-sync/stable`, scripts `scripts/build_sync_manifest.py` and `scripts/review_workflow_sync_skip.sh`, log prefixes `SYNC_MANIFEST` and `AUTOFIX_GATE_WORKFLOW_SYNC_SKIP`. Each was grepped in `.github/`, `scripts/`, `workflow-templates/` and `tests/` before use, with no clash; the implementer re-greps before adding them.
- **§14 consumer registry.** The template and updater changes reach all 13 consumers in `.github/ai/consumer_repos.json` on their next sync after the release. No consumer is added.
- **§15 GitHub API hygiene.**
  - Release side: 1 attestation write and 1 asset upload, both through existing steps' credentials.
  - Updater side, per run:
    - 1 release-asset download;
    - 1 attestation lookup (`gh attestation verify`);
    - when there are changes, 1 `GET /repos/{repo}` for allowed merge methods, 1 `GET .../pulls?head=…&state=open`, 0–1 `POST .../pulls`, and 1 auto-merge enable or 1 merge.

  No call sits inside a per-file loop.
- **§18 automation.** No manual script. Everything runs inside the existing release jobs and the existing updater job (see the section below).
- **§20 / §30.** One changelog fragment and one `agents.d/` fragment per phase PR. `agents.md` is never edited directly.
- **§27 workflow size.**
  - `test-and-mark-stable.yml` is 397,779 bytes and `review_autofix.yml` is 441,223 bytes, so new logic goes into `scripts/` with thin workflow steps. `review_autofix.yml` must stay under 480,000 bytes; the expected growth is under 1 KB.
  - `update_workflows.yml` (48,024 bytes) keeps its verifier inline (see Approach).
- **Security ordering.** The verifier must not come from the tree it verifies, so it lives in `update_workflows.yml` itself. A consumer calls that file at a pinned SHA, so the verifier is pinned with it.

## Automation wiring (§18.E)

- **Scripts.**
  - New `scripts/build_sync_manifest.py`, invoked only from the `release` jobs of `test-and-mark-stable.yml` and `mark-stable.yml`.
  - New `scripts/review_workflow_sync_skip.sh`, sourced from the "Evaluate review gate" step of `review_autofix.yml`.

  Everything else extends existing steps of `update_workflows.yml`. Neither new script is single-use or long-running, so neither needs a `docs/scripts-pending-removal.md` entry (§18.F).
- **Entry points.**
  - Release: the `promote-main-to-stable.yml` daily cron (00:00 UTC) → `test-and-mark-stable.yml` job `release`, plus manual `mark-stable.yml` job `release`.
  - Consumer: `ai-update-workflows.yml` (daily 04:00 UTC cron, `repository_dispatch` `coding-workflows-stable-released`, `workflow_dispatch`) → `update_workflows.yml` job `update-wrappers`.
  - Review: `ai-review.yml` → `review_autofix.yml` "Evaluate review gate".
- **Supervisor.** None needed (§18.C).
- **DB work.** None (§10 and §18.D don't apply).

## Approach

**Manifest (Q46).** `scripts/build_sync_manifest.py --repo-root . --commit <sha> --version <vX.Y.Z> --out sync-manifest.json` writes canonical JSON (sorted keys, `\n` line ends, UTF-8):

```json
{
  "schema": "coding-workflows.sync-manifest.v1",
  "repository": "shubhodeep1/coding-workflows",
  "commit": "<40-hex release commit>",
  "version": "vX.Y.Z",
  "files": { "<repo-relative path>": "<sha256>", "...": "..." },
  "allowed_consumer_paths": ["<exact path or glob>", "..."]
}
```

- **`files`** covers every regular file the updater's sparse checkout contains: everything under `workflow-templates/` and `scripts/`. Symlinks are recorded as `symlink:<target>`. `workflow-templates/CLAUDE.md` is a symlink to `../CLAUDE.md`, so root `CLAUDE.md` is also hashed.
- **`allowed_consumer_paths`** is derived at release time:
  - `.github/workflows/<name>` for each `workflow-templates/*.yml`;
  - `.claude/<rel>` for each file under `workflow-templates/.claude/`;
  - `CLAUDE.md`;
  - each `target` in `workflow-templates/audit-gate/contract.json`, plus `package.json`;
  - each path in `workflow-templates/retired_files.txt` (deletions);
  - `changelog.d/.gitkeep`, `changelog.d/*.md`, `CHANGELOG.md`;
  - `agents.md`, `AGENTS.md`, `agents.d/*.md`;
  - `scripts/.gitignore`, which the Telegram step writes today.

  The implementer confirms this list against every `git add` in the "Commit and push updates" step, and a test enforces that.

**Signing (Q46, Q51).** In each `release` job:
1. Build the manifest for the intended release commit and attest it with `actions/attest-build-provenance` (`subject-path: sync-manifest.json`). The job gains `id-token: write` and `attestations: write`.
2. Fail the job before any tag push if either step fails.
3. After `gh release create`, upload `sync-manifest.json` as a release asset with `gh_retry`.

If the upload fails after `stable` has moved, the job fails and alerts. Consumers then fail closed on their next run (no unverified change) until a re-run uploads the asset. The tag-push order is not changed.

**Verification (Q51), inline in `update_workflows.yml`, new step "Verify signed sync manifest" right after "Fetch latest workflow templates from coding-workflows@stable":**
1. Find the `vX.Y.Z` tag that points at `steps.fetch.outputs.upstream_sha`, using `git ls-remote --tags` on the existing upstream clone and peeled refs. No tag found fails with `ERR_SYNC_MANIFEST_NO_VERSION_TAG`.
2. Download `sync-manifest.json` from that release (`gh release download`). Missing fails with `ERR_SYNC_MANIFEST_MISSING`.
3. Run `gh attestation verify sync-manifest.json --repo shubhodeep1/coding-workflows --cert-identity-regex '^https://github\.com/shubhodeep1/coding-workflows/\.github/workflows/(test-and-mark-stable|mark-stable)\.yml@refs/heads/stable$' --deny-self-hosted-runners`. Failure gives `ERR_SYNC_MANIFEST_SIGNATURE`.
4. Check the manifest: `schema`, `repository` and `commit` must equal the fetched SHA (`ERR_SYNC_MANIFEST_COMMIT_MISMATCH`). Every file in the sparse checkout must be listed with a matching hash, and every listed file must be present (`ERR_SYNC_MANIFEST_FILE_MISMATCH path=<p>`).
5. Export `allowed_consumer_paths` to a file for the commit step.

All later steps run only when this step succeeds. The Telegram step sources `scripts/tg_helpers.sh` from the verified sparse checkout instead of downloading it from `ref=stable` again. Steps 1 and 4 are inline Python in a heredoc; steps 2 and 3 use the runner's `gh`.

**Delivery (Q47, Q48).** New `workflow_call` input `update_via_pull_request` (boolean, default `true`; also on `workflow_dispatch`). In "Commit and push updates":
1. **Policy check before commit.** `git diff --cached --name-only --no-renames` must be a subset of `allowed_consumer_paths` (glob match). Otherwise fail with `ERR_SYNC_PATH_NOT_ALLOWED path=<p>`.
2. **Old path.** `update_via_pull_request == false` keeps today's `git push`, unchanged.
3. **PR path.**
   1. Commit as today, then `git push --force origin HEAD:refs/heads/ai-sync/stable`. The branch belongs to the updater.
   2. Find an open PR with head `ai-sync/stable` (one REST call). Create it if missing, with title `chore: sync AI workflow wrappers from coding-workflows <vX.Y.Z>`, a body containing the existing commit body and a marker `<!-- ai-sync:v1 version=<vX.Y.Z> commit=<sha> -->`, and base the repository's default branch.
   3. Choose a merge method from `GET /repos/{repo}` in the order squash, merge, rebase, taking the first one allowed.
   4. Run `gh pr merge --auto --<method>`. If GitHub refuses because the PR is already in clean status (no required checks), run `gh pr merge --<method>`.
   5. If merging is refused for any other reason (for example a required review), leave the PR open. Set output `sync_pr_state=awaiting_merge` and include the PR URL in the Telegram message.

   A rerun with the PR still open force-updates the branch and reuses the PR. That keeps it idempotent: one sync PR per consumer at a time.

**Template (Q49).** `workflow-templates/ai-update-workflows.yml` gains `pull-requests: write` in `permissions`. Its `update` job gains `if: ${{ vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false' }}`, and the header comment documents both variables and the PR delivery. Unset means on, so the other consumers keep syncing. Setting `'false'` stops the job entirely; `ALLOW_WORKFLOW_EDITS=false` keeps its current meaning (the job runs and skips edits).

**Review skip (Q50).** `scripts/review_workflow_sync_skip.sh` exposes `review_workflow_sync_skip_applies <head_ref> <changed_paths_file>`. It returns true only when:
- the head ref starts with `ai-sync/`;
- every changed path matches the sync-managed classes above.

The review gate already lists changed files, and the helper reuses that list. "Evaluate review gate" then sets `SHOULD_RUN="false"`, logs `AUTOFIX_GATE_WORKFLOW_SYNC_SKIP pr=<n> head_ref=<ref> outcome=skipped`, and makes sure the deterministic-skip-merge path does not run. The review pipeline never merges, labels or comments on these PRs, so a hand-made `ai-sync/` branch gains no merge from review. Anything else falls through to normal review.

**Alternatives rejected.** cosign by hand (Q46 B), an SSH key secret (Q46 C), human merges (Q47 B), opt-in PR mode (Q48 B), a default-off template gate (Q49 B), reviewing sync PRs (Q50 B), warn-only verification (Q51 B).

## Phases & Merge Strategy

Two phases, one PR each, mergeable in either order (Q52 A). Each phase ships to consumers only through the next stable release.

1. **Phase 1: signed manifest and fail-closed verification.**
   - **Scope:** the manifest builder and signing in both release jobs, plus the verifier and the path policy in the updater. Delivery stays `git push`.
   - **Files:** see Implementation Steps 1.1–1.7.
   - **Why signing and verification ship together:** the first release containing the verifier is produced by a release job that already signs. A consumer pinned to that release then always finds a signed stable.
   - **Done:** CI green; and on a dry run of the release workflow (`dry_run`/`gate_only`), the manifest is built and validated and the attestation step is skipped. A real release yields `sync-manifest.json` plus an attestation, and a consumer sync against it reports `SYNC_MANIFEST outcome=verified`.
   - **Independent of Phase 2:** delivery unchanged; template unchanged.
   - **Rollback:** revert the PR. Release jobs stop signing, and the updater stops verifying.
2. **Phase 2: PR delivery, template gate, review skip.**
   - **Scope:** the `update_via_pull_request` input and PR path, the template's gate and permission, and the review-gate skip.
   - **Files:** see Implementation Steps 2.1–2.6.
   - **Done:** CI green; with `update_via_pull_request: true`, a sync opens or reuses `ai-sync/stable` and merges per Q47; the review gate skips such a PR.
   - **Independent of Phase 1:** the PR path works on unverified input exactly as the push path does today. Without Phase 1 there is no path policy check before commit; Phase 1 adds it, whichever phase merges first.
   - **Rollback:** revert the PR, or have a consumer set `update_via_pull_request: false` / `AI_WORKFLOW_AUTO_UPDATE_ENABLED=false`.

Both phases edit the "Commit and push updates" step. Phase 1 adds the policy check at the top of the step, and Phase 2 replaces the trailing `git push`. Whichever merges second resolves a small textual conflict through the normal conflict path. Neither depends on the other's lines.

## Implementation Steps

### Phase 1

1.1 **New** `scripts/build_sync_manifest.py`.
- CLI as in Approach.
- Exit 2 on bad arguments and 1 on any unreadable file. Log prefix `SYNC_MANIFEST` (`outcome=built files=<n> allowed=<n>`).
- Python standard library only; tabs; opening braces per the §9 style used in other scripts.

1.2 `.github/workflows/test-and-mark-stable.yml`, job `release`.
- Permissions: add `id-token: write` and `attestations: write`.
- Before the tag-push block (~6583), add steps "Build sync manifest" (runs 1.1 for the intended release commit and `${VERSION}`) and "Attest sync manifest" (`actions/attest-build-provenance@v2`, `subject-path: sync-manifest.json`).
- Pass every `${{ }}` value through `env:` (§27).

1.3 Same file, after `gh_retry gh release create` (~6636): add "Upload sync manifest" (`gh_retry gh release upload "${VERSION}" sync-manifest.json --clobber`). On failure, `::error::` plus the existing alert path.

1.4 `.github/workflows/mark-stable.yml`, job `release` (636; release create ~983): the same three steps and the same permissions.

1.5 `.github/workflows/update_workflows.yml`: new step "Verify signed sync manifest" (`id: verify_manifest`) between `fetch` and `render_wrappers`, as in Approach steps 1–5. Every later step gets `if: steps.verify_manifest.outcome == 'success'`, or relies on the default `success()`; the implementer confirms none uses `always()`.

1.6 Same file.
- In "Commit and push updates", before `git commit`: add the path policy check against `/tmp/sync_allowed_consumer_paths.txt` (`ERR_SYNC_PATH_NOT_ALLOWED`).
- In "Send Telegram notification": source `scripts/tg_helpers.sh` from the verified sparse checkout (`$(dirname "${{ steps.fetch.outputs.upstream_dir }}")/scripts/tg_helpers.sh`) instead of the contents-API download, keeping the existing fallback to raw `curl`.
- Surface `failure_reason_code` values for the new errors in the existing Summary step.

1.7 Fragments: `changelog.d/<pr>-signed-sync-manifest.md` (`security`), and `agents.d/<pr>-signed-sync-manifest.md` adding a "Consumer sync verification" note and the `SYNC_MANIFEST` log prefix to `## Stable log prefixes (contractual)`.

### Phase 2

2.1 `.github/workflows/update_workflows.yml`: add input `update_via_pull_request` (boolean, default `true`) to `workflow_call` and `workflow_dispatch`.

2.2 Same file, "Commit and push updates" (keep the name and the `if:`): implement the PR path from Approach Delivery steps 2–5. Add outputs `sync_pr_url` and `sync_pr_state` (`merged|auto_merge_enabled|awaiting_merge|pushed`). The existing step gets `id: commit_push`; there is none today, so this is additive.

2.3 Same file, "Send Telegram notification" and "Summary": include `sync_pr_state` and `sync_pr_url`.

2.4 `workflow-templates/ai-update-workflows.yml`:
- add `pull-requests: write`;
- add the `if:` gate from Approach Template;
- rewrite the header comment to describe PR delivery, `AI_WORKFLOW_AUTO_UPDATE_ENABLED` (unset or anything but `'false'` means on) and `ALLOW_WORKFLOW_EDITS`.

2.5 **New** `scripts/review_workflow_sync_skip.sh`, sourced in `.github/workflows/review_autofix.yml` "Evaluate review gate" next to the other skip checks (~840–930).
- On a match: set `SHOULD_RUN="false"` and log `AUTOFIX_GATE_WORKFLOW_SYNC_SKIP`, keeping the deterministic-skip-merge output false.
- Register the script wherever the gate's other sourced helpers are registered (`REQUIRED_BOOTSTRAP_SCRIPTS` / the support-checkout path list), following the §27 pattern in `agents.md`.

2.6 Fragments: `changelog.d/<pr>-sync-via-pull-request.md` (`changed`), and `agents.d/<pr>-sync-via-pull-request.md` (input, variables, branch name, review skip, log prefix).

## Files & Modules

- `scripts/build_sync_manifest.py` [new] (Phase 1)
- `.github/workflows/test-and-mark-stable.yml` (Phase 1)
- `.github/workflows/mark-stable.yml` (Phase 1)
- `.github/workflows/update_workflows.yml` (Phases 1 and 2)
- `workflow-templates/ai-update-workflows.yml` (Phase 2)
- `scripts/review_workflow_sync_skip.sh` [new] (Phase 2)
- `.github/workflows/review_autofix.yml` (Phase 2)
- `tests/test_build_sync_manifest.py` [new] (Phase 1)
- `tests/test_update_workflows_manifest_verification.py` [new] (Phase 1)
- `tests/test_update_workflows_pull_request_delivery.py` [new] (Phase 2)
- `tests/test_review_workflow_sync_skip.py` [new] (Phase 2)
- `tests/test_update_workflows_guardrails.py` (both phases, extended)
- `.github/workflows/ci.yml` (both phases: a step per new test file, following existing per-file steps)
- `changelog.d/*`, `agents.d/*` [new] (one each per phase)

## Tests

**Unit, `tests/test_build_sync_manifest.py`:**
- canonical byte-stable output across two runs;
- symlink recording;
- `allowed_consumer_paths` covers every `git add` target in "Commit and push updates": the test parses the step body, so a new sync category that forgets the allowlist fails CI;
- bad arguments exit 2.

**Workflow contract, `tests/test_update_workflows_manifest_verification.py`:**
- the verify step sits before `render_wrappers`;
- the identity regex names only the two release workflows on `refs/heads/stable`;
- `--deny-self-hosted-runners` is present;
- no later step uses `always()`;
- the Telegram step no longer fetches `tg_helpers.sh` through the contents API;
- the inline verifier, extracted and run against fixtures, rejects:
  - a missing file;
  - an extra file;
  - a hash mismatch;
  - a commit mismatch;
  - a staged path off the allowlist.

**Release contract (extend the existing release-workflow tests):**
- both `release` jobs have `id-token: write` and `attestations: write`;
- "Attest sync manifest" runs before the first `publish_tag_with_remote_verification`;
- upload follows `gh release create`.

**Phase 2:**
- `tests/test_update_workflows_pull_request_delivery.py`: the input defaults to `true`; the `false` path is byte-identical to today's `git push`; the PR path uses branch `ai-sync/stable`, makes one lookup call and at most one create, and falls back to a direct merge on clean status; the template has the gate and `pull-requests: write`.
- `tests/test_review_workflow_sync_skip.py`: an `ai-sync/` head with managed paths skips; an `ai-sync/` head with any other path reviews; a non-`ai-sync/` head reviews; the skip never sets the deterministic-skip-merge output.
- `tests/test_workflow_file_size_limit.py` still passes.

**End to end (after release, part of Rollout):**
1. The first real release shows the asset and `gh attestation verify` passing.
2. drhyg (on the default pin) runs its next scheduled sync and logs `SYNC_MANIFEST outcome=verified`. After Phase 2, it opens and merges an `ai-sync/stable` PR.
3. binance is verified as in Rollout.

## Risks & Mitigations

- **Upload fails after `stable` moves.** Consumers fail closed (no change) until the asset exists. Mitigation: `gh_retry` on upload, the job fails loudly, and re-running the release job's upload is idempotent (`--clobber`).
- **A consumer still on the old pin runs the old updater once more.** Expected: the pinned updater is what runs. Its run renders the new pin, and the next run verifies. ACCEPTED: one last unverified sync, the same as today.
- **Every consumer's syncs stop if signing silently breaks.** Mitigation: the release job fails before tagging if signing fails (Q51), and the updater's Telegram alert names `ERR_SYNC_MANIFEST_*`.
- **Auto-merge in an unprotected consumer merges as soon as the PR is created** (Q47). ACCEPTED: provenance and path policy have already passed inside the job. Repos that want a gate add required checks or reviews, which make the PR wait (`awaiting_merge`).
- **Someone pushes a hand-made `ai-sync/` branch.** Review skips it but never merges it, so it needs a human merge exactly as today. ACCEPTED.
- **The template default (unset means on) differs from binance's hand-written default (unset means off)** (Q49). ACCEPTED: binance's variable is `true` once it re-pins. Documented in the template header and the agents fragment.
- **`gh attestation verify` flags differ between `gh` versions on runner images.** Mitigation: the implementation runs `gh attestation verify --help` in the step and fails closed with `ERR_SYNC_MANIFEST_SIGNATURE` if a required flag is missing, and the contract test pins the arguments.
- **The release tags are not GitHub-immutable**, so a tag could be moved. ACCEPTED: the attestation binds the manifest to the commit, and a moved tag at a different commit fails `ERR_SYNC_MANIFEST_COMMIT_MISMATCH`.

## Rollout

1. Merge Phase 1 and Phase 2 (either order). Each reaches consumers only through the next automatic stable release (`promote-main-to-stable.yml`, 00:00 UTC). No manual dispatch.
2. After the first release containing both phases, check the release has `sync-manifest.json` and that `gh attestation verify` passes. Then confirm one consumer (drhyg) synced through a merged `ai-sync/stable` PR with `SYNC_MANIFEST outcome=verified`.
3. **binance (Q53).** An interactive session, with the user's approval at that time (§23.C), does three things:
   1. edits `binance-blessings/.github/workflows/ai-update-workflows.yml` to pin `uses:` to that release commit, keeping the gate and both permissions;
   2. sets binance's repository variable `AI_WORKFLOW_AUTO_UPDATE_ENABLED=true`;
   3. confirms the next sync opens and merges a verified `ai-sync/stable` PR, then closes `binance-blessings#317` with a link to that run.

   The pipeline's grant guard blocks automated edits to this workflow path, which is why a person does this.
4. **Rollback.**
   - Per consumer: `update_via_pull_request: false` or `AI_WORKFLOW_AUTO_UPDATE_ENABLED=false`.
   - Upstream: revert the phase PR, and the next release carries the revert.

## References

- `shubhodeep1/binance-blessings#317` (finding), `#328`, `#338` (operator step), `#339`–`#367` (fix-up chain)
- `shubhodeep1/coding-workflows#7038` (unblock fix-up chain guard)
- `.github/workflows/update_workflows.yml`, `workflow-templates/ai-update-workflows.yml`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/mark-stable.yml`, `.github/workflows/review_autofix.yml`
- GitHub artifact attestations: `actions/attest-build-provenance`, `gh attestation verify`
- CLAUDE.md §6, §14, §15, §18, §20, §27, §30
