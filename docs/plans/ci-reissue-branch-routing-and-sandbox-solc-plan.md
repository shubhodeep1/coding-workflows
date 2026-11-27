# Run CI-failure re-issues on the branch that holds the failing code, and give the sandbox a Solidity compiler

## Summary

Two independent harness changes that came out of `shubhodeep1/fun-token-multi-chain#821`. Phase 1 makes an unblock-judge re-issue of a CI failure run on the base branch of the pull request that failed, when that pull request already merged into a non-default branch. Phase 2 lets a repository opt in to having a pinned `solc` compiler available inside the network-isolated implement sandbox, so `hardhat compile` and `hardhat test` work there.

## Context

Issue `fun-token-multi-chain#821` was a re-issue, filed by the unblock judge, of a CI failure on PR #781. PR #781 had already merged into the integration branch `orchestrator/project-768` with `CI Reproducible Validation` failing. The re-issue carried only a `<!-- check-failure-triage:pr=781 -->` marker.

- `scripts/unblock_actions.py` (`reissue` branch, around lines 1120-1136) adds a `- Integration branch:` line to a CI-failure re-issue only when the owning PR is still **open** (it uses the PR head ref). For a merged PR the re-issue carries the marker and nothing else.
- `scripts/resolve_integration_ref.sh` (`CI_TRIAGE_PARSE_PY`, lines 226-231) deliberately treats an issue with no `ai:check-triage` label and only a `check-failure-triage:pr=` marker as "not a triage issue to route; it keeps the default-branch behaviour". `agents.md` documents this ("is not routed and keeps the default branch").
- So clarify, plan and implement for #821 all ran on `main`. The code to change existed only on `orchestrator/project-768`. Implement run 42664351523 (head `34a7cb5` on `main`) ended with `BLOCKED: this checkout is the default branch, not orchestrator/project-768 ... There is also no compiler available, so the runtime bytecode size can't be measured.`
- The judge then returned `operator_step` twice, naming this harness gap, and could not file a heal report because `ai:blocked` is not in `HEAL_REPORT_LABELS`.

The compiler half of the BLOCKED message follows from how the sandbox is built. `scripts/codex_isolated_exec.sh` runs the implementer in a container with `--network none` and a private tmpfs home. Its `prepare --deps` phase installs npm and Python dependencies through `scripts/dependency_registry_proxy.py`, whose default allowlist is `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` and `registry.yarnpkg.com`. Hardhat 2 (the consumer pins `hardhat` 2.28.6) downloads `solc` from `binaries.soliditylang.org` the first time it compiles, which cannot happen in that container. This is derived from the code, not from a captured failed-compile log.

#821 itself is closed: the size fix it asked for landed on the integration branch through other issues.

## Goals

- A standalone, non-security CI-failure re-issue whose owning PR merged into a non-default branch that still exists is clarified, planned, implemented and validated on that branch, and its implementation PR targets that branch.
- Re-issues that already exist (marker only, no branch line) get the same routing without being edited.
- Every other issue resolves to exactly the branch it resolves to today.
- A repository that sets `SANDBOX_SOLC_VERSIONS` gets those `solc` versions, checksum-verified, available to Hardhat inside the implement sandbox with no network access at agent time.
- A repository that does not set `SANDBOX_SOLC_VERSIONS` sees no behaviour change, no new network host and no added run time.

## Non-goals

- Foundry. The sandbox image gains no `forge` binary; `npm run test:forge` stays unverifiable in the sandbox (user decision Q8: A).
- Detecting the compiler version from `hardhat.config.*` or `foundry.toml` (user decision Q7: A).
- Adding `binaries.soliditylang.org` to the default `DEPENDENCY_PROXY_ALLOWED_HOSTS` (user decision Q9: A).
- A new "the branch is already green, do not re-issue" check in the unblock judge.
- Accepting `ai:blocked` as a heal-report label (`HEAL_REPORT_LABELS`).
- Changing what the unblock judge writes into a re-issue body. Phase 1 fixes routing in the resolver, so the judge's output stays as it is.
- The false "unrouted reply" warning on #821; that is fixed separately in PR #9167.

## Constraints

- **§6 naming immutability.** No existing identifier is renamed or removed. `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED`, the `CI_TRIAGE_PR_BRANCH_ROUTING` log prefix, the resolver's exit codes (0, 1, 2, 3, 75) and the `ci_owner` object keep their meaning. New names (`CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING_ENABLED`, `SANDBOX_SOLC_VERSIONS`, the new log prefixes and outcome tokens) must be checked for collisions before use.
- **§4 env vars.** Both new variables have defaults: `true` and empty.
- **§15 GitHub API hygiene.** Phase 1 adds at most two REST reads (the PR, then the base ref) and only for an issue that matches the re-issue shape; all other issues add zero calls. The resolver already holds the issue JSON, which is reused.
- **§27 workflow file size.** `.github/workflows/implement.yml` is 433,073 bytes; the guard is 480,000. Phase 2 adds only `env:` lines there. Any logic goes into `scripts/`. Check with `wc -c` after editing.
- **§14 consumers.** Both phases reach all 13 repositories in `.github/ai/consumer_repos.json` on the next `@stable` sync. Phase 1 is on by default for all of them. Phase 2 is inert until a repository sets the variable.
- **Security (§1).** The resolver's routing decision is based on issue-body text, so the same trust checks as the existing triage routing apply (trusted author, same-repository PR, validated ref name, live branch). The compiler is an executable fetched from the network: it must be pinned by version and verified by SHA-256 before the agent can run it, and the fetch must not widen egress for anything else.
- **§20 / §30.** Each phase ships one `changelog.d/` fragment and one `agents.d/` fragment. The two `agents.md` sentences that Phase 1 makes false are corrected by a small direct edit (§30.B).
- **§18.** No standalone script, supervisor or DB operation is introduced.

## Automation (§18.E)

- **New script, extended script, or existing code only:** both phases extend existing scripts (`scripts/resolve_integration_ref.sh`, `scripts/codex_isolated_exec.sh`, `scripts/codex_isolated_workspace.py`, `scripts/dependency_registry_proxy.py`). No new standalone script.
- **Scheduler / entry point:** Phase 1 runs inside the existing "Resolve integration ref" step of `.github/workflows/clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml` and `validate.yml` (their existing triggers). Phase 2 runs inside the existing `codex_isolated_exec.sh prepare --deps` calls in `.github/workflows/implement.yml` and `scripts/implement_step_prepush_test_gate.sh`.
- **Long-running supervisor:** none.
- **DB work:** none.
- **`docs/scripts-pending-removal.md` entry:** none needed; nothing single-use or long-running is added.

## Approach

### Phase 1: route in the shared resolver

All five phase workflows already call `scripts/resolve_integration_ref.sh` to pick the branch. Today the resolver returns `skip` for the judge's re-issue shape. Phase 1 replaces that one `skip` with a verified lookup:

1. The issue has no `ai:check-triage` label, no `Integration branch:` / `Target branch:` line (those still win, unchanged), and exactly one line-anchored `<!-- check-failure-triage:pr=<n> -->` marker with no other `check-failure-triage:` marker.
2. The issue is not a pull request and its `author_association` is OWNER, MEMBER or COLLABORATOR.
3. One read of `repos/<repo>/pulls/<n>`: the PR is in this repository (head and base), `merged` is true, and `base.ref` differs from `base.repo.default_branch`.
4. `base.ref` passes the same ref-name validation the triage routing uses, and one read of `repos/<repo>/git/ref/heads/<base>` confirms the branch exists.
5. The resolver prints `base.ref` and logs `CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING issue=<n> pr=<n> outcome=routed branch=<ref>`.

Anything else keeps today's result (empty output, default branch) and logs one `outcome=skipped reason=<token>` line: `pr_open`, `pr_not_merged`, `base_is_default`, `base_missing`, `base_ref_invalid`, `author_untrusted`, `pr_cross_repository`, `pr_lookup_failed`, `flag_off`.

**Fail-open, not fail-closed.** The existing triage routing refuses (exit 3) when it cannot verify, because falling back would silently edit the wrong branch for an issue that explicitly asked for PR-branch routing. For this shape the default branch is today's behaviour for every repository, so an unverifiable lookup keeps it. A rate-limited read still exits 75 as elsewhere in the resolver.

The existing `RETARGET_MERGED_BASE_ENABLED` step in `implement.yml` runs after the resolver and already handles an integration branch whose own PR has merged, so a finished integration branch is retargeted without new code.

**Alternatives considered.** (a) Have the judge write an `Integration branch:` line into new re-issues: smaller, but it does not help re-issues that already exist and leaves the resolver's documented "keeps the default branch" rule in place. (b) Change only `implement.yml`, as the judge's instruction worded it: clarify and plan would still read `main`, which is what produced #821's plan against the wrong tree.

### Phase 2: an opt-in compiler stage in `prepare --deps`

A repository sets the repo variable `SANDBOX_SOLC_VERSIONS` to a comma- or whitespace-separated list of exact versions, for example `0.8.34`. Empty (the default) skips the stage entirely.

When set, `codex_isolated_exec.sh prepare --deps` runs one more step inside the existing network-isolated dependency container, after the npm and Python installs:

1. Validate each entry against `^0\.[0-9]{1,2}\.[0-9]{1,2}$`; at most 4 entries. An invalid entry is dropped with a warning.
2. Start the registry proxy for this step with an allowlist of exactly `binaries.soliditylang.org`. The general allowlist (`DEPENDENCY_PROXY_ALLOWED_HOSTS`) is not changed and is not used for this step.
3. Download `https://binaries.soliditylang.org/linux-amd64/list.json`, find the build for each version, download that binary, and compare its SHA-256 with the value in `list.json`. A mismatch deletes the file and fails that version.
4. Place `list.json` and the verified binaries in the layout the pinned Hardhat reads its compiler cache from, in a directory under the sandbox root, and mount it read-only into the agent container at the cache path Hardhat uses for `HOME=/home/agent`.
5. Log `CODEX_ISOLATION solc version=<v> outcome=ready|checksum_mismatch|not_listed|download_failed|invalid_version`.

The stage is fail-open like the rest of the dependency phase: a failure logs a `::warning::` and the agent runs without the compiler, marking what it cannot run as UNVERIFIED, exactly as it does today.

The cache layout and the exact files Hardhat needs to treat a compiler as already downloaded must be read from the Hardhat source for the supported major version rather than assumed. The plan's acceptance test (below) is the contract: `hardhat compile` succeeds with `--network none`.

**Alternatives considered.** Bake `solc` into the sandbox image: every repository would pay for it and the version would not follow the repository. Allow the host for the whole dependency phase: wider egress for npm lifecycle-free installs that do not need it (rejected by Q9: A).

## Phases & Merge Strategy

Two phases, one PR each. Neither depends on the other; they touch disjoint files except for documentation fragments, which use separate fragment files.

### Phase 1 — Base-branch routing for merged CI-failure re-issues

- **Scope:** the resolver change, its flag, tests and docs.
- **Files:** `scripts/resolve_integration_ref.sh`, `tests/fixtures/integration_ref_resolver_ci_triage/` (new fixtures), `tests/test_resolve_integration_ref_ci_triage.py`, the "Resolve integration ref" step `env:` in `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `validate.yml`, `README.md`, `agents.md` (two-sentence correction), `agents.d/<issue>-reissue-base-branch-routing.md`, `changelog.d/<issue>-reissue-base-branch-routing.md`.
- **Done when:** the resolver self-test and `tests/test_resolve_integration_ref_ci_triage.py` pass with the new fixtures, and every pre-existing fixture still produces its recorded output.
- **Rollback:** set `CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING_ENABLED=false`, or revert the PR.

### Phase 2 — Opt-in `solc` in the implement sandbox

- **Scope:** the compiler stage, its variable, tests and docs.
- **Files:** `scripts/codex_isolated_exec.sh`, `scripts/codex_isolated_workspace.py` (only if the verify/place logic lives there), `scripts/dependency_registry_proxy.py` (only if a per-call allowlist needs a new entry point), `scripts/implement_step_prepush_test_gate.sh` (forward the variable), `.github/workflows/implement.yml` (`env:` lines on the three steps that already forward `DEPENDENCY_PROXY_ALLOWED_HOSTS`), tests for the above, `README.md`, `agents.d/<issue>-sandbox-solc.md`, `changelog.d/<issue>-sandbox-solc.md`.
- **Done when:** the tests below pass, and with the variable unset the `prepare --deps` command line, proxy allowlist and log output are byte-identical to before.
- **Rollback:** unset `SANDBOX_SOLC_VERSIONS` in the repository, or revert the PR.

## Implementation Steps

### Phase 1

1. `scripts/resolve_integration_ref.sh`: add `ci_triage_reissue_base_routing_enabled()`, parsing `CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING_ENABLED` like `ci_triage_prose_marker_skip_enabled` (unset, empty or `true` on; `false` off; anything else off with one `::warning::`).
2. Same file, `CI_TRIAGE_PARSE_PY`: at the `not has_label and re.search(r"<!--\s*check-failure-triage:(?!pr=)", body) is None` branch, print `reissue <pr>` instead of `skip` when the flag argument is on, the body has exactly one line-anchored `pr=` marker with a valid number, the item is not a pull request and the author association is trusted. Otherwise keep `skip`.
3. Same file: add `resolve_ci_triage_reissue_base()` that performs the PR read and the ref read through `ci_triage_gh_get`, applies the checks in Approach, prints the base ref or nothing, and always returns 0 except for the resolver's existing rate-limit exit 75.
4. Same file, `resolve_ci_triage_branch()`: handle the new `reissue` verdict by calling the new function.
5. Same file: when `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` is off the resolver never reads the issue JSON, so the new routing is off too. Document that in the flag comment.
6. The five workflows: add `CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING_ENABLED: ${{ vars.CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING_ENABLED || 'true' }}` beside the existing routing flag and pass it on the resolver's command line the same way. Run `wc -c` on `implement.yml`.
7. Extend the self-test runner (`run_case`) so a fixture can set or unset the new flag, defaulting fixtures to an explicit value so an outer environment cannot leak in.
8. Docs: README env-var table row; `agents.d/` fragment under the section that documents triage routing; correct the two `agents.md` sentences ("is not routed and keeps the default branch", "never the base ref").

### Phase 2

1. `scripts/codex_isolated_exec.sh`: read `SANDBOX_SOLC_VERSIONS` on the host, validate, and skip the stage when nothing valid remains.
2. Same file: after the existing install container, start a second proxy broker limited to `binaries.soliditylang.org` and run the fetch-and-verify step in a `--network none` container with only the proxy socket and an empty output directory mounted. No repository file is mounted into it.
3. Verify SHA-256 on the host side as well, from the host copy of `list.json` taken before the container could write to the directory, mirroring how `nested.json` is copied host-side today.
4. Mount the verified directory read-only into the agent container at Hardhat's compiler cache path, for both the Codex and the Claude engine branches of `run`.
5. `scripts/implement_step_prepush_test_gate.sh`: forward the variable to its own `prepare --deps` call so the gate's `npm test` sees the same compiler.
6. `.github/workflows/implement.yml`: add `SANDBOX_SOLC_VERSIONS: ${{ vars.SANDBOX_SOLC_VERSIONS || '' }}` to the steps at the three places that forward `DEPENDENCY_PROXY_ALLOWED_HOSTS`. Run `wc -c`.
7. Docs: README env-var row including the new egress host and that it applies only when the variable is set; `agents.d/` and `changelog.d/` fragments.

## Files & Modules

- `scripts/resolve_integration_ref.sh`
- `tests/test_resolve_integration_ref_ci_triage.py`
- `tests/fixtures/integration_ref_resolver_ci_triage/*.json` `[new]` fixtures
- `.github/workflows/clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `validate.yml`
- `scripts/codex_isolated_exec.sh`
- `scripts/codex_isolated_workspace.py`
- `scripts/dependency_registry_proxy.py`
- `scripts/implement_step_prepush_test_gate.sh`
- tests covering `codex_isolated_exec.sh` and the proxy (extend the existing test modules for those scripts)
- `README.md`, `agents.md`
- `agents.d/<issue>-reissue-base-branch-routing.md` `[new]`, `agents.d/<issue>-sandbox-solc.md` `[new]`
- `changelog.d/<issue>-reissue-base-branch-routing.md` `[new]`, `changelog.d/<issue>-sandbox-solc.md` `[new]`

## Tests

### Phase 1 (unit, resolver fixtures)

- Marker-only re-issue, PR merged into `orchestrator/project-1`, branch exists: prints `orchestrator/project-1`, exit 0, one `outcome=routed` line.
- Same, PR merged into the default branch: empty output, `reason=base_is_default`.
- Same, PR still open: empty output, `reason=pr_open` (the judge already writes the head ref for that case).
- Same, PR closed without merging: empty output, `reason=pr_not_merged`.
- Same, base branch deleted (404): empty output, `reason=base_missing`.
- PR read fails with a non-rate-limit error: empty output, `reason=pr_lookup_failed`, exit 0.
- PR read rate-limited: exit 75.
- Head or base in another repository: `reason=pr_cross_repository`.
- Author association `NONE`: `reason=author_untrusted`.
- Two different `pr=` markers, or a marker that is not line-anchored: today's `skip`, no API call.
- Body also has an `Integration branch:` line: that line wins, no PR read.
- Flag `false`: today's `skip`, no API call.
- Every pre-existing fixture in `integration_ref_resolver` and `integration_ref_resolver_ci_triage` passes unchanged.
- The gh mock asserts the exact set of API calls per fixture, so the "zero extra calls for ordinary issues" goal is tested.

### Phase 2

- Unit: version validation (accepts `0.8.34`; rejects `latest`, `0.8`, `v0.8.34`, `0.8.34;rm`, a fifth entry).
- Unit: checksum match places the file; mismatch deletes it and reports `checksum_mismatch`; a version missing from `list.json` reports `not_listed`.
- Unit: with the variable unset, the docker argument list and proxy allowlist equal the recorded baseline.
- Unit: the compiler proxy refuses any host other than `binaries.soliditylang.org`.
- Integration (Docker, in the existing isolated-exec test job if one runs Docker; otherwise marked as a manual check in the PR body): a fixture project with `hardhat` 2.x and one contract compiles under `--network none` after `prepare --deps` with `SANDBOX_SOLC_VERSIONS` set, and fails to compile without it.

## Risks & Mitigations

- **A re-issue lands on a branch nobody chose by hand.** It is only ever the verified base of the PR the issue names, the flag turns it off, and the log line names the branch.
- **The integration branch has moved on and the work is already done there** (this is what happened to #821). ACCEPTED — the planner and implementer see the current branch and can report nothing to do; a "branch already green" check is a non-goal here.
- **Issue-body text steers the checkout.** Mitigated by the trusted-author check, the same-repository PR check, ref-name validation and the live-branch read; an `ai:check-triage`-labelled issue keeps its stricter existing path.
- **Hardhat's cache layout differs between versions.** Mitigated by the Docker acceptance test; if a future Hardhat major changes the layout the stage fails open with a warning. Hardhat 3 support is ACCEPTED — pending discovery when a consumer adopts it.
- **Supply chain: a compiler binary from the network runs in the sandbox.** Mitigated by exact version pinning and SHA-256 verification against `list.json`, host-side re-verification, a read-only mount, and the sandbox having no credentials or network. `list.json` and the binary come from the same host, so this detects corruption and partial downloads, not a compromised origin. ACCEPTED — the same trust the consumer's own CI already places in that host.
- **Run time.** One extra container start plus a download of roughly 10-15 MB per version, only for repositories that opt in.
- **`implement.yml` size.** Only `env:` lines are added; verify with `wc -c` and keep it under 480,000 bytes (§27).

## Rollout

- Phase 1 merges on by default and reaches consumers on the next `@stable` sync. Watch for `CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING ... outcome=routed` lines. Turn off per repository with `CI_TRIAGE_REISSUE_BASE_BRANCH_ROUTING_ENABLED=false`.
- Phase 2 merges inert. After it reaches `@stable`, set `SANDBOX_SOLC_VERSIONS=0.8.34` on `shubhodeep1/fun-token-multi-chain` (a repository-variable change, which needs the owner's approval per CLAUDE.md §23.C) and confirm a `CODEX_ISOLATION solc version=0.8.34 outcome=ready` line in the next implement run.
- The two phases can merge in either order.

## References

- `shubhodeep1/fun-token-multi-chain#821` (closed), its implement run 42664351523, and PR #781.
- `shubhodeep1/coding-workflows` PR #9167 (the separate false-warning fix).
- Issue #7032 (unblock judge CI-failure re-issue and its owning PR) and #7283 / #7540 (triage routing fail-closed rules), as cited in `scripts/resolve_integration_ref.sh` and `agents.md`.
- `scripts/codex_isolated_exec.sh` header comment (sandbox isolation contract) and `scripts/dependency_registry_proxy.py` (`DEFAULT_ALLOWED_HOSTS`).
