# Safeguarded consumer workflow updater: plan

## Summary

Consumer repositories receive every `@stable` release through
`ai-update-workflows.yml`, which calls this repository's reusable
`.github/workflows/update_workflows.yml`. Today that updater fetches the
`stable` tag, overwrites the consumer's wrappers, `.claude/` assets and
`CLAUDE.md`, and pushes the result straight to the consumer's default branch
with `contents: write`. An upstream compromise, or a mistaken release, would
install privileged workflows on 13 default branches in one dispatch. The
security audit in `shubhodeep1/binance-blessings` (its issue #317,
`mutable-workflow-auto-update-default-branch`, high, confidence 10/10) put
that consumer on a hold: its updater runs only when the repository variable
`AI_WORKFLOW_AUTO_UPDATE_ENABLED` is literally `true`, and that may be set
only after (1) upstream publishes a safeguarded release that verifies a
signed immutable manifest against a trusted identity, enforces an allowed
path and hash list, and submits updates as a pull request instead of pushing,
(2) the consumer's `uses:` pin moves to that release, and (3) provenance,
policy and required checks pass. v1.31.0 meets none of (1), so that consumer
has been frozen on v1.30.0 since the hold and every stable release since then
has not reached it (operator check, 2026-10-09).

This plan builds the safeguarded release and updater upstream, so every
consumer gets the same protection and the frozen consumer can be re-enabled
without weakening its hold. After it ships, a release publishes a signed
manifest of exactly the files the updater may write, the updater refuses any
file that is not in the manifest or whose hash differs, writes the update to
a branch and opens a pull request, and merges that pull request only after
the consumer's own checks and an independent re-verification job pass. The
operator decisions below (D1 to D6) were taken on 2026-10-09 (Q33: A).

## Automation wiring (§18.E)

| Question | Answer |
|---|---|
| New script / extended script / code-only? | Two new scripts, both run only from workflows: `scripts/release_manifest.py` (`build` at release time, `verify` in the updater and in the consumer-side verification job) and `scripts/update_workflows_pr.sh` (the branch, pull-request and merge-policy step of the updater). Existing `update_workflows.yml`, `test-and-mark-stable.yml`, `mark-stable.yml` and the `workflow-templates/ai-update-workflows.yml` template are extended. No script is run by hand. |
| Scheduler / PR-push entry points | Release: `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml` (`release` job) build, attest and upload the manifest. Consumer sync: `workflow-templates/ai-update-workflows.yml` (daily 04:00 UTC cron, `repository_dispatch` on `coding-workflows-stable-released`, `workflow_dispatch`) → `.github/workflows/update_workflows.yml`. Consumer verification: a new `verify-update` job inside `update_workflows.yml` triggered on the update pull request's `pull_request` events through the same wrapper. |
| New long-running supervisor (§18.C)? | No. The release and sync crons already exist; nothing waits between events. |
| DB work (§18.D)? | None (§10 N/A). |
| `docs/scripts-pending-removal.md` (§18.F) | No entries. Both new scripts are permanent parts of the release and sync path, not single-use. |

## Context

- `.github/workflows/update_workflows.yml` resolves `refs/tags/stable` to a
  commit, sparse-checks-out `workflow-templates/` and `scripts/`, renders the
  wrappers, syncs `.claude/`, retired-file removals, `CLAUDE.md` and changelog
  fragments, then runs `git push` on the consumer's default branch (step
  "Commit and push updates"). The `repository_dispatch` payload's `sha` is
  treated as a hint only; the freshly resolved tag always wins.
- The release jobs (`test-and-mark-stable.yml` from line 6595, `mark-stable.yml`)
  create a GitHub Release from the changelog and dispatch
  `coding-workflows-stable-released` with `version` and `sha` to every repo in
  `.github/ai/consumer_repos.json` (13 repositories). No manifest, checksum or
  attestation is published today.
- `workflow-templates/ai-update-workflows.yml` (the consumer-side wrapper)
  grants `contents: write` and calls the reusable updater `@stable`. The
  binance-blessings copy diverges by hand: it adds the
  `AI_WORKFLOW_AUTO_UPDATE_ENABLED == 'true'` job gate, `pull-requests: write`,
  and a `uses:` pin on the v1.30.0 commit (`2a063d45…`). Its header documents the
  three conditions above and warns that the first enabled sync would remove the
  gate because the template lacks it.
- `scripts/sync_claude_live_copies.py` and the `.claude/` sync already carry a
  notion of security paths (`hooks/`, `settings.json`); the manifest generalises
  that to every file the updater writes.
- GitHub artifact attestations (`actions/attest-build-provenance`, verified with
  `gh attestation verify --repo <owner>/<repo>`) give a Sigstore-signed statement
  bound to this repository's workflow identity without managing keys. That is
  the "signed immutable manifest against a trusted identity" the hold asks for.

## Decisions (operator answers, 2026-10-09)

### D1 — Trust root: GitHub artifact attestations, no private keys

The manifest is attested with `actions/attest-build-provenance` in the release
job and verified with `gh attestation verify --repo shubhodeep1/coding-workflows
--signer-workflow shubhodeep1/coding-workflows/.github/workflows/test-and-mark-stable.yml`
(and the `mark-stable.yml` signer). No cosign keys, no secrets to rotate. The
verification fails closed: an unverifiable or missing attestation means no
update is written and a WARNING alert names the reason.

### D2 — Manifest contents

`scripts/release_manifest.py build` writes `release-manifest.json` with: the
release version and commit, and for every file the updater may write (every
rendered template under `workflow-templates/`, every synced `.claude/` asset,
`CLAUDE.md`, the changelog assets, and the retired-file list) its repo-relative
consumer path and sha256. The manifest is uploaded as a release asset and
attested. The updater computes the same set from the fetched tag and refuses
to write a path that is absent from the manifest or whose hash differs
(`ERR_MANIFEST_PATH_UNLISTED`, `ERR_MANIFEST_HASH_MISMATCH`).

### D3 — Pull request, not push

The updater commits to `ai/update-workflows/<version>` and opens (or updates)
one pull request per release against the consumer's default branch. It never
pushes to the default branch again. The pull request body lists the version,
the manifest digest, the attestation verification result and the changed
paths. The consumer wrapper gains `pull-requests: write`.

### D4 — Merge policy

The update pull request merges automatically only after both pass: the
consumer's required checks, and a new `verify-update` job (same reusable
workflow, run on the pull request) that re-reads the manifest from the
release, re-verifies the attestation, and checks that the pull request's tree
matches the manifest exactly. When the consumer repository has auto-merge
enabled, the updater uses `gh pr merge --squash --auto`; when it does not,
the `verify-update` job merges with `gh pr merge --squash` once it has
observed every other check green, and otherwise leaves the pull request open
and sends one WARNING naming the failing check. A consumer can opt out of
automatic merging with the repository variable
`AI_WORKFLOW_UPDATE_AUTOMERGE=false` (default: merge).

### D5 — Gate semantics that keep every current consumer running

The template carries the `AI_WORKFLOW_AUTO_UPDATE_ENABLED` gate so a sync can
never remove it: the job runs when the variable is `true`, or when it is unset
and the repository has not set `AI_WORKFLOW_UPDATE_REQUIRE_OPT_IN=true`;
`false` always disables it. The 12 consumers that auto-update today keep doing
so (variable unset, opt-in not required). binance-blessings keeps its explicit
`true` requirement by setting `AI_WORKFLOW_UPDATE_REQUIRE_OPT_IN=true` once, and
is enabled by the operator after the pin swap (D6). `ALLOW_WORKFLOW_EDITS=false`
keeps its meaning as the hard off switch.

### D6 — Re-enabling the frozen consumer is a separate operator step

After the release that carries this plan is published, the operator (in an
interactive session, CLAUDE.md §23.C) opens one pull request in
binance-blessings that moves its `uses:` pin to that release's commit and
replaces its hand-edited wrapper with the new template, then sets the two
repository variables. That step is outside this plan's code work and is not
automated here, because it is the hold's own human decision.

## Goals

- G1: a release publishes an attested manifest of every file the updater may
  write, and the updater verifies it before touching the consumer.
- G2: the updater never pushes to a consumer default branch; every update is a
  pull request.
- G3: an update pull request merges only after the consumer's checks and an
  independent re-verification pass; failure leaves the pull request open with
  one alert.
- G4: the consumer wrapper template carries the opt-in gate, so a sync cannot
  remove a consumer's hold.
- G5: no current consumer loses automatic updates when the new template lands.
- G6: everything runs from the existing release and sync workflows; nothing is
  run by hand.

## Non-goals

- Changing what the updater syncs (the file set stays as it is today).
- Signing anything other than the release manifest.
- Replacing GitHub's attestation service with a self-managed key.
- Re-enabling binance-blessings inside this project (D6).

## Constraints

- CLAUDE.md §6: `update_workflows.yml` inputs, step names, `ALLOW_WORKFLOW_EDITS`,
  the `coding-workflows-stable-released` event and its payload keys, and the
  `ERR_*` reason tokens are unchanged; new tokens are added beside them.
- CLAUDE.md §15: the updater adds at most four API calls per run (release asset
  download, attestation verify, pull-request create or update, merge); the
  verify job adds two.
- CLAUDE.md §20: one changelog fragment per phase pull request.
- CLAUDE.md §27: `test-and-mark-stable.yml` is close to the size guard; the
  manifest build step must be a script call, not an inline body.
- The reusable updater must keep working for a consumer whose wrapper is the
  old template (no `pull-requests: write`): in that case it fails closed with
  `ERR_UPDATE_PR_PERMISSION_MISSING` and an alert that names the wrapper
  upgrade, instead of falling back to a push.
- Private consumer repositories need the `GH_PAT` the updater already holds
  for `gh attestation verify`; the public-repository fallback must not be used
  silently.

## Phases & Merge Strategy

Each phase is one independently mergeable, production-safe pull request
against `main`. Earlier phases ship nothing user-visible until the later ones
turn it on, so a half-landed project leaves today's behaviour intact.

| Phase | Scope | Safe alone because |
|---|---|---|
| 1 | Manifest build and attestation at release | Adds an asset to releases; the updater ignores it until phase 2. |
| 2 | Updater verification, fail-closed, behind `UPDATE_MANIFEST_VERIFY_ENABLED` (default `false` in this phase) | Off by default; turned on in phase 3 once the first attested release exists. |
| 3 | Pull-request delivery, merge policy, verify job, template gate; flips `UPDATE_MANIFEST_VERIFY_ENABLED` default to `true` | Lands as one release so consumers never see a verified-but-pushed or unverified-PR state. |

## Implementation Steps

### Phase 1 — Attested release manifest

1. Add `scripts/release_manifest.py` with `build --tag <version> --out
   release-manifest.json` (enumerates the updater's write set from the tagged
   tree, hashes with sha256, writes JSON) and `verify --manifest <file>
   --tree <dir>` (reports unlisted paths and hash mismatches, exit 1 on any).
2. In the `release` job of `test-and-mark-stable.yml` and `mark-stable.yml`:
   build the manifest, attach it to the GitHub Release with `gh release upload`,
   and attest it with `actions/attest-build-provenance` (`id-token: write`,
   `attestations: write` on that job only).
3. Tests: `tests/test_release_manifest.py` (build set, determinism, hash
   mismatch, unlisted path) and workflow contract assertions for both release
   workflows (`tests/test_release_workflows_manifest_contract.py`).
4. README "Release" section and `agents.md`: what the manifest contains and
   where it lives.

### Phase 2 — Fail-closed verification in the updater

5. In `update_workflows.yml`, after "Fetch latest workflow templates": download
   `release-manifest.json` for the resolved release, run `gh attestation verify`
   with the signer workflows from D1, then `release_manifest.py verify` against
   the rendered tree. Any failure sets `ERR_MANIFEST_*` and stops before the
   write steps; the Telegram summary names the token.
6. Gate the step with input `manifest_verify_enabled` (default `false`) and
   repository variable `UPDATE_MANIFEST_VERIFY_ENABLED` so phase 2 can merge
   before any attested release exists.
7. Tests: updater contract test for the new step order and the fail-closed
   branches; an end-to-end dry run in `tests/` using a fixture manifest.

### Phase 3 — Pull-request delivery and merge policy

8. Replace the `git push` in "Commit and push updates" with
   `scripts/update_workflows_pr.sh`: branch `ai/update-workflows/<version>`,
   commit, push the branch, create or update the pull request, apply D4.
9. Add the `verify-update` job to `update_workflows.yml` (runs when the
   calling wrapper's event is `pull_request` on an update branch): re-verify
   per step 5 against the pull request tree, then merge per D4 or alert.
10. Update `workflow-templates/ai-update-workflows.yml`: add `pull_request`
    trigger for `ai/update-workflows/**`, `pull-requests: write`, and the D5
    gate; keep the header's opt-out text and document both new variables.
11. Flip the phase 2 default to `true`; make a push to a consumer default
    branch impossible from this workflow (no step runs `git push` without a
    branch argument; a contract test asserts it).
12. Tests: `tests/test_update_workflows_pr.py` (branch naming, idempotent
    re-run on the same version, auto-merge versus direct merge, opt-out),
    template contract test for the gate and permissions, and an update of
    `tests/test_update_workflows_contract.py` (or the nearest existing updater
    test) for the removed push.
13. README consumer section and `agents.md`: the pull-request flow, the two
    new variables, the alert texts, and the D6 operator step for a consumer on
    an explicit hold.

## Tests

- Unit: manifest build is deterministic for the same tree; a changed byte
  changes exactly one hash; unlisted path and mismatch are reported with
  their paths.
- Contract: both release workflows build, upload and attest; the updater
  verifies before any write step; no `git push` to the default branch remains;
  the template carries the gate and permissions.
- End to end (dry run in CI): fixture release with a manifest, consumer
  checkout, expected pull-request body; a tampered template fails closed.
- Existing updater and template tests keep passing unchanged.

## Risks & Mitigations

- `gh attestation verify` needs GitHub CLI 2.49 or newer on the runner:
  pin the CLI version in the updater step, fail with a named token if older.
- A consumer without auto-merge enabled: covered by D4's direct-merge path in
  the verify job.
- A consumer whose branch protection requires reviews: the pull request stays
  open with one alert; the operator decides. No bypass is built.
- First attested release after phase 3 flips verification on: the phase 3
  pull request merges only after a phase 1 release exists on `stable`
  (ordering is enforced by the phase split).
- Size of `test-and-mark-stable.yml` (§27): the manifest work is a script
  call; the contract test asserts the file stays under the guard.

## Rollout

1. Phase 1 merges and the next promote cycle publishes the first attested
   release.
2. Phase 2 merges (verification available, off by default).
3. Phase 3 merges; the following promote cycle is the first safeguarded
   release. The dispatch to every consumer opens one update pull request per
   consumer; the verify job merges them.
4. Operator step (D6) for binance-blessings.

## References

- `shubhodeep1/binance-blessings` issue #317 (the security finding) and its
  hand-edited `.github/workflows/ai-update-workflows.yml` header (the three
  conditions).
- `.github/workflows/update_workflows.yml` ("Fetch latest workflow templates
  from coding-workflows@stable", "Commit and push updates").
- `.github/workflows/test-and-mark-stable.yml` (`release` job, "Notify
  consumer repos via repository_dispatch").
- `workflow-templates/ai-update-workflows.yml`.
- CLAUDE.md §6, §15, §18, §20, §23.C, §27.
